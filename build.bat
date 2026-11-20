@echo off
REM Baut KlarBlock.exe (eine einzelne Datei) mit PyInstaller.
REM Voraussetzung: Python 3.11+ von python.org (mit tcl/tk), Haken bei "Add to PATH".
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --noconsole --uac-admin --name KlarBlock klarblock.py
echo.
echo Fertig: dist\KlarBlock.exe
pause
