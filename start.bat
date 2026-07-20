@echo off
chcp 65001 >nul 2>&1
setlocal

REM ============================================================
REM  LxOcConnector 一键启动脚本 (Windows)
REM  1. 确保 .env 已配置
REM  2. 自动启动 opencode serve (端口 4096)
REM  3. 自动启动蓝信桥接
REM ============================================================

cd /d "%~dp0"

if not exist ".env" (
    echo [!] 未找到 .env，正在从 .env.example 创建...
    copy .env.example .env >nul
    echo.
    echo [!] 请编辑 .env 填写蓝信凭证后重新运行本脚本。
    echo     获取方式：蓝信桌面端 → 通讯录 → 晁能机器人 → 个人机器人 → ℹ️ 图标
    echo.
    notepad .env
    goto :end
)

REM ---- 从 .env 读取关键配置 ----
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    set "%%a=%%b"
) 2>nul

REM ---- 启动 opencode serve（如果端口没被占用）----
set "OC_PORT=4096"
netstat -ano | findstr ":%OC_PORT% " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo [*] 启动 opencode serve (端口 %OC_PORT%)...
    if defined OPENCODE_SERVER_PASSWORD (
        start /min "opencode-serve" cmd /c "set OPENCODE_SERVER_PASSWORD=%OPENCODE_SERVER_PASSWORD% && opencode serve --port %OC_PORT% --hostname 127.0.0.1 >nul 2>&1"
    ) else (
        start /min "opencode-serve" cmd /c "opencode serve --port %OC_PORT% --hostname 127.0.0.1 >nul 2>&1"
    )
    echo [*] 等待 opencode serve 启动...
    timeout /t 5 /nobreak >nul
) else (
    echo [*] opencode serve 已在端口 %OC_PORT% 运行，跳过启动
)

REM ---- 启动蓝信桥接 ----
echo [*] 启动蓝信桥接服务...
python -u main.py

:end
endlocal
pause
