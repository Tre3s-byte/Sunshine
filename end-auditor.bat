@echo off
set PYTHON=C:\Python314\python.exe

echo %date% %time% START >> C:\Tools\Sunshine\logs\bat-debug.log

%PYTHON% -c "import sys; print('EXEC:', sys.executable)" >> C:\Tools\Sunshine\logs\bat-debug.log

%PYTHON% "C:\Tools\Sunshine\auditor-client.py" end >> C:\Tools\Sunshine\logs\bat-debug.log 2>&1

echo Exit code: %errorlevel% >> C:\Tools\Sunshine\logs\bat-debug.log