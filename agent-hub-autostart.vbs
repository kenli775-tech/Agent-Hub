' Agent Hub 开机自启动 + 崩溃自动重启（守护循环，隐藏窗口）
' 停止方法：运行 stop_agent_hub.bat，或手动创建 agent-hub\.norestart 后结束进程
Set shell = CreateObject("WScript.Shell")
Set fso  = CreateObject("Scripting.FileSystemObject")
hubDir = "C:\Users\MVW\Documents\kimi\tasks\2026-09-19\07-51-28-c1e701cd\agent-hub"
flag   = hubDir & "\.norestart"

Do
  If fso.FileExists(flag) Then fso.DeleteFile(flag) : WScript.Quit 0

  Set chk = shell.Exec("cmd /c netstat -ano | findstr :8765")
  If InStr(chk.StdOut.ReadAll(), "LISTENING") = 0 Then
    shell.CurrentDirectory = hubDir
    ' 阻塞运行：uvicorn 崩溃退出后回到循环，5 秒后拉起新实例
    shell.Run """" & hubDir & "\.venv\Scripts\python.exe"" -m uvicorn agent_hub_platform:app --host 127.0.0.1 --port 8765", 0, True
  End If
  WScript.Sleep 5000
Loop
