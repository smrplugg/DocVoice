@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Установка PyTorch (CPU, ~200 МБ) для движка Silero...
.venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cpu || (echo Ошибка & pause & exit /b 1)
echo Готово. Модель Silero скачайте в программе: Голоса - вкладка Silero.
pause
