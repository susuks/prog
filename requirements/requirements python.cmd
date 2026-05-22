@echo off
echo [SISTEMA] Iniciando configuracao do ambiente Python...
cd ..

if not exist "venv\" (
    echo [SISTEMA] Criando o ambiente virtual (venv)...
    python -m venv venv
)

echo [SISTEMA] Ativando o ambiente virtual e instalando pacotes...
call venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r "requirements\requirements all.txt"

echo [SISTEMA] Instalacao de dependencias do Python concluida com exito.
pause