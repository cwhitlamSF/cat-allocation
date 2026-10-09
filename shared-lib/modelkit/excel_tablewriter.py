# modelkit/excel_tablewriter.py
"""
Write the rows of Excel tables (ListObjects) into a copy of a workbook, without Excel.

    writeTables('Template.xlsm', 'My Run.xlsm', {'DataSources': rows, 'Outputs': rows})

Only the cells inside each table's data body change, and each table is resized to fit its rows.
Every other part of the file is copied byte for byte, so VBA, buttons and shapes, data validation,
named ranges, formatting outside the tables, and the other sheets all survive. (openpyxl rebuilds
the whole file and drops shapes and form controls, which is why it isn't used here.)

Within a changed sheet, only the <sheetData> element (and the used-range reference) is rewritten;
the rest of the sheet's XML, including its namespace declarations, is kept as it was. Standard
library only (xml.etree), no lxml.

Rows are lists of values in the table's column order: str, int/float, bool or None (blank).
Cells keep the number format and style of the column's first data cell. Tables with a totals row
or calculated columns are refused rather than half-written.
"""

import os
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from math import isfinite

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
RELNS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
XMLNS = 'http://www.w3.org/XML/1998/namespace'
N = {'m': NS}


def _q(tag):
    return f'{{{NS}}}{tag}'


def _cell(ref):
    m = re.fullmatch(r'\$?([A-Z]+)\$?(\d+)', ref)
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)), col


def _letters(col):
    s = ''
    while col:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def _rels_path(part):
    d, f = posixpath.split(part)
    return posixpath.join(d, '_rels', f + '.rels')


def _target(base_part, target):
    if target.startswith('/'):
        return target.lstrip('/')
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _xml(z, part):
    return ET.fromstring(z.read(part))


def tableParts(path):
    """{table name: (sheet part, table part)} for the workbook at path."""
    found = {}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        wb = _xml(z, 'xl/workbook.xml')
        wbrels = {r.get('Id'): _target('xl/workbook.xml', r.get('Target'))
                  for r in _xml(z, 'xl/_rels/workbook.xml.rels')}
        for sheet in wb.find('m:sheets', N):
            sheetpart = wbrels.get(sheet.get(f'{{{RELNS}}}id'))
            relspart = _rels_path(sheetpart) if sheetpart else None
            if not relspart or relspart not in names:
                continue
            for rel in _xml(z, relspart):
                if rel.get('Type', '').endswith('/table'):
                    tpart = _target(sheetpart, rel.get('Target'))
                    t = _xml(z, tpart)
                    found[t.get('displayName') or t.get('name')] = (sheetpart, tpart)
    return found


# ---------------------------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------------------------
_BAD = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _new_cell(ref, value, style):
    """A <c> element for value (None if the value can't be stored, e.g. NaN)."""
    c = ET.Element(_q('c'), r=ref)
    if style is not None:
        c.set('s', style)
    if value is None or (isinstance(value, str) and value == ''):
        return c if style is not None else None          # blank: keep the column's formatting only
    if isinstance(value, bool):
        c.set('t', 'b')
        ET.SubElement(c, _q('v')).text = '1' if value else '0'
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not isfinite(value):
            return c if style is not None else None
        whole = float(value).is_integer() and abs(value) < 1e15
        ET.SubElement(c, _q('v')).text = repr(int(value)) if whole else repr(float(value))
    else:
        text = _BAD.sub('', str(value))
        c.set('t', 'inlineStr')
        t = ET.SubElement(ET.SubElement(c, _q('is')), _q('t'))
        if text != text.strip() or '\n' in text:
            t.set(f'{{{XMLNS}}}space', 'preserve')
        t.text = text
    return c


