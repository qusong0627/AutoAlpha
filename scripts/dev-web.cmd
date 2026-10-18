@echo off
REM Factor-mining slim frontend dev server.
REM   Modules: alpha-research (mining / factor pool / backtest) + factor-research
REM   Usage: scripts\dev-web.cmd
REM Keep this file ASCII-only and CRLF: cmd.exe mis-decodes UTF-8 Chinese
REM comments under the default CP936 codepage and then runs garbage as commands.
setlocal
cd /d "%~dp0..\web"

set npm_config_registry=https://registry.npmmirror.com/
REM Backend: standalone factor-mining service (8001)
set VITE_API_URL=http://localhost:8001
set VITE_API_BASE_URL=http://localhost:8001
set VITE_PORT=3000
REM Internal identity headers are NOT hardcoded here anymore. The vite proxy only
REM injects them when INTERNAL_CALL_SECRET is present in the environment; with
REM AUTH_DISABLED=1 (the dev default) no headers are needed at all. To exercise
REM the auth-enabled path, export INTERNAL_CALL_SECRET before running this script.
set DEV_USER_ID=10000001

if not exist node_modules (
  echo [1/2] node_modules missing, running npm ci ...
  call "%ProgramFiles%\nodejs\npm.cmd" ci --no-audit --no-fund || goto :fail
)

echo [2/2] starting vite -^> http://localhost:3000
call "%ProgramFiles%\nodejs\npm.cmd" run dev
goto :eof

:fail
echo npm ci failed, check network or registry config
exit /b 1
