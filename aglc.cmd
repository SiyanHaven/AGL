@echo off
rem AGL command line launcher: works from any directory, pip install not required.
rem Default: use the same `python` that the user agent tool invokes.
rem Override with `set AGL_PYTHON=C:\path\to\python.exe`.
setlocal
set "AGL_ROOT=%~dp0"
if "%PYTHONPATH%"=="" (
  set "PYTHONPATH=%AGL_ROOT%"
) else (
  set "PYTHONPATH=%AGL_ROOT%;%PYTHONPATH%"
)
set "PYTHONUTF8=1"

if defined AGL_PYTHON goto :run_env
if exist "%AGL_ROOT%python-path.txt" goto :read_hint
goto :find_path

:read_hint
set "AGL_PY="
set /p AGL_PY=<"%AGL_ROOT%python-path.txt"
if not defined AGL_PY goto :find_path
if not exist "%AGL_PY%" goto :find_path
"%AGL_PY%" -m compiler.cli %*
exit /b

:run_env
"%AGL_PYTHON%" -m compiler.cli %*
exit /b

:find_path
where python >nul 2>nul
if errorlevel 1 goto :use_py
python -m compiler.cli %*
exit /b

:use_py
py -3 -m compiler.cli %*
exit /b
