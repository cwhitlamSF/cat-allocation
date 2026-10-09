# modelkit/dialogs.py
"""
GUI dialogs for file selection and user interaction.
Uses tkinter on Windows, supports headless mode.
"""

import ctypes
import glob
import sys
import os
from .common import get_logger

MYLOGGER = get_logger('modelkit.dialogs')

# Win32 MessageBox flags and return codes
_MB_OK, _MB_OKCANCEL, _MB_YESNOCANCEL = 0x0, 0x1, 0x3
_MB_ICONS = {'error': 0x10, 'question': 0x20, 'warning': 0x30, 'info': 0x40, None: 0x0}
_MB_TOPMOST = 0x40000
_IDOK, _IDCANCEL, _IDYES, _IDNO = 1, 2, 6, 7


def _messageBox(titlestring: str, textstring: str, buttons: int, icon: str = None):
    """Show a topmost Win32 message box; return the button code, or None if unavailable."""
    try:
        return ctypes.windll.user32.MessageBoxW(
            None, textstring, titlestring, buttons | _MB_ICONS.get(icon, 0) | _MB_TOPMOST)
    except AttributeError:
        # Not on Windows
        print(f'{titlestring}: {textstring}')
    except Exception as e:
        MYLOGGER.warning(f'Message box failed: {e}')
    return None


def showMessageBox(titlestring: str, textstring: str, icon: str = None) -> None:
    """
    Display a Windows message box (modal, blocks execution).

    Parameters
    ----------
    titlestring : str
        Title of the message box.
    textstring : str
        Message body.
    icon : str, optional
        'info', 'warning', 'error' or 'question'. Default: no icon.
    """
    _messageBox(titlestring, textstring, _MB_OK, icon)


def askYesNoCancel(titlestring: str, textstring: str, icon: str = 'warning'):
    """
    Ask a Yes/No/Cancel question.

    Returns
    -------
    True for Yes, False for No, None for Cancel (or if no dialog could be shown).
    """
    result = _messageBox(titlestring, textstring, _MB_YESNOCANCEL, icon)
    return {_IDYES: True, _IDNO: False}.get(result)


def askOkCancel(titlestring: str, textstring: str, icon: str = 'warning') -> bool:
    """Ask an OK/Cancel question. Returns True for OK; False for Cancel or no dialog."""
    return _messageBox(titlestring, textstring, _MB_OKCANCEL, icon) == _IDOK


def tkinterSelectFromList(options: list) -> str:
    """
    Present a tkinter dropdown menu for user selection.
    
    Parameters
    ----------
    options : list of str
        List of options to display.
    
    Returns
    -------
    str
        Selected option. Defaults to the first option if the window is
        closed without a selection being made.
    """
    import tkinter as tk
    from tkinter import ttk

    MYLOGGER.debug(f'Starting selection dialog with {len(options)} options')

    def close_and_run(event):
        # Close the Tkinter window
        root.destroy()

    # Create the main window
    root = tk.Tk()
    root.eval('tk::PlaceWindow . center')
    root.title("Select Analysis Mode")

    # Adjust size
    root.geometry("300x100")

    # Create a variable to hold the selected option
    dropdown_var = tk.StringVar()

    # Create the dropdown menu
    dropdown = ttk.Combobox(root, textvariable=dropdown_var, width=40)
    dropdown['values'] = options
    dropdown.bind("<<ComboboxSelected>>", close_and_run)
    dropdown.pack(pady=20)

    # Set a default value
    dropdown_var.set(options[0])

    # Run the application
    root.mainloop()

    return dropdown_var.get()

def selectAnalysisFile(filetypes=None) -> str:
    """
    Open a file dialog to select an analysis file (Excel or gzip spec).
    
    Returns
    -------
    str
        Full path to selected file, or empty string if cancelled.
    """
    if filetypes is None:
        filetypes = [
            ("Excel File", "*.xlsx;*.xlsm;*.xlsb"),
            ("Spec File", "*.gzip")
        ]
    
    from tkinter import Tk
    from tkinter import filedialog
    
    MYLOGGER.debug('Starting Select Analysis File dialog')
    
    root = Tk()
    root.wm_attributes('-topmost', 1)
    root.withdraw()
    
    selected_file = filedialog.askopenfilename(
        parent=root,
        title="Select analysis file.",
        multiple=False,
        filetypes=filetypes
    )
    root.destroy()

    if selected_file:
        return selected_file.replace(os.sep, "/")
    return ""


def selectSaveFile(title="Save as", initialfile="", initialdir=None, filetypes=None,
                   defaultextension="") -> str:
    """Save As dialog. Returns the chosen path, or "" if cancelled. Asks before replacing a file."""
    from tkinter import Tk
    from tkinter import filedialog

    root = Tk()
    root.wm_attributes('-topmost', 1)
    root.withdraw()
    chosen = filedialog.asksaveasfilename(
        parent=root, title=title, initialfile=initialfile, initialdir=initialdir or None,
        filetypes=filetypes or [("All files", "*.*")], defaultextension=defaultextension)
    root.destroy()
    return chosen.replace(os.sep, "/") if chosen else ""


def selectAnalysisFile_LocalVersion(filetypes=[("Excel File","*.xlsx;*.xlsm;*.xlsb"),("Spec File","*.gzip")]):
    #For selecting analysis file from python script, not via panel app
    from tkinter import Tk
    from tkinter import filedialog

    MYLOGGER.debug('Starting Select Analysis File or Folder')

    root = Tk()
    root.wm_attributes('-topmost', 1)
    root.withdraw()
    selectedfile=filedialog.askopenfilename(parent=root,title="Select analysis file. Use filetype dropdown to specify Excel or gzip prepared Spec file.",
                                 multiple=False,filetypes=filetypes)

    if selectedfile:
        return selectedfile.replace(os.sep, "/")
    else:
        return ""


def selectAnalysisFile_PanelVersion(selectedfile=None, base_dir='/app/RSA'):
    # base_dir: folder the Panel app looks in; each model passes its own (default kept for RSA)
    MYLOGGER.debug('Starting Select Analysis File or Folder')
    specfiles=glob.glob(os.path.join(base_dir, selectedfile))

    try:
        if len(specfiles)>0:
            return specfiles[0].replace(os.sep, "/")
        else:
            return None
    except:
        return None
