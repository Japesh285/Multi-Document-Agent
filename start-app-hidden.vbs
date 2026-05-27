' start-app-hidden.vbs — launches start-app.bat with no visible console window.
' Use this for the desktop shortcut once setup has been completed once and you
' know things work. For debugging, run start-app.bat directly instead.

Set sh    = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target    = scriptDir & "\start-app.bat"

If Not fso.FileExists(target) Then
  MsgBox "start-app.bat not found in " & scriptDir, vbCritical, "Spreadsheet Agent"
  WScript.Quit 1
End If

' 0 = hide window, False = don't wait for completion
sh.Run """" & target & """", 0, False
