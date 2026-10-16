# Motor de Integração Server-to-Server — Consórcio Tradição & Autocred

![Python](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-WSGI%20Waitress-lightgrey?logo=flask)
![Architecture](https://img.shields.io/badge/Architecture-Hybrid%20HTTP%20%2B%20AI-success)
![PM2](https://img.shields.io/badge/Process%20Manager-PM2-2B037A?logo=pm2&logoColor=white)
![Code Style](https://img.shields.io/badge/Linter-Pylint%2010%2F10-brightgreen)

Serviço de alta disponibilidade responsável pela extração cadastral expressa, decodificação em memória de boletos bancários (QR Code PIX e Código de Barras I25) e sincronização contínua de relatórios de carteira entre a intranet legada do **Consórcio Tradição** e a plataforma **Autocred**.

---

## Principais Características

* **Ingestão 100% HTTP (`requests` + `BeautifulSoup4`):** Tempo médio de resposta de **1 a 2 segundos** para consultas cadastrais síncronas.
* **Autenticação Efêmera via IA:** Uso pontual do Selenium Headless integrado ao CapSolver apenas para transpor o desafio ReCaptchaV2 e ancorar cookies de sessão ASP, destruindo o processo do navegador imediatamente após o login.
* **Processamento de Boletos Zero-Disk I/O:** Download e renderização de PDFs a 300 DPI diretamente na memória RAM (`PyMuPDF`, `Pillow`, `pyzbar`), prevenindo condições de corrida e desgaste de disco.
* **Contratos de Dados Estritos (`core/schemas.py`):** Sanitização e validação de tipagem de saída antes da entrega via API ou Webhook.
* **Proteção Operacional:** Controle de concorrência por Mutex (`threading.Lock`), disjuntor de *Cooldown* (10 minutos) contra falhas de login e janela automática de hibernação noturna (21:00 às 06:30 UTC-4).

> Para detalhes aprofundados sobre o fluxo de sessão, estrutura de pastas e especificações de rotas, consulte a [**Documentação de Arquitetura (ARCHITECTURE.md)**](./ARCHITECTURE.md).

---

## Implantação Automatizada (Ubuntu / Debian)

O repositório conta com o script `deploy.sh`, que automatiza a verificação da biblioteca óptica do sistema (`libzbar0`), atualização do código via Git, configuração do ambiente virtual (`venv`), instalação do `requirements.txt` e recarregamento dos processos no PM2:

```bash
chmod +x deploy.sh
./deploy.sh
```

---

## Instalação Manual Passo a Passo

1. Instale a biblioteca nativa necessária para decodificação óptica de códigos de barras:

   ```bash
   sudo apt-get update && sudo apt-get install -y libzbar0 python3-venv
   ```

2. Crie o ambiente virtual e instale as dependências Python da raiz:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Configure as variáveis de ambiente copiando o arquivo de modelo:

   ```bash
   cp .env.example .env
   ```

   Preencha o arquivo `.env` com as credenciais do portal, chave da CapSolver, URLs e tokens de autenticação.

---

## Execução e Orquestração (PM2)

O arquivo `ecosystem.config.js` gerencia simultaneamente a API síncrona (`site_main.py`) e o Worker de sincronização de carteira (`api_cda_main.py`):

```bash
# Iniciar ou recarregar os serviços com limite de RAM configurado
pm2 startOrReload ecosystem.config.js --update-env

# Verificar status e consumo de memória
pm2 status

# Acompanhar logs em tempo real
pm2 logs
```

---

## Estrutura de Branches

* **`main`:** Arquitetura atual Server-to-Server (API REST + Worker de Sincronização GZIP).
* **`legacy`:** Versão anterior do motor integrada diretamente ao WhatsApp (`whatsapp-web.js`) e planilhas do Google Sheets (`gspread`).
