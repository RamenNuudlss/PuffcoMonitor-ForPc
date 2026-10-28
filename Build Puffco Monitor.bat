@echo off
rem Builds "Puffco Monitor.exe" from this folder's source.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed - get it from python.org and tick "Add to PATH". & pause & exit /b)
echo Installing build tools...
python -m pip install --quiet --upgrade -r requirements.txt pyinstaller || (pause & exit /b)
echo Building Puffco Monitor.exe - this takes a minute or two...
python -m PyInstaller --noconfirm --clean puffco_monitor.spec || (pause & exit /b)
copy /y "dist\Puffco Monitor.exe" "Puffco Monitor.exe" >nul
rmdir /s /q build dist 2>nul
echo.
echo Done - "Puffco Monitor.exe" is ready in this folder.
pause
