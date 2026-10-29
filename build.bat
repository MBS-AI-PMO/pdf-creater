@echo off
rem Builds dist\ImageToPDF.exe (single file, no console window).
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install pillow img2pdf pyinstaller || goto :error
)

.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed ^
    --name ImageToPDF image_to_pdf.py || goto :error

echo.
echo Done: %~dp0dist\ImageToPDF.exe
exit /b 0

:error
echo Build failed.
exit /b 1
