@echo off
:: Azure PIM Tray – starts with no console window
:: Place in your Startup folder for auto-start on login

set SCRIPT_DIR=%~dp0
pythonw "%SCRIPT_DIR%pim_tray.py" %*
