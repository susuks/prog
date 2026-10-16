#!/usr/bin/env bash
# Script de Implantacao e Atualizacao Automatizada (Ubuntu/Debian)
set -e

echo ">>> [1/5] Verificando dependencias de sistema (libzbar0)..."
if ! dpkg -s libzbar0 >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y libzbar0 python3-venv
fi

echo ">>> [2/5] Sincronizando codigo com o repositorio Git (branch main)..."
git fetch origin
git reset --hard origin/main

echo ">>> [3/5] Configurando ambiente virtual Python (venv)..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r /requirements.txt -q

echo ">>> [4/5] Verificando arquivo de variaveis de ambiente (.env)..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "[AVISO] Arquivo .env criado a partir do modelo. Preencha as credenciais antes de operar."
    exit 1
fi

echo ">>> [5/5] Recarregando servicos no PM2..."
pm2 startOrReload ecosystem.config.js --update-env
pm2 save

echo ">>> Deploy concluido com sucesso!"