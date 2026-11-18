@echo off
echo =========================================
echo   Twitch Drop Miner Multiuser Mod
echo   Docker Setup Script
echo =========================================
echo.

:: Stop and remove existing container
echo [1/4] Stopping existing container...
docker stop dnx-twitch-miner-multiuser-mod 2>nul
docker rm dnx-twitch-miner-multiuser-mod 2>nul

:: Remove old image
echo [2/4] Removing old image...
docker rmi dnx-twitch-miner-multiuser-mod 2>nul

:: Build new image (no code embedded - comes from volume)
echo [3/4] Building Docker image...
docker build -t dnx-twitch-miner-multiuser-mod .

:: Run container (with volume for code)
echo [4/4] Starting container...
docker run -d ^
    --name dnx-twitch-miner-multiuser-mod ^
    -p 4295:4295 ^
    -v "%CD%:/app" ^
    --restart unless-stopped ^
    dnx-twitch-miner-multiuser-mod

echo.
echo =========================================
echo   Done!
echo   Web UI: http://localhost:4295
echo   Logs: %CD%\output.log
echo =========================================
echo.
pause