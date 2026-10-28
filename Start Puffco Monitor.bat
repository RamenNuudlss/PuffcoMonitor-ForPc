@echo off
rem Runs Puffco Monitor from source (the exe does the same thing without needing Python).
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Use "Puffco Monitor.exe" instead, or get Python from python.org. & pause & exit /b)
python -c "import bleak, webview, pystray, PIL, numpy" 2>nul || python -m pip install --quiet bleak pywebview pystray pillow numpy
start "" pythonw puffco_monitor.py
