@echo off
rem 停止 Agent Hub：告知守护进程不再重启，然后结束服务和守护脚本
setlocal
set "HUB_DIR=C:\Users\MVW\Agent-Hub"
type nul > "%HUB_DIR%\.norestart"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R ":8765 .*LISTENING"') do taskkill /PID %%p /F >nul 2>&1
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='wscript.exe'\" | Where-Object { $_.CommandLine -like '*agent-hub-autostart*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
echo Agent Hub 已停止（守护进程不会再拉起）。
endlocal
