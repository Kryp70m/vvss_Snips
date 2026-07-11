@echo off
title VolSnipe V2 - Total System Refresh
echo ===================================================
echo [1/3] Stopping existing containers and wiping cache...
echo ===================================================
docker-compose down --volumes --remove-orphans
docker-compose build --no-cache
docker-compose up -d --force-recreate

echo.
echo ===================================================
echo [2/3] Launching web engine and streaming matrix...
echo ===================================================
docker-compose up -d

echo.
echo ===================================================
echo SUCCESS: System refreshed! Your dashboard is live.
echo ===================================================
pause