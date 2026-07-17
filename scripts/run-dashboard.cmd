@echo off
cd /d "%~dp0.."
"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m polybtc dashboard 1>> "data\dashboard-live.stdout.log" 2>> "data\dashboard-live.stderr.log"
