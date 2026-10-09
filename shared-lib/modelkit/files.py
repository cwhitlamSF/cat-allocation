# modelkit/files.py
"""
Folders and files: create, empty, delete, list; parquet read/write.
"""

import ast
import glob
import os
import shutil
from io import StringIO
from pathlib import Path

import pandas as pd
import polars as pl
from .common import get_logger
from .lists import list_intersection

MYLOGGER = get_logger('modelkit.files')


def createFolderIfNot(rootfolder,newfolder):
    #Checks if the desired directory exists
    #If the directory exists: returns the path to the directory
    #If the directory does not exist: creates the directory and returns the path in string form

    retPath = os.path.join(rootfolder, newfolder)
    if not os.path.isdir(retPath):
        os.makedirs(retPath)
    return str(retPath).replace(os.sep, "/")

def deleteFolderIfExists(rootfolder,newfolder=None):
    #Checks if the desired directory exists
    #If the directory exists: returns the path to the directory
    #If the directory does not exist: creates the directory and returns the path in string form

    if not newfolder:
        retPath=rootfolder
    else:
        retPath = os.path.join(rootfolder, newfolder)
    if os.path.exists(retPath):
        shutil.rmtree(retPath)

def empty_dir(d: Path, *, keep_root_files: bool = False) -> None:
    """
    If keep_root_files=True, only deletes subdirectories (and their contents),
    leaving files directly in `d` untouched.
    """
    d.mkdir(parents=True, exist_ok=True)

    for p in d.iterdir():
        if p.is_dir():
            shutil.rmtree(p)
        elif not keep_root_files:
            p.unlink()

def deleteFilesFromFolder(folder, keep_suffixes=()):
    """
    Delete every file and subfolder in `folder`, except files whose names end
    with one of `keep_suffixes` (e.g. ("- lalae.parquet", "- summary lalae.parquet")).
    """
    keep_suffixes = tuple(keep_suffixes)
    for filename in os.listdir(folder):
        if keep_suffixes and filename.endswith(keep_suffixes):
            continue
        file_path = os.path.join(folder, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print('Failed to delete %s. Reason: %s' % (file_path, e))

def getFileList(folder,extension,includeSubfolders=False):
    MYLOGGER.debug("Entered getFileList")
    if not os.path.isdir(folder):
        if not os.path.isdir(str(folder).replace("/","\\")):
            return []
        else:
            folder=str(folder).replace("/","\\")
    else:
        folder=str(folder).replace("/","\\")

    result=[]

    for filename in glob.glob(os.path.join(folder, f"*.{extension}")):
        result=result+[filename]

    if includeSubfolders==True:
        os.walk(folder)
        for root,dirs,_ in os.walk(folder):
            for d in dirs:
                path_sub = os.path.join(root,d) # this is the current subfolder
                for filename in glob.glob(os.path.join(path_sub, f"*.{extension}")):
                    result=result+[filename]

    return result

def getFileNameFromPath(filepath):
    #Get file name from full path
    #Used to get file name from path in excel
    if os.path.isfile(filepath):
        return os.path.basename(filepath)
    else:
        return None

def getFromParquet(parquetFullName):
    if os.path.isfile(parquetFullName) and os.access(parquetFullName, os.R_OK):
        result = pl.scan_parquet(parquetFullName)
    else:
        result = "No file found"
    return result

def saveToParquet(objecttosave, parquetname):
    try:
        objecttosave.to_parquet(parquetname)
    except:
        objecttosave.write_parquet(parquetname)

def fromGzipParquet(filename,key1,key2=None):
    temp=ast.literal_eval(pd.read_parquet(filename, filters=[("key","=",key1)],columns=["value"],engine='pyarrow')['value'].values[0])
    result={}
    if key2 is None:
        keylist=temp.keys()
        resulttype='dict'
    else:
        if isinstance(key2,str):
            resulttype='value'
            key2=[key2]
        else:
            resulttype='dict'
        keylist=list_intersection(temp.keys(),key2)

    for key in keylist:
        if isinstance(temp[key],dict):
            result[key]=temp[key]
        else:
            result[key]=pl.from_pandas(pd.read_csv(StringIO(temp[key]),sep="\t"))

    if resulttype=='dict':
        return result
    else:
        return result[key2[0]]
