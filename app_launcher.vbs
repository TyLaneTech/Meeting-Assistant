' Meeting Assistant click-to-open launcher.
'
' This is what the Start Menu entry (and any pin made from it) runs. Clicking it:
'   1. if the local server is already up  -> just open the app window (instant);
'   2. if it is not up                    -> start it hidden (tray-only), wait
'      for it to start accepting requests, THEN open the window;
'   3. on a first run (no .venv yet)      -> run launch.bat in a visible console
'      instead, so the one-time install shows its progress and any error.
' No console window otherwise (run under wscript). The window itself is opened
' by the running app (POST /api/window/open), so the installed-PWA / chromeless
' window / default-browser choice lives in core/browser.py alone. The Chrome
' fallback at the bottom only runs against a server too old to have that route.
Option Explicit

Dim shell, fso, projDir, port, statusUrl, appUrl, i, chrome
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

projDir   = fso.GetParentFolderName(WScript.ScriptFullName)
port      = EnvPort(projDir)
statusUrl = "http://localhost:" & port & "/api/status"
appUrl    = "http://localhost:" & port & "/"

' PORT from .env, the same file the app reads; 6969 when unset.
Function EnvPort(dir)
  Dim ts, line
  EnvPort = "6969"
  On Error Resume Next
  If fso.FileExists(dir & "\.env") Then
    Set ts = fso.OpenTextFile(dir & "\.env", 1)
    Do Until ts.AtEndOfStream
      line = Trim(ts.ReadLine)
      If Left(line, 5) = "PORT=" Then EnvPort = Trim(Replace(Mid(line, 6), Chr(34), ""))
    Loop
    ts.Close
  End If
  On Error GoTo 0
  If EnvPort = "" Then EnvPort = "6969"
End Function

Function ServerUp()
  Dim http
  ServerUp = False
  On Error Resume Next
  Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  http.setTimeouts 1500, 1500, 2000, 2000
  http.open "GET", statusUrl, False
  http.send
  If Err.Number = 0 Then
    If http.status >= 200 And http.status < 500 Then ServerUp = True
  End If
  On Error GoTo 0
End Function

' Ask the running app to open (or focus) its window. False on an older server
' without the route, or any error.
Function OpenWindowViaApp()
  Dim http
  OpenWindowViaApp = False
  On Error Resume Next
  Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  http.setTimeouts 2000, 2000, 5000, 5000
  http.open "POST", "http://localhost:" & port & "/api/window/open", False
  http.setRequestHeader "Content-Type", "application/json"
  http.send "{}"
  If Err.Number = 0 Then
    If http.status = 200 Then OpenWindowViaApp = True
  End If
  On Error GoTo 0
End Function

' First run: no environment yet. Run the installer in a console so its progress
' (and any error) is visible; launch.py opens nothing on its own.
If Not fso.FileExists(projDir & "\.venv\Scripts\python.exe") Then
  shell.CurrentDirectory = projDir
  shell.Run "cmd /c """ & projDir & "\launch.bat""", 1, False
  WScript.Quit
End If

' 1) Ensure the server is up.
If Not ServerUp() Then
  shell.CurrentDirectory = projDir
  ' launch_hidden.vbs -> launch.bat --hidden -> launch.py: starts the app
  ' tray-only. Fire and forget.
  shell.Run "wscript.exe """ & projDir & "\launch_hidden.vbs""", 0, False
  ' Wait up to three minutes: an update installs its new packages first. Flask
  ' answers /api/status within a few seconds of the app starting, well before
  ' the models finish loading, so the window opens fast and the app shows its
  ' own loading state.
  For i = 1 To 180
    WScript.Sleep 1000
    If ServerUp() Then Exit For
  Next
  If Not ServerUp() Then
    MsgBox "Meeting Assistant is taking longer than usual to start. If it does not appear in the tray shortly, the newest launch-startup log in storage\logs has the details.", vbInformation, "Meeting Assistant"
    WScript.Quit
  End If
End If

' 2) Open the app window. The app decides how (installed PWA, chromeless app
'    window, default browser). Older servers lack the route: fall back to a
'    chromeless Chrome window, then the default browser.
If Not OpenWindowViaApp() Then
  chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
  If Not fso.FileExists(chrome) Then chrome = "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
  If fso.FileExists(chrome) Then
    shell.Run """" & chrome & """ --profile-directory=Default --app=" & appUrl & " --window-size=1360,900", 1, False
  Else
    shell.Run appUrl, 1, False
  End If
End If
