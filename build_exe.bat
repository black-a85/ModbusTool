@echo off
REM Build ModbusTool into a single Windows .exe using the project venv.
REM Output: dist\ModbusTool.exe
REM
REM The AI libraries (pdfplumber, pypdfium2, anthropic, openai) are imported
REM lazily at runtime, so PyInstaller can't discover them by static analysis --
REM we force them in with --collect-all. pypdfium2 ships a native DLL and
REM certifi/pdfminer ship data files, which --collect-all also pulls in.

cd /d "%~dp0"

".venv\Scripts\python.exe" -m pip install --upgrade pyinstaller

".venv\Scripts\pyinstaller.exe" --noconfirm --clean --onefile --windowed ^
  --name ModbusTool ^
  --collect-all pymodbus ^
  --collect-all serial ^
  --collect-all pdfplumber ^
  --collect-all pdfminer ^
  --collect-all pypdfium2 ^
  --collect-all pypdf ^
  --collect-all anthropic ^
  --collect-all openai ^
  --collect-all pydantic ^
  --collect-all pydantic_core ^
  --collect-all httpx ^
  --collect-all httpcore ^
  --collect-all certifi ^
  --collect-all jiter ^
  --collect-all distro ^
  main.py

echo.
echo Done. The executable is at: dist\ModbusTool.exe
