@echo off
rem Run the agent in the foreground (for testing). Use install_windows.ps1 for auto start.
cd /d %~dp0
chcp 65001 >nul
.venv\Scripts\python.exe -m bridge_agent --config config.yaml
pause