def _rewrite_sheetdata(data, edits):
    """edits: list of (header row, first col, last col, old last row, new rows). Edits <sheetData>."""
    rows = {int(r.get('r')): r for r in data.findall('m:row', N)}

    for hdr, c1, c2, oldlast, newrows in edits:
        # styles from the first data row, so a column keeps its number format
        styles = {}
        first = rows.get(hdr + 1)
        if first is not None:
            for c in first.findall('m:c', N):
                col = _cell(c.get('r'))[1]
                if c1 <= col <= c2 and c.get('s') is not None:
                    styles[col] = c.get('s')
        newlast = hdr + max(len(newrows), 1)
        # clear the old body (and anything in these columns where the new body will go)
        for rnum in range(hdr + 1, max(oldlast, newlast) + 1):
            row = rows.get(rnum)
            if row is None:
                continue
            for c in row.findall('m:c', N):
                if c1 <= _cell(c.get('r'))[1] <= c2:
                    row.remove(c)
        # the new body
        for i, values in enumerate(newrows):
            rnum = hdr + 1 + i
            row = rows.get(rnum)
            if row is None:
                row = ET.Element(_q('row'), r=str(rnum))
                rows[rnum] = row
            cells = row.findall('m:c', N)
            for j, value in enumerate(values[:c2 - c1 + 1]):
                c = _new_cell(f'{_letters(c1 + j)}{rnum}', value, styles.get(c1 + j))
                if c is not None:
                    cells.append(c)
            # cells must be in column order, alongside any cells outside the table
            for c in row.findall('m:c', N):
                row.remove(c)
            for c in sorted(cells, key=lambda c: _cell(c.get('r'))[1]):
                row.append(c)

    # rows back in order; drop rows left with no cells and no formatting of their own
    for r in data.findall('m:row', N):
        data.remove(r)
    for rnum in sorted(rows):
        row = rows[rnum]
        row.attrib.pop('spans', None)            # optional; the old value may no longer be right
        if len(row) == 0 and not (set(row.attrib) - {'r'}):
            continue
        data.append(row)
    return [_cell(c.get('r')) for c in data.iter(_q('c'))]


_DECL = re.compile(r'\sxmlns(?::([A-Za-z_][\w.-]*))?\s*=\s*"([^"]*)"')


def _escape(text, attr=False):
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    if attr:
        text = text.replace('"', '&quot;').replace('\n', '&#10;').replace('\r', '&#13;').replace('\t', '&#9;')
    return text


def _name(qname, prefixes):
    """'{uri}local' -> 'prefix:local' using the worksheet's own prefixes ('' = default namespace)."""
    if not qname.startswith('{'):
        return qname
    uri, local = qname[1:].split('}', 1)
    if uri not in prefixes:
        raise ValueError(f'namespace {uri} is not declared on the worksheet')
    p = prefixes[uri]
    return f'{p}:{local}' if p else local


def _serialise(el, prefixes):
    """Serialise an element using the prefixes declared on the worksheet, so the fragment can go
    back into the sheet's XML text without any namespace declarations of its own."""
    parts = ['<', _name(el.tag, prefixes)]
    for k, v in el.attrib.items():
        parts += [' ', _name(k, prefixes), '="', _escape(v, attr=True), '"']
    if len(el) or el.text:
        parts.append('>')
        if el.text:
            parts.append(_escape(el.text))
        for child in el:
            parts.append(_serialise(child, prefixes))
            if child.tail:
                parts.append(_escape(child.tail))
        parts += ['</', _name(el.tag, prefixes), '>']
    else:
        parts.append('/>')
    return ''.join(parts)


def _rewrite_sheet(xml_bytes, edits):
    """Rewrite only the <sheetData> element of a worksheet (and its <dimension>), leaving the rest
    of the XML text, including the namespace declarations, exactly as it was."""
    text = xml_bytes.decode('utf-8')
    root_tag = re.search(r'<(?:[\w.-]+:)?worksheet\b[^>]*>', text)
    decls = {(p or ''): uri for p, uri in _DECL.findall(root_tag.group(0))}
    main_prefix = next((p for p, uri in decls.items() if uri == NS), '')

    m = re.search(r'<(%s)sheetData\b[^>]*?(?:/>|>.*?</\1sheetData\s*>)' %
                  (re.escape(main_prefix + ':') if main_prefix else ''), text, re.S)
    if m is None:
        raise ValueError('worksheet has no sheetData')
    # parse the fragment inside a wrapper carrying the worksheet's namespace declarations
    wrapper_decls = ''.join(f' xmlns{":" + p if p else ""}="{uri}"' for p, uri in decls.items())
    wrapper = ET.fromstring(f'<wrap{wrapper_decls}>{m.group(0)}</wrap>'.encode('utf-8'))
    data = wrapper[0]
    cells = _rewrite_sheetdata(data, edits)

    prefixes = {uri: p for p, uri in decls.items()}
    prefixes[XMLNS] = 'xml'
    fragment = _serialise(data, prefixes)
    text = text[:m.start()] + fragment + text[m.end():]

    # the used range
    if cells:
        ref = f"A1:{_letters(max(c for _, c in cells))}{max(r for r, _ in cells)}"
        text = re.sub(r'(<(?:[\w.-]+:)?dimension\b[^>]*?\bref=")[^"]*(")', rf'\g<1>{ref}\g<2>', text, count=1)
    return text.encode('utf-8')


