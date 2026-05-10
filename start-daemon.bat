@echo off
set PYTHON=C:\Python314\python.exe

echo %date% %time% DAEMON START >> C:\Tools\Sunshine\logs\bat-debug.log

%PYTHON% "C:\Tools\Sunshine\daemon\sunshine_daemon.py" >> C:\Tools\Sunshine\logs\bat-debug.log 2>&1

echo Exit code: %errorlevel% >> C:\Tools\Sunshine\logs\bat-debug.log
