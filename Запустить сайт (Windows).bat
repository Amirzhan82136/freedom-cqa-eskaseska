@echo off
rem Двойной клик: запускает сайт на Windows и открывает его в браузере.
rem Чтобы остановить сайт, закройте это окно.
chcp 65001 >nul
cd /d "%~dp0"
python server.py
pause
