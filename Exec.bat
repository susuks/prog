0'@echo off

echo ==========================================
echo   INICIANDO SISTEMA DE AUTOMACAO TOTAL
echo ==========================================

:: 1. Inicia o Coletor Python (Intranet)
::start "1. COLETOR" cmd /k "python main.py"

:: 2. Inicia o Monitor de Vendas (Texto)
start "2. CDA" cmd /k "python cda_main.py"

:: 3. Inicia o Monitor de Vendas (Texto)
::start "3. MONITOR" cmd /k "node monitor.js"

:: 4. Inicia o Carteiro de Mídia
::start "4. ENCAMINHADOR" cmd /k "node encaminhar_midia.js"

echo Tudo iniciado! Pode minimizar esta janela.
pause