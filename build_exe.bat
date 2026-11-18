@echo off
rem Compila FLP-Manager.exe en la carpeta dist\
cd /d "%~dp0"
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name FLP-Manager ^
  --icon manager\icon.ico ^
  --paths . --paths manager ^
  --add-data "manager\ui.html;." ^
  --add-data "device_AutoOrganizer.py;." ^
  --hidden-import flpraw --hidden-import flp_ordenar ^
  manager\app.py
