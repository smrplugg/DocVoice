@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка DocVoice ===
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% --version || (echo Не найден Python. Установите Python 3.10-3.12 с python.org, отметив "Add to PATH". & pause & exit /b 1)
if not exist .venv (%PY% -m venv .venv || goto err)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || goto err
echo.
echo Готово! Запуск - run.bat
echo Голоса скачиваются в программе: кнопка "Голоса".
echo Для движка Silero дополнительно запустите install_silero.bat
pause
exit /b 0
:err
echo ОШИБКА установки, см. сообщения выше.
pause
exit /b 1
