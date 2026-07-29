@echo off
REM Ejecutado por la tarea programada de Windows "PlusDigital - Sincronizar Lecturas"
REM (ver scripts/registrar_tarea_sincronizar_lecturas.ps1). Corre lo mismo que el
REM boton "Sincronizar lecturas ahora" del dashboard, pero automatico a diario.
setlocal
set PROYECTO=C:\Proyectos\Sistema-Facturacion
if not exist "%PROYECTO%\logs" mkdir "%PROYECTO%\logs"
cd /d "%PROYECTO%"
echo ==== %date% %time% ==== >> "%PROYECTO%\logs\sincronizar_lecturas.log"
"%PROYECTO%\venv\Scripts\python.exe" manage.py sincronizar_lecturas >> "%PROYECTO%\logs\sincronizar_lecturas.log" 2>&1
endlocal
