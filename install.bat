@echo off
echo ============================================
echo  MLB Analyzer -- Installation
echo ============================================

:: Verifier Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python n'est pas installe ou pas dans le PATH.
    pause
    exit /b 1
)

:: Creer venv si absent
if not exist "venv\" (
    echo Creation de l'environnement virtuel...
    python -m venv venv
)

:: Activer venv
call venv\Scripts\activate.bat

:: Installer dependances
echo Installation des dependances...
pip install --upgrade pip
pip install -r requirements.txt

:: Installer Playwright browsers
echo Installation de Playwright Chromium...
playwright install chromium

echo.
echo ============================================
echo  Installation terminee !
echo  Lancez start.bat pour demarrer l'application.
echo ============================================
pause