def _set_table_ref(xml_bytes, newref):
    """Update ref on the <table> element and its <autoFilter>, leaving the rest of the XML as is."""
    text = xml_bytes.decode('utf-8')
    text = re.sub(r'(<(?:[\w.-]+:)?table\b[^>]*?\sref=")[^"]*(")', rf'\g<1>{newref}\g<2>', text, count=1)
    text = re.sub(r'(<(?:[\w.-]+:)?autoFilter\b[^>]*?\sref=")[^"]*(")', rf'\g<1>{newref}\g<2>', text, count=1)
    return text.encode('utf-8')


# ---------------------------------------------------------------------------------------------
# Workbook
# ---------------------------------------------------------------------------------------------
def writeTables(src, dst, tables):
    """
    Copy the workbook src to dst with each named table's data body replaced.
    tables: {table name: list of rows}. Returns {table name: new range}.
    dst may be the same file as src; it is written to a temporary file and moved into place.
    Raises KeyError for a table that isn't in the workbook.
    """
    parts = tableParts(src)
    missing = [t for t in tables if t not in parts]
    if missing:
        raise KeyError(f"Not in {os.path.basename(src)}: {', '.join(missing)}")

    with zipfile.ZipFile(src) as z:
        replaced, by_sheet, refs = {}, {}, {}
        for name, rows in tables.items():
            sheetpart, tpart = parts[name]
            t = _xml(z, tpart)
            if int(t.get('totalsRowCount') or 0):
                raise ValueError(f"{name} has a totals row; writing it isn't supported")
            if int(t.get('headerRowCount', '1')) != 1:
                raise ValueError(f"{name} has no header row; writing it isn't supported")
            if any(c.find('m:calculatedColumnFormula', N) is not None for c in t.find('m:tableColumns', N)):
                raise ValueError(f"{name} has calculated columns; writing it isn't supported")
            start, end = t.get('ref').split(':')
            hdr, c1 = _cell(start)
            oldlast, c2 = _cell(end)
            rows = [list(r) + [None] * (c2 - c1 + 1 - len(r)) for r in rows]
            newref = f"{_letters(c1)}{hdr}:{_letters(c2)}{hdr + max(len(rows), 1)}"
            replaced[tpart] = _set_table_ref(z.read(tpart), newref)
            by_sheet.setdefault(sheetpart, []).append((hdr, c1, c2, oldlast, rows))
            refs[name] = newref
        for sheetpart, edits in by_sheet.items():
            replaced[sheetpart] = _rewrite_sheet(z.read(sheetpart), edits)

        # Excel rebuilds the calculation chain; a stale one makes it "repair" the file
        drop = set()
        if 'xl/calcChain.xml' in z.namelist():
            drop.add('xl/calcChain.xml')
            ct = z.read('[Content_Types].xml').decode('utf-8')
            replaced['[Content_Types].xml'] = re.sub(
                r'<Override\b[^>]*PartName="/xl/calcChain\.xml"[^>]*/>', '', ct).encode('utf-8')
            rels = z.read('xl/_rels/workbook.xml.rels').decode('utf-8')
            replaced['xl/_rels/workbook.xml.rels'] = re.sub(
                r'<Relationship\b[^>]*Target="[^"]*calcChain\.xml"[^>]*/>', '', rels).encode('utf-8')

        fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(dst)[1], dir=os.path.dirname(os.path.abspath(dst)))
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as out:
                for info in z.infolist():
                    if info.filename in drop:
                        continue
                    data = replaced.get(info.filename)
                    if data is None:
                        out.writestr(info, z.read(info.filename))      # unchanged, including vbaProject.bin
                    else:
                        info2 = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                        info2.compress_type = zipfile.ZIP_DEFLATED
                        info2.external_attr = info.external_attr
                        out.writestr(info2, data)
        except BaseException:
            os.remove(tmp)
            raise

    # mkstemp creates the file readable by its owner only; give it the replaced file's permissions,
    # or the usual ones for a new file
    if os.path.exists(dst):
        mode = os.stat(dst).st_mode & 0o777
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    os.chmod(tmp, mode)
    os.replace(tmp, dst)
    return refs
