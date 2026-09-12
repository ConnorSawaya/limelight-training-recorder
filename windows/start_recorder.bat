@echo off
setlocal
cd /d "%~dp0.."
where py >nul 2>&1
if not errorlevel 1 goto use_py
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" goto use_known_313
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" goto use_known_312
if exist "%LocalAppData%\Programs\Python\Python311\python.exe" goto use_known_311
python limelight_recorder.py start %*
goto done
:use_py
py -3 limelight_recorder.py start %*
goto done
:use_known_313
"%LocalAppData%\Programs\Python\Python313\python.exe" limelight_recorder.py start %*
goto done
:use_known_312
"%LocalAppData%\Programs\Python\Python312\python.exe" limelight_recorder.py start %*
goto done
:use_known_311
"%LocalAppData%\Programs\Python\Python311\python.exe" limelight_recorder.py start %*
:done
if errorlevel 1 pause
