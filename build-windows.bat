@echo off
setlocal
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 exit /b 1
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
python -m unittest discover -s tests -v
if errorlevel 1 exit /b 1
pyinstaller --noconfirm --clean --windowed --onedir --name PoE2ArbDesk --collect-all rapidocr --collect-all onnxruntime --add-data "poe2arb\demo.json;poe2arb" --add-data "poe2arb\catalog.json;poe2arb" --add-data "poe2arb\icons;poe2arb\icons" main.py
if errorlevel 1 exit /b 1
dist\PoE2ArbDesk\PoE2ArbDesk.exe --self-test
if errorlevel 1 exit /b 1
echo Built: dist\PoE2ArbDesk\PoE2ArbDesk.exe
