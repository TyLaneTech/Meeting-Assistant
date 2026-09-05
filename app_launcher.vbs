' Meeting Assistant click-to-open launcher.
'
' This is what the app icon (taskbar / desktop / Start Menu) runs. Clicking it:
'   1. if the local server is already up  -> just open the app window (instant);
'   2. if it is not up                    -> start it hidden (tray-only), wait
'      for it to start accepting requests, THEN open the window.
' So the app "turns on when you click it" and you never land on the browser's
' localhost-refused page. No console window (run under wscript).
Option Explicit

Dim shell, fso, projDir, statusUrl, i, chromeProxy, chrome, pwaArgs, appUrl
Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

projDir   = fso.GetParentFolderName(WScript.ScriptFullName)
statusUrl = "http://localhost:6969/api/status"
appUrl    = "http://localhost:6969/"

' The installed PWA (matches the existing taskbar pin so the window docks there).
chromeProxy = "C:\Program Files\Google\Chrome\Application\chrome_proxy.exe"
chrome      = "C:\Program Files\Google\Chrome\Application\chrome.exe"
If Not fso.FileExists(chrome) Then chrome = "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
pwaArgs     = "--profile-directory=Default --app-id=pmaddcbhfddcgdflmbmpneamdilppkbn"

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

' 1) Ensure the server is up.
If Not ServerUp() Then
  shell.CurrentDirectory = projDir
  ' launch_hidden.vbs -> launch.bat -> launch.py: starts the app tray-only and
  ' respawns the in-process freeze watchdog. Fire and forget.
  shell.Run "wscript.exe """ & projDir & "\launch_hidden.vbs""", 0, False
  ' Wait (up to ~60s) for Flask to bind. It answers /api/status within a few
  ' seconds, well before the transcription models finish loading, so the window
  ' opens fast and the app shows its own loading state.
  For i = 1 To 60
    WScript.Sleep 1000
    If ServerUp() Then Exit For
  Next
End If

' 2) Open the app window. Prefer the installed PWA (clean taskbar grouping),
'    then a chromeless --app window, then the default browser.
If fso.FileExists(chromeProxy) Then
  shell.Run """" & chromeProxy & """ " & pwaArgs, 1, False
ElseIf fso.FileExists(chrome) Then
  shell.Run """" & chrome & """ --profile-directory=Default --app=" & appUrl & " --window-size=1360,900", 1, False
Else
  shell.Run appUrl, 1, False
End If
