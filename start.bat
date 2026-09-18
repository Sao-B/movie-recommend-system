@echo off
setlocal
rem start.bat - start Flask backend + Vite frontend in hidden background windows.
rem
rem KEEP THIS FILE ASCII. cmd.exe reads .bat files using the OEM codepage (936 on
rem this machine), so UTF-8 Chinese inside a rem line gets mis-decoded and can be
rem executed as a command. That already happened once here and caused a recursive
rem start.bat loop. The Chinese explanation of this file lives in scripts/README.md.
rem
rem The services open the browser once the frontend is up, and stop themselves
rem after the TTL below, so there is no stop script.
rem
rem Usage:  start.bat        start, auto-stop after 4 hours
rem         start.bat 8      auto-stop after 8 hours
rem         start.bat 0      no auto-stop
set "PROJECT_ROOT=%~dp0"
set "TTL=%~1"
if "%TTL%"=="" set "TTL=4"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%scripts\start_bg.ps1" -TtlHours %TTL%
