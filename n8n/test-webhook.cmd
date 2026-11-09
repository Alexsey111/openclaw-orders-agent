@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo.
echo   Production: POST http://localhost:5678/webhook/orders-intake
echo.
curl.exe -s -X POST "http://localhost:5678/webhook/orders-intake" -H "Content-Type: application/json" -d @payload.json -w "\nHTTP %%{http_code}\n"
echo.
echo   Test (only right after clicking Execute workflow, one call):
echo.
curl.exe -s -X POST "http://localhost:5678/webhook-test/orders-intake" -H "Content-Type: application/json" -d @payload.json -w "\nHTTP %%{http_code}\n"
echo.
pause
