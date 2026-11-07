' ==============================================================================
' SOL Media Studio - Silent Windows Launcher
' Launches Media Studio and keeps WSL alive in background with ZERO popup windows.
' ==============================================================================
Option Explicit

Dim objShell, strWslDistro, strUrl, objHttp, isAlive, i

Set objShell = CreateObject("WScript.Shell")
strWslDistro = "Ubuntu-24.04"
strUrl = "http://127.0.0.1:8080/"

' 1. Check if keepalive process is already running in WSL; if not, launch hidden sleep infinity
' Window style 0 = hidden, False = run asynchronously without blocking
objShell.Run "wsl.exe -d " & strWslDistro & " -- bash -c ""pgrep -f '^sleep infinity$' >/dev/null || exec sleep infinity""", 0, False

' 2. Wait up to 5 seconds for media-api to respond to socket connection
isAlive = False
For i = 1 To 10
    On Error Resume Next
    Set objHttp = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    objHttp.setTimeouts 500, 500, 1000, 1000
    objHttp.Open "GET", "http://127.0.0.1:8080/api/v1/health", False
    objHttp.Send
    If Err.Number = 0 Then
        If objHttp.Status = 200 Then
            isAlive = True
            On Error GoTo 0
            Exit For
        End If
    End If
    On Error GoTo 0
    WScript.Sleep 500
Next

' 3. Open default browser to Media Studio GUI
objShell.Run strUrl, 1, False

Set objHttp = Nothing
Set objShell = Nothing
