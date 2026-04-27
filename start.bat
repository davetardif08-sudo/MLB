@echo off
echo ============================================
echo  MLB Analyzer -- Demarrage
echo ============================================

cd /d "%~dp0"
call venv\Scripts\activate.bat

echo Demarrage du serveur Flask sur http://localhost:5001 ...
start "" "http://localhost:5003"
python app.py

pause
