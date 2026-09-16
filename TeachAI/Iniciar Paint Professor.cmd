@echo off
cd /d "%~dp0"
"%~dp0.paint-professor-env\Scripts\python.exe" "%~dp0paint_professor.py" %*
if errorlevel 1 pause
