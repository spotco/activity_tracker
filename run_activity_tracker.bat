@echo off
setlocal
cd /d "%~dp0"
start "" /b pyw -3.11 "%~dp0activity_tracker.pyw"
endlocal
