Attribute VB_Name = "CCARun"
Option Explicit

' Run button for the Cat Cost Allocation tool.
' Starts the CCA exe on this workbook; the exe reads the tables from the open workbook
' (xlwings Book.caller), runs Prepare Data or Allocate and writes the results. Warnings go to the
' Data Issues sheet; results to the Layer Summary, LOB Summary, Diagnostics and Output Log sheets.
'
' Needs the xlwings VBA module (the one with RunFrozenPython) in this workbook: export it from
' the RSA workbook and import it here, or add it with "xlwings quickstart --standalone".
' The exe path is the named cell _executablepath on the Model Path sheet.

Sub RunModel()
    Dim myexe As String
    Dim calcsetting As Long

    On Error Resume Next
    myexe = ThisWorkbook.Names("_executablepath").RefersToRange.Value2
    On Error GoTo 0
    If Len(myexe) = 0 Or Dir(myexe) = "" Then
        MsgBox "Can't find the CCA program at:" & vbCrLf & myexe & vbCrLf & vbCrLf & _
               "Update the path on the Model Path sheet.", vbExclamation, "CCA"
        Exit Sub
    End If

    calcsetting = Application.Calculation
    Application.Calculation = xlCalculationManual
    On Error GoTo RunFailed
    RunFrozenPython myexe, "main"
    Application.Calculation = calcsetting
    Exit Sub

RunFailed:
    Application.Calculation = calcsetting
    MsgBox "The CCA run could not start:" & vbCrLf & Err.Description, vbCritical, "CCA"
End Sub
