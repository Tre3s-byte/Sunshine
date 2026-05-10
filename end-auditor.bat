@echo off
set PYTHON=C:\Python314\python.exe

echo %date% %time% STREAM END >> C:\Tools\Sunshine\logs\bat-debug.log

%PYTHON% "C:\Tools\Sunshine\client\auditor_client.py" end >> C:\Tools\Sunshine\logs\bat-debug.log 2>&1

echo Exit code: %errorlevel% >> C:\Tools\Sunshine\logs\bat-debug.log
