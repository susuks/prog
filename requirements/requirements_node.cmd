@echo off
echo [SISTEMA] Iniciando configuracao do ambiente Node.js...
cd ..

echo [SISTEMA] Executando a instalacao dos pacotes via NPM...
npm install whatsapp-web.js qrcode-terminal csv-parser axios winston

echo [SISTEMA] Instalacao de dependencias do Node.js concluida com exito.
pause