"""
Módulo de Configurações Globais e Infraestrutura de Logging.

Carrega variáveis de ambiente exclusivamente via arquivo .env (python-dotenv),
centraliza constantes de URLs e padroniza logs rotativos.
"""

import os
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# Carrega as variáveis de ambiente a partir do arquivo .env na raiz
load_dotenv(".env")

# ============================================================================
# CAMINHOS E CONSTANTES GLOBAIS
# ============================================================================
PASTA_TEMP_CDA = "api_trad"
TEMPO_INATIVIDADE_MAXIMO = 300


def carregar_configuracoes() -> dict:
    """Retorna dicionário de configurações e URLs lidas do ambiente (.env)."""
    return {
        "MATRICULA": os.getenv("MATRICULA", "").strip(),
        "SENHA": os.getenv("SENHA", "").strip(),
        "TOKEN_API": os.getenv("TOKEN_API", "").strip(),
        "CAPSOLVER": os.getenv("CAPSOLVER", "").strip(),
        "KEY": os.getenv("KEY", "").strip(),
        "TOKEN_SITE": os.getenv("TOKEN_SITE", "").strip(),
        "URL_BASE_PORTAL": os.getenv("URL_BASE_PORTAL", "").strip().rstrip("/"),
        "URL_BASE_API_TRADICAO": (
            os.getenv("URL_BASE_API_TRADICAO", "").strip().rstrip("/")
        ),
        "URL_UPLOAD_AUTOCRED": os.getenv("URL_UPLOAD_AUTOCRED", "").strip(),
        "URL_WEBHOOK_BOLETO": os.getenv("URL_WEBHOOK_BOLETO", "").strip(),
        "CORS_ORIGIN": os.getenv("CORS_ORIGIN", "*").strip(),
    }


CONFIG = carregar_configuracoes()

# Credenciais e Chaves
USUARIO_LOGIN = CONFIG["MATRICULA"]
SENHA_LOGIN = CONFIG["SENHA"]
TOKEN_API = CONFIG["TOKEN_API"]
CHAVE_CAPSOLVER = CONFIG["CAPSOLVER"]
KEY_API_TRADICAO = CONFIG["KEY"]
TOKEN_SITE_AUTOCRED = CONFIG["TOKEN_SITE"]

# URLs e Endpoints Dinâmicos
URL_BASE_PORTAL = CONFIG["URL_BASE_PORTAL"]
URL_BASE_API_TRADICAO = CONFIG["URL_BASE_API_TRADICAO"]
URL_ATUALIZACAO_API = f"{URL_BASE_API_TRADICAO}/ultima_atualizacao"
URL_UPLOAD_AUTOCRED = CONFIG["URL_UPLOAD_AUTOCRED"]
URL_WEBHOOK_BOLETO = CONFIG["URL_WEBHOOK_BOLETO"]
CORS_ORIGIN = CONFIG["CORS_ORIGIN"]

ENDPOINTS_API_TRADICAO = {
    "cotas": f"{URL_BASE_API_TRADICAO}/customers",
    "boletos": f"{URL_BASE_API_TRADICAO}/customers/boletos",
    "calendario_assem": f"{URL_BASE_API_TRADICAO}/customers/calendario",
    "clientes": f"{URL_BASE_API_TRADICAO}/customers/clientes",
    "comissoes": f"{URL_BASE_API_TRADICAO}/customers/comissao",
    "dev_desistentes": f"{URL_BASE_API_TRADICAO}/customers/dev-cancelados",
    "ultima_atualizacao": f"{URL_BASE_API_TRADICAO}/ultima_atualizacao",
    "ultima_parc_paga": f"{URL_BASE_API_TRADICAO}/customers/parcpg",
}


def configurar_logger(nome_logger: str, arquivo_log: str) -> logging.Logger:
    """Cria e retorna um logger com rotação automática de arquivos (5MB)."""
    os.makedirs(os.path.dirname(arquivo_log), exist_ok=True)
    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    logger = logging.getLogger(nome_logger)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.hasHandlers():
        logger.handlers.clear()

    handler_arquivo = RotatingFileHandler(
        arquivo_log,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler_arquivo.setFormatter(log_formatter)

    handler_console = logging.StreamHandler()
    handler_console.setFormatter(log_formatter)

    logger.addHandler(handler_arquivo)
    logger.addHandler(handler_console)

    return logger
