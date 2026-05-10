@echo off
set PYTHON=C:\Python314\python.exe

echo %date% %time% STREAM START >> C:\Tools\Sunshine\logs\bat-debug.log

%PYTHON% "C:\Tools\Sunshine\client\auditor_client.py" start >> C:\Tools\Sunshine\logs\bat-debug.log 2>&1

echo Exit code: %errorlevel% >> C:\Tools\Sunshine\logs\bat-debug.log
