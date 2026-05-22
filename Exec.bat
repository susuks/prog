0'@echo off

echo ==========================================
echo   INICIANDO SISTEMA DE AUTOMACAO TOTAL
echo ==========================================

:: 1. Inicia o Coletor Python (Intranet)
start "2. COLETOR" cmd /k "python main.py"

:: 2. Inicia o Monitor de Vendas (Texto)
start "1. MONITOR" cmd /k "node monitor.js"

:: 3. Inicia o Carteiro de Mídia
::start "3. ENCAMINHADOR" cmd /k "node encaminhar_midia.js"

echo Tudo iniciado! Pode minimizar esta janela.
pause