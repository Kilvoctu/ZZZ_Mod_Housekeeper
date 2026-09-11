@echo off
setlocal
cd /d "%~dp0"

set "VPYTHON=venv\Scripts\python.exe"

if not exist "%VPYTHON%" (
    echo [ERROR] venv\Scripts\python.exe not found.
    exit /b 1
)

if not exist "icon.ico" (
    echo [ERROR] icon.ico not found in the project root.
    exit /b 1
)

"%VPYTHON%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo == Installing PyInstaller ==
    "%VPYTHON%" -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] PyInstaller install failed.
        exit /b 1
    )
)

echo == Building ZZZHashFix.exe ==
"%VPYTHON%" -m PyInstaller --noconfirm --clean ZZZHashFix.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    exit /b 1
)

if not exist "dist\ZZZHashFix.exe" (
    echo [ERROR] Build did not produce dist\ZZZHashFix.exe
    exit /b 1
)

echo == Copying to project root... ==
copy /Y "dist\ZZZHashFix.exe" "ZZZHashFix.exe" >nul
if errorlevel 1 (
    echo [ERROR] Copy to project root failed.
    exit /b 1
)

echo == Cleaning build artifacts... ==
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist __pycache__ rmdir /s /q __pycache__

echo Done. ZZZHashFix.exe rebuilt at project root.
exit /b 0
