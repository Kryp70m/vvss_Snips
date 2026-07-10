@echo off
setlocal EnableExtensions
title Stop Alpha Sniper Flow

echo Stopping Alpha Sniper Flow local servers...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /F /PID %%P >nul 2>nul
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8099" ^| findstr "LISTENING"') do taskkill /F /PID %%P >nul 2>nul

echo Done.
pause
