' Meeting Assistant tray-only launcher.
'
' Starts the app with NO console window and NO taskbar button, so the system
' tray icon is the only UI. It runs launch.bat hidden and redirects the startup
' output to storage\logs\launch-startup-<stamp>.log. launch.py in turn starts
' the external freeze watchdog and then the app, so every autostart is
' supervised.
'
' One log file per launch, never a fixed name: cmd opens a redirect target
' without write sharing, so a fixed name could not be reopened while the chain
' being replaced (an in-app restart or update) still held it, and the second
' launcher died silently before running anything (2026-09-05). Logs older than
' a week are pruned here so the folder never grows past a handful of files.
Option Explicit

Dim shell, fso, projDir, logDir, bat, logFile, cmd, q, stamp, f
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projDir = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FolderExists(projDir & "\storage") Then fso.CreateFolder(projDir & "\storage")
logDir = projDir & "\storage\logs"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

On Error Resume Next
For Each f In fso.GetFolder(logDir).Files
  If LCase(Left(f.Name, 15)) = "launch-startup-" And LCase(Right(f.Name, 4)) = ".log" Then
    If DateDiff("d", f.DateLastModified, Now) > 7 Then f.Delete True
  End If
Next
On Error GoTo 0

Randomize
stamp = Year(Now) & Right("0" & Month(Now), 2) & Right("0" & Day(Now), 2) & "-" & _
        Right("0" & Hour(Now), 2) & Right("0" & Minute(Now), 2) & Right("0" & Second(Now), 2) & _
        "-" & Int(Rnd * 9000 + 1000)

bat = projDir & "\launch.bat"
logFile = logDir & "\launch-startup-" & stamp & ".log"

' Quote every path so an install directory containing spaces still works. cmd /c
' strips the outermost pair of quotes, leaving each path individually quoted.
' Window style 0 = hidden; False = do not wait (the launcher returns immediately
' and the app runs on in the tray).
q = Chr(34)
shell.CurrentDirectory = projDir
cmd = "cmd /c " & q & q & bat & q & " > " & q & logFile & q & " 2>&1" & q
shell.Run cmd, 0, False
