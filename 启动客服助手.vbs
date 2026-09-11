Set fso = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("WScript.Shell")
sDir = fso.GetParentFolderName(WScript.ScriptFullName)
venvPy = sDir & "\.venv\Scripts\pythonw.exe"
If fso.FileExists(venvPy) Then
    exe = venvPy
Else
    exe = "pythonw.exe"
End If
ws.Run """" & exe & """ """ & sDir & "\tray.py""", 0, False