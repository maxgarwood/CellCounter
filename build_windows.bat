@echo off
rem Builds the Windows version of Cell Counter. Double-click this file.
rem Needs Python 3.12 from python.org and an internet connection (first time only).
rem The result: dist\Cell Counter\Cell Counter.exe, and "Cell Counter for Windows.zip" to share.
setlocal
cd /d "%~dp0"

echo.
echo === Cell Counter: building the Windows app ===
echo.

rem 1. Find Python 3.12
py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo Python 3.12 was not found.
    echo Install it from https://www.python.org/downloads/ ^(Windows installer, 64-bit^),
    echo then run this file again.
    goto :fail
)

rem 2. A private Python environment just for building (made once, then reused)
if not exist "build-env\Scripts\python.exe" (
    echo Creating the build environment...
    py -3.12 -m venv build-env || goto :fail
)
echo Installing the libraries the app needs ^(the first time takes a few minutes^)...
"build-env\Scripts\python.exe" -m pip install --upgrade pip --quiet || goto :fail
"build-env\Scripts\python.exe" -m pip install -r requirements.txt --quiet || goto :fail

rem 3. Package the app
echo Packaging the app ^(about 1-3 minutes^)...
"build-env\Scripts\pyinstaller.exe" --noconfirm CellCounter.spec || goto :fail

rem 4. Zip it for sharing
echo Making "Cell Counter for Windows.zip"...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\Cell Counter' -DestinationPath 'Cell Counter for Windows.zip' -Force" || goto :fail

rem 5. Check it works (a window with the results appears)
echo Running the self-test...
"dist\Cell Counter\Cell Counter.exe" --self-test

echo.
echo === Done ===
echo The app is in:      dist\Cell Counter\Cell Counter.exe
echo To share it, send:  Cell Counter for Windows.zip
echo.
pause
exit /b 0

:fail
echo.
echo === The build stopped because of an error (see the messages above). ===
pause
exit /b 1
