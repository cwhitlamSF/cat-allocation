# modelkit/lists.py
"""
List, comma-string and small dict helpers.
Set-style operations return sorted lists.
"""

import polars as pl
from .common import get_logger

MYLOGGER = get_logger('modelkit.lists')


def list_intersection(list1, list2):
    #returns sorted instersection of two lists
    return sorted(list(set(list1) & set(list2)))

def list_difference(list1, list2):
    #returns sorted difference between two lists
    return sorted(list(set(list1).symmetric_difference(set(list2))))

def list_dropped(list1,list2):
    #returns sorted list1 with items in list2 removed
    return sorted(list(set(list1)-set(list2)))

def list_added(list1,list2):
    #returns sorted list1 with items in list2 added
    return sorted(list(set(list2+list1)))

def uniqueList(list_in):
    unique_list=[]
    for x in list_in:
        if x not in unique_list:
            unique_list.append(x)
    return unique_list

def list_intersection_sls(str1,list2)->str:
    #list1 as string, list2 as list, return string
    #returns sorted instersection of two lists
    if (str1==None)|(list2==[]):
        result= pl.lit("")   #used to return None
    else:
        list1=str1.split(",")
        list1=[x.strip() for x in list1]
        result=sorted(list(set(list1).intersection(set(list2))))
        result=",".join(result)
    return result

def list_intersection_sll(str1,list2):
    #list1 as string, list2 as list, return string
    #returns sorted instersection of two lists
    if (str1==None)|(list2==[]):
        return pl.lit([])
    else:
        list1=str1.split(",")
        list1=[x.strip() for x in list1]
        result=sorted(list(set(list1).intersection(set(list2))))
        return result

def list_intersection_sss(str1,str2)->str:
    #list1 as string, list2 as string, return string
    #returns sorted instersection of two lists
    if (str1==None)|(str2==None):
        return pl.lit("")
    else:
        list1=str1.split(",")
        list1=[x.strip() for x in list1]
        list2=str2.split(",")
        list2=[x.strip() for x in list2]
        result=sorted(list(set(list1).intersection(set(list2))))
        return ",".join(result)

def list_intersection_ssl(str1,str2):
    #list1 as string, list2 as string, return list
    #returns sorted instersection of two lists
    if (str1==None)|(str2==None):
        return pl.lit([])
    else:
        list1=str1.split(",")
        list1=[x.strip() for x in list1]
        list2=str2.split(",")
        list2=[x.strip() for x in list2]
        result=sorted(list(set(list1).intersection(set(list2))))
        return result

def list_to_string(list1)->str:
    #list1 as list, return string
    #returns string of list elements
    if list1==None:
        return ""
    else:
        return ",".join(sorted(list(set(list1))))

def flagValueIfNotNullNorInList(str1,list2):
    if str1==None:
        return str1
    elif str1 in list2:
        return str1
    else:
        return "Flag"

def getDictValue(key,dictname):
    try:
        return dictname[key]
    except:
        return "key not found"

def concatenateDictListVals(checkdict):
    MYLOGGER.debug('Enter dictValsToList')
    result=[]
    for v in checkdict.values():
        if isinstance(v,list):
            for x in v:
                result.append(x)
    return result

def keysAreDicts(checkdict):
    MYLOGGER.debug('Enter keysAreDicts')
    if isinstance(checkdict,dict):
        return [isinstance(x,dict) for x in checkdict.values()]
    else:
        return [False]

def create_consistent_groups(initial):
    from collections import defaultdict

    # Step 1: Track co-occurrence consistency
    co_occurrence = defaultdict(set)
    element_occurrences = defaultdict(list)

    # Build occurrences mapping: track which sublists contain each element
    for idx, sublist in enumerate(initial):
        for element in sublist:
            element_occurrences[element].append(idx)

    # Build co-occurrence map
    for sublist in initial:
        for i in range(len(sublist)):
            for j in range(i + 1, len(sublist)):
                co_occurrence[sublist[i]].add(sublist[j])
                co_occurrence[sublist[j]].add(sublist[i])

    # Step 2: Group elements that consistently appear together
    def is_consistent(element, other):
        # Check if `other` appears in all sublists where `element` appears
        element_sublists = set(element_occurrences[element])
        other_sublists = set(element_occurrences[other])
        return element_sublists == other_sublists

    visited = set()
    groups = []

    for element in co_occurrence:
        if element not in visited:
            # Build a consistent group for this element
            group = {element}
            for other in co_occurrence[element]:
                if other not in visited and is_consistent(element, other):
                    group.add(other)
            groups.append(list(group))
            visited.update(group)

    # Step 3: Handle elements that never co-occur with others
    for element in element_occurrences:
        if element not in visited:
            groups.append([element])

    return groups

    # Example usage
    #initial = [['a', 'b', 'c'], ['a', 'b', 'd','e','f'], ['a','b'], ['d', 'e', 'c']]
    #consistent_groups = create_consistent_groups(initial)
    #result=[['b', 'a'], ['c'], ['e', 'd'], ['f']]
