@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Докладывает в готовую сборку библиотеки VC++, нужные PyTorch (Silero) на других ПК.
if not exist dist\DocVoice\_internal\torch\lib (echo Не найдена сборка с Silero: dist\DocVoice\_internal\torch & pause & exit /b 1)
for %%D in (vcruntime140_threads.dll msvcp140_1.dll msvcp140_2.dll msvcp140_atomic_wait.dll concrt140.dll vcomp140.dll) do (
  if exist "%SystemRoot%\System32\%%D" (copy /Y "%SystemRoot%\System32\%%D" dist\DocVoice\_internal\torch\lib\ >nul && echo + %%D)
)
echo.
echo Готово. Скопируйте папку dist\DocVoice на другой ПК заново
echo (или только папку _internal\torch\lib).
pause
