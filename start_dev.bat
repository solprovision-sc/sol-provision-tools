@echo off
call C:\ProgramData\anaconda3\Scripts\activate.bat
call conda activate solprov
cd /d "%~dp0app"
python server.py --db ..\dataforge.db --port 5000 --debug
pause