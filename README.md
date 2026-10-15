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

## Pré-requisitos de Sistema

No servidor Ubuntu/Debian, instale a biblioteca nativa necessária para decodificação óptica de códigos de barras:

```bash
sudo apt-get update && sudo apt-get install -y libzbar0
