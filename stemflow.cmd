@echo off
cd /d "%~dp0"
set "STEMFLOW_API_PORT=8000"
if exist ".env" for /f "usebackq tokens=1,* delims==" %%A in (".env") do if "%%A"=="AUDIO_SERVICE_API_PORT" set "STEMFLOW_API_PORT=%%B"
docker compose exec -T -e STEMFLOW_PUBLIC_API_URL=http://localhost:%STEMFLOW_API_PORT% backend stemflow %*
exit /b %errorlevel%
