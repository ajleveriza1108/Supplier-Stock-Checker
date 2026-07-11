@echo off
 1. Check for Administrator privileges
net session nul 2&1
if %errorLevel% == 0 (
    goto start_app
) else (
    echo Requesting Administrator Privileges...
    powershell -Command Start-Process cmd -ArgumentList 'c %~dpnx0' -Verb RunAs
    exit
)

start_app
 2. Instantly jump to your deep subfolder
cd d DDropboxPythonProjectsstock_checker

 3. Launch the application
py main.py