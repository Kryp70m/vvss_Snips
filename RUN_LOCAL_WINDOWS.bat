@echo off
title VolSnipe V2 - Docker Build Configuration
echo Rebuilding V2 Docker Container Layers...
cd /d "%~dp0"
docker compose down
docker compose build --no-cache
echo.
echo Build complete. Ready to launch.
pause