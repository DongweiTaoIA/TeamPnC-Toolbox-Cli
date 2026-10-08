@echo off
:: Build a single-file Windows executable for the Azure PIM tray app.
:: Output: dist\AzurePIM.exe  (share this one file with teammates)
::
:: Prerequisites:
::   - Python 3.10+ on PATH
::   - Run from this folder

setlocal
set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

echo === Creating build virtual environment ===
if not exist ".venv-build" (
    python -m venv .venv-build || goto :error
)
call ".venv-build\Scripts\activate.bat" || goto :error

echo === Installing runtime + build dependencies ===
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt || goto :error
python -m pip install pyinstaller==6.10.0 || goto :error

echo === Cleaning previous build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist AzurePIM.spec del /q AzurePIM.spec

echo === Building single-file executable ===
pyinstaller ^
    --onefile ^
    --windowed ^
    --name AzurePIM ^
    --collect-all pystray ^
    --collect-all azure.identity ^
    --collect-submodules msal_extensions ^
    pim_tray.py || goto :error

echo.
echo === Build complete ===
echo Output: %SCRIPT_DIR%dist\AzurePIM.exe
echo Share that single file with your teammates.
echo.
exit /b 0

:error
echo.
echo *** Build failed (exit code %ERRORLEVEL%) ***
exit /b %ERRORLEVEL%
