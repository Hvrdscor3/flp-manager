@echo off
rem Arrastra un proyecto .flp encima de este archivo.
if "%~1"=="" (echo Arrastra tu proyecto .flp encima de este archivo. & pause & exit /b)
"%~dp0.venv\Scripts\python.exe" -I "%~dp0flp_ordenar.py" "%~1"
pause
