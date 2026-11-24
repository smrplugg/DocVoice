@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Сборка папки dist\DocVoice с DocVoice.exe.
rem Если в .venv установлен PyTorch (install_silero.bat) - Silero войдёт в сборку (~1 ГБ),
rem иначе сборка будет лёгкой, только Piper и голоса Windows.
if not exist .venv\Scripts\python.exe (echo Сначала запустите install.bat & pause & exit /b 1)
.venv\Scripts\python -m pip install pyinstaller || goto err

set "EXTRA=--exclude-module torch"
.venv\Scripts\python -c "import torch" >nul 2>nul && set "EXTRA=--hidden-import torch.package --hidden-import torch.jit._script --hidden-import wave --hidden-import gzip --hidden-import unicodedata --hidden-import xml.etree.ElementTree --hidden-import json"
echo %EXTRA% | find "exclude" >nul && (echo PyTorch не найден - сборка БЕЗ Silero.) || (echo PyTorch найден - сборка С Silero, это займёт несколько минут.)

rem PyInstaller стирает dist\DocVoice целиком. Голоса и модели, скачанные из собранной программы,
rem сначала забираем в data проекта: только недостающие файлы, ничего не заменяя, без настроек и журналов.
if exist dist\DocVoice\data (
  echo Сохраняю голоса и модели, скачанные в собранной программе ...
  robocopy dist\DocVoice\data data /E /XC /XN /XO /NFL /NDL /NJH /NJS /XD logs espeak-ng-data espeak-ng-data.tmp /XF settings*.json *.log *.tmp *.part >nul
)

rem Через python -m, а не pyinstaller.exe: в .exe-обёртках venv записан путь к python.exe,
rem и после переноса папки программы они перестают запускаться.
rem pymupdf.layout / pymupdf4llm не нужны: PyMuPDF 2 при импорте сам грузит их ONNX-модель,
rem а её файлы в сборку не попадают - открытие любого PDF падало с FileNotFoundError.
rem --noupx: сжатие UPX - частая причина ложных тревог антивирусов и сбоев загрузки DLL.
.venv\Scripts\python -m PyInstaller --noconfirm --clean --windowed --noupx --name DocVoice --collect-all piper --collect-data num2words --hidden-import win32com.client --hidden-import pythoncom --exclude-module pymupdf.layout --exclude-module pymupdf4llm --hidden-import tokenizers --hidden-import razdel --hidden-import docvoice.support_config --add-data "docvoice\accent\LICENSE-ruaccent.txt;docvoice\accent" %EXTRA% run.py || goto err

if exist data (
  echo Копирую голоса и модели из data в dist\DocVoice\data ...
  rem Без ваших личных файлов: настроек (там недавние книги), журналов и временных копий.
  robocopy data dist\DocVoice\data /E /NFL /NDL /NJH /NJS /XD logs espeak-ng-data espeak-ng-data.tmp "проверка-ёжик" /XF settings*.json *.log *.tmp *.part >nul
)
echo Копирую руководства ...
copy /Y "docs\Руководство.txt" dist\DocVoice\ >nul
copy /Y "docs\Инструкция.txt" dist\DocVoice\ >nul

echo Самопроверка собранной программы (до минуты) ...
start "" /wait dist\DocVoice\DocVoice.exe --selftest
echo.
type dist\DocVoice\data\logs\selftest.txt
rem журнал проверки не должен уехать к пользователям
if exist dist\DocVoice\data\logs rmdir /S /Q dist\DocVoice\data\logs
echo.
echo Готово: dist\DocVoice\DocVoice.exe
echo Папку dist\DocVoice можно переносить целиком.
pause
exit /b 0
:err
echo Ошибка сборки
pause
exit /b 1
