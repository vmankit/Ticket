@echo off
REM ========================================
REM Fix Broken Virtual Environment
REM ========================================

echo.
echo ========================================
echo Fixing Ticket Project Environment
echo ========================================
echo.

echo Step 1: Deactivating current environment...
call deactivate 2>nul

echo Step 2: Removing corrupted .venv-2...
if exist .venv-2 (
    rmdir /s /q .venv-2
    echo ✓ Removed .venv-2
) else (
    echo ✓ .venv-2 already removed
)

echo.
echo Step 3: Creating fresh virtual environment...
C:\Python313\python.exe -m venv .venv-2
if %errorlevel% neq 0 (
    echo ❌ Failed to create virtual environment
    echo Please ensure Python 3.13 is installed at C:\Python313\
    pause
    exit /b 1
)
echo ✓ Created .venv-2

echo.
echo Step 4: Activating virtual environment...
call .venv-2\Scripts\activate.bat
echo ✓ Activated .venv-2

echo.
echo Step 5: Upgrading pip...
python -m pip install --upgrade pip
echo ✓ Pip upgraded

echo.
echo Step 6: Installing dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo ❌ Failed to install dependencies
    pause
    exit /b 1
)
echo ✓ Dependencies installed

echo.
echo Step 7: Verifying installation...
python -c "import flask; import reportlab; import openpyxl; import qrcode; import pdfplumber; print('✓ All dependencies verified')"
if %errorlevel% neq 0 (
    echo ❌ Dependency verification failed
    pause
    exit /b 1
)

echo.
echo Step 8: Testing Flask app import...
python -c "from app import app; print('✓ Flask app imports successfully')"
if %errorlevel% neq 0 (
    echo ❌ Flask app import failed
    pause
    exit /b 1
)

echo.
echo ========================================
echo ✓ Environment Fixed Successfully!
echo ========================================
echo.
echo You can now run the application with:
echo   python app.py
echo.
echo Or start it automatically? (Y/N)
set /p START_APP=
if /i "%START_APP%"=="Y" (
    echo.
    echo Starting application...
    python app.py
)

pause
