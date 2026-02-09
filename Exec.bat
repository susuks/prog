0'@echo off
cd /d "%~dp0main prog"

echo ==========================================
echo   INICIANDO SISTEMA DE AUTOMACAO TOTAL
echo ==========================================
echo.
echo Iniciando os 3 robos em janelas separadas...

:: 1. Inicia o Monitor de Vendas (Texto)
start "1. MONITOR" cmd /k "node monitor.js"

:: 2. Inicia o Coletor Python (Intranet e Planilhas Google)
start "2. COLETOR" cmd /k "python coleta_dados.py"

echo.
echo Tudo iniciado! Pode minimizar esta janela.
pause