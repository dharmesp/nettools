@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Tools Manager - Easy Setup

echo.
echo  ==============================================
echo     Tools Manager - Easy Setup
echo  ==============================================
echo.
echo  This gets everything ready for you.
echo  It takes about one minute. Just wait. You do not need to type anything.
echo.

rem ---------- Step 1: find Python (version 3.9 or newer) ----------
echo  [1/3] Looking for Python...
set "PY="
for %%C in ("py -3" "python") do (
    if not defined PY (
        %%~C -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
        if not errorlevel 1 set "PY=%%~C"
    )
)
if not defined PY (
    echo.
    echo  Python is missing. Tools Manager needs it to run.
    echo  Python is a free program. Here is how to get it:
    echo.
    echo    1. A web page will open. Click the big yellow Download button.
    echo    2. Open the file that you downloaded.
    echo    3. VERY IMPORTANT: tick the box that says "Add python.exe to PATH".
    echo    4. Click "Install Now".
    echo    5. When it is finished, double-click install.bat again.
    echo.
    start "" "https://www.python.org/downloads/windows/"
    goto :fail
)
echo        Found it.

rem ---------- Step 2: build the private .venv folder ----------
echo  [2/3] Building a private home for the program...
if not exist ".venv\Scripts\pythonw.exe" (
    %PY% -m venv --clear ".venv"
    if errorlevel 1 (
        echo.
        echo  Sorry, the setup step did not work.
        echo  Please close other programs that use this folder, then try again.
        goto :fail
    )
)
if not exist ".venv\Scripts\pythonw.exe" (
    echo.
    echo  Sorry, something is missing after setup. Please try again.
    goto :fail
)
echo        Done.

rem ---------- Step 3: put the icon on the Desktop ----------
echo  [3/3] Putting a Tools Manager icon on your Desktop...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=$env:TM_DESKTOP; if(-not $d){$d=[Environment]::GetFolderPath('Desktop')}; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($d+'\Tools Manager.lnk'); $s.TargetPath='%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments='\"%~dp0server.py\"'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0static\favicon.ico'; $s.Description='Tools Manager'; $s.Save()" >nul 2>nul
if errorlevel 1 (
    echo        The Desktop icon could not be made. That is OK.
    echo        You can start the program with run.bat instead.
) else (
    echo        Done.
)

echo.
echo  ==============================================
echo     All done!
echo  ==============================================
echo.
echo  To open Tools Manager any time, double-click the
echo  "Tools Manager" icon on your Desktop.
echo.
echo  Opening it for you now...
if not defined TM_NO_START start "" ".venv\Scripts\pythonw.exe" "server.py"
if not defined TM_NO_PAUSE timeout /t 5 >nul
exit /b 0

:fail
echo.
echo  Setup did not finish. Read the message above.
echo  When you have fixed it, double-click install.bat to try again.
if not defined TM_NO_PAUSE pause
exit /b 1
