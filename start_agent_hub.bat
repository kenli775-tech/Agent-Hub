@echo off
rem Agent Hub 一键启动（Windows）— 双击后自动打开 Web 控制台
cd /d %~dp0
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -q -r requirements_agent_hub.txt
)
rem 服务已在跑则直接打开页面
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if errorlevel 1 (
  start "agent-hub" /min .venv\Scripts\python.exe -m uvicorn agent_hub_platform:app --host 127.0.0.1 --port 8765
  :wait
  timeout /t 1 >nul
  netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
  if errorlevel 1 goto wait
)
start "" http://localhost:8765/ui
