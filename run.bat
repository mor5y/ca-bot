@echo off
cd /d "%~dp0"
for %%F in (tg_session.session tg_session.session-journal tg_session.session-shm tg_session.session-wal positions.json settings.json) do (
    if exist "%%F" del /f /q "%%F"
)
if not exist venv (
    echo Creating virtual environment...
    py -m venv venv
)
call venv\Scripts\activate
pip install -r requirements.txt -q
python tg_ca_bot.py
pause
