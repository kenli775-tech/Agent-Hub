@echo off
rem ============================================================
rem  review-diff.bat — 一条命令完成多AI代码评审
rem  用法: 在任意 git 仓库目录执行  review-diff.bat
rem  效果: 自动启动 Agent Hub(如未运行) -> hermes 提交当前
rem        git diff -> 轮询 -> 输出裁决结论
rem ============================================================
setlocal
set "HUB_DIR=C:\Users\MVW\Agent-Hub"
set "HERMES=C:\Users\MVW\AppData\Local\hermes\bin\hermes.exe"

rem --- 1. 确保 Agent Hub 服务在跑 --------------------------------
netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
if errorlevel 1 (
    echo [review-diff] 启动 Agent Hub 服务...
    if not exist "%HUB_DIR%\.env" (
        echo [review-diff] 未找到 %HUB_DIR%\.env，无法确定模型密钥，请先配置。
        exit /b 1
    )
    start "agent-hub" /min /D "%HUB_DIR%" "%HUB_DIR%\.venv\Scripts\python.exe" -m uvicorn agent_hub_platform:app --host 127.0.0.1 --port 8765
    rem 等待服务就绪
    :wait
    timeout /t 2 >nul
    netstat -ano | findstr /R /C:":8765 .*LISTENING" >nul
    if errorlevel 1 goto wait
)

rem --- 2. 调用 hermes 完成评审 -----------------------------------
"%HERMES%" chat -s review-diff -q "请按 review-diff 技能流程，评审当前目录的 git diff，并把最终裁决结论完整告诉我。" <nul
endlocal
