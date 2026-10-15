# Arquitetura de Integração Server-to-Server (Consórcio Tradição / Autocred)

Sistema de alta disponibilidade para extração de dados cadastrais, decodificação de boletos em memória e sincronização contínua de relatórios de carteira entre o portal Consórcio Tradição e a plataforma Autocred.

---

## 1. Visão Geral da Arquitetura

O projeto opera sob uma **Arquitetura Híbrida de Sessão (Ephemeral Selenium + Pure HTTP)**:

1. **Autenticação Efêmera (`core/autenticacao.py`):** O navegador Chrome Headless é instanciado exclusivamente no momento do login para contornar o desafio ReCaptchaV2 via IA (CapSolver). Assim que os cookies de sessão ASP são ancorados no `MasterFrameset.asp`, o processo do navegador é destruído e a sessão é transferida para um objeto `requests.Session`.
2. **Extração Cadastral Expressa (`site_main.py` + `core/extrator_portal.py`):** Responde às requisições síncronas em **1 a 2 segundos** via chamadas HTTP diretas e parsing DOM (`BeautifulSoup`).
3. **Processamento Assíncrono de Boletos (`core/extrator_boleto.py`):** Após responder ao front-end, uma thread em background (protegida por mutex `lock_api`) faz o download do PDF da 2ª via em memória RAM (`pymupdf`), renderiza a página a 300 DPI (`Pillow`), decodifica o QR Code PIX e o código de barras I25 (`pyzbar`) sem I/O de disco e envia o resultado via Webhook.
4. **Sincronização de Longo Prazo (`api_cda_main.py`):** Worker independente que monitora o hash de atualização da API oficial da Tradição, baixa os lotes JSON, raspa o relatório legado de desistentes (`Codigo_Formulario=11345`) e envia os pacotes compactados em stream GZIP (`.json.gz`) para a Autocred.

---

## 2. Estrutura de Diretórios

```text
prog/
├── core/
│   ├── __init__.py          # Inicializador do pacote Core
│   ├── config.py            # Variáveis de ambiente (.env / config.txt), constantes e Logger rotativo
│   ├── autenticacao.py      # Login efêmero com Selenium + CapSolver e forja de sessão HTTP
│   ├── extrator_portal.py   # Raspagem HTTP (BeautifulSoup) de vendas e relatórios de carteira
│   ├── extrator_boleto.py   # Leitura em memória RAM de PDF, QR Code PIX e Código de Barras
│   ├── schemas.py           # Contratos de dados estritos (Dataclasses) para validação de saída
│   └── utils.py             # Formatação monetária/datas, controle de hibernação UTC-4 e I/O atômico
├── files/
│   └── config(template).txt # Template legado de configuração
├── requirements/
│   └── requirements.txt     # Dependências Python do ambiente de produção
├── .env.example             # Modelo de variáveis de ambiente
├── ecosystem.config.js      # Orquestração declarativa do PM2 com limites de RAM
├── site_main.py             # Entrypoint 1: API WSGI (Waitress - Porta 5050)
└── api_cda_main.py          # Entrypoint 2: Worker de Sincronização Contínua (Polling 1h)
