"""
Motor Principal de Automação de Consórcio V6 (Enterprise).

Arquitetura Server-to-Server com Ingestão HTTP Pura (requests).
O Selenium é instanciado de forma efêmera apenas para renovação de token IA.
"""

import time
import threading
import logging
from logging.handlers import RotatingFileHandler
import re
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from waitress import serve

from gerador_dados import (
    registrar_contrato_unificado,
    TEMPO_INATIVIDADE_MAXIMO,
    TOKEN_API,
)

from old.motor_navegacao import (
    iniciar_navegador,
    fazer_login_com_ia,
)

from cda_modulos import (
    consultar_venda_http_completa,
    extrair_pdf_boleto_memoria,
)

# ============================================================================
# CONFIGURAÇÃO DE LOGGING ESTRUTURADO ROTATIVO (INDEPENDENTE)
# ============================================================================
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

logger_api = logging.getLogger("SiteAPI")
logger_api.setLevel(logging.INFO)
logger_api.propagate = False  # Evita logs duplicados no console pelo Flask

if logger_api.hasHandlers():
    logger_api.handlers.clear()

handler_arquivo = RotatingFileHandler(
    "files/site_sistema.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
handler_arquivo.setFormatter(log_formatter)

handler_console = logging.StreamHandler()
handler_console.setFormatter(log_formatter)

logger_api.addHandler(handler_arquivo)
logger_api.addHandler(handler_console)

# ============================================================================
# INICIALIZAÇÃO DA API E ESTADO GLOBAL
# ============================================================================
app = Flask(__name__)
CORS(app, origins="https://autocredbrasil.app")

lock_api = threading.Lock()

ESTADO_API = {
    "sessao_http": None,
    "autenticado": False,
    "ultimo_keep_alive": time.time(),
    "ultimo_login": 0,
}


def aquecer_sessao_asp(sessao: requests.Session):
    """Ancora os cookies forjados acessando as molduras primárias do portal."""
    try:
        url_master = (
            "https://intranet.consorciotradicao.com.br/autocred/MasterFrameset.asp"
        )
        url_left = (
            "https://intranet.consorciotradicao.com.br/autocred/"
            "LeftFrame.asp?codigo_modulo=AG"
        )
        url_pesq = (
            "https://intranet.consorciotradicao.com.br/autocred/Attendance/"
            "searchCota.asp?codigo_formulario_intranet=4&"
            "descricao_formulario_intranet=Consorciado"
        )

        sessao.get(url_master, timeout=10)
        sessao.get(url_left, timeout=10)
        sessao.get(url_pesq, timeout=10)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_api.warning("Aviso durante aquecimento da sessão ASP: %s", e)


def garantir_sessao_http():
    """Cria ou renova a sessão HTTP usando Selenium efêmero para transbordo."""
    agora = time.time()
    precisa_relogin = (agora - ESTADO_API["ultimo_login"]) > 14000

    if (
        not ESTADO_API["autenticado"]
        or precisa_relogin
        or ESTADO_API["sessao_http"] is None
    ):
        logger_api.info("[SISTEMA] Sessão expirada. Invocando IA de Login...")
        driver_temp = iniciar_navegador()

        if fazer_login_com_ia(driver_temp):
            nova_sessao = requests.Session()
            nova_sessao.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                }
            )

            for cookie in driver_temp.get_cookies():
                nova_sessao.cookies.set(
                    cookie["name"], cookie["value"], domain=cookie["domain"]
                )

            driver_temp.quit()
            aquecer_sessao_asp(nova_sessao)

            ESTADO_API["sessao_http"] = nova_sessao
            ESTADO_API["autenticado"] = True
            ESTADO_API["ultimo_keep_alive"] = agora
            ESTADO_API["ultimo_login"] = agora
            logger_api.info("[LIBERADO] Sessão ancorada. Selenium Destruído.")
        else:
            driver_temp.quit()
            logger_api.error("[BARRADO] Falha ao forjar sessão HTTP.")
            raise ConnectionError("Falha de autenticação IA no portal Autocred.")


# ============================================================================
# TAREFAS EM BACKGROUND
# ============================================================================
def tarefa_background_boleto(sessao, cpf, grupo, cota, contrato, url_custom=None):
    """Extrai PDF em memória e dispara o Webhook sem bloquear a API principal."""
    boleto = extrair_pdf_boleto_memoria(sessao, cpf, grupo, cota)

    url_webhook_boleto = url_custom or "https://autocredbrasil.app/api/webhook/boleto"

    payload = {
        "contrato": str(contrato),
        "link_qrcode": boleto.get("qr_code", ""),
        "numero_barcode": boleto.get("codigo_barras", ""),
    }

    headers_webhook = {"Authorization": TOKEN_API, "Content-Type": "application/json"}

    try:
        requests.post(
            url_webhook_boleto, json=payload, headers=headers_webhook, timeout=15
        )
        logger_api.info("[WEBHOOK BOLETO] Sucesso para o contrato: %s", contrato)
    except Exception as e_hook:  # pylint: disable=broad-exception-caught
        logger_api.warning("[ERRO WEBHOOK BOLETO] %s: %s", contrato, e_hook)


# ============================================================================
# ROTA DA API PRINCIPAL
# ============================================================================
@app.route("/processar_venda", methods=["POST"])
def processar_venda():
    """Endpoint HTTP POST para extração veloz (1-2s)."""
    auth_header = request.headers.get("Authorization")
    if auth_header != TOKEN_API:
        logger_api.warning("Acesso não autorizado. Token: %s", auth_header)
        return jsonify({"erro": "nao_autorizado", "mensagem": "Acesso negado."}), 403

    dados = request.json
    if not dados:
        return jsonify({"erro": "payload_vazio", "mensagem": "Payload vazio."}), 400

    contrato = str(dados.get("contrato")).strip()
    vendedor = str(dados.get("vendedor")).strip()
    url_webhook_custom = dados.get("webhook_url")

    logger_api.info("API RECEBIDA | Contrato: %s | Vendedor: %s", contrato, vendedor)

    with lock_api:
        try:
            garantir_sessao_http()
            ESTADO_API["ultimo_keep_alive"] = time.time()

            resultado = consultar_venda_http_completa(
                ESTADO_API["sessao_http"], contrato
            )

            if resultado["motivo"] == "SESSAO_CAIU":
                logger_api.warning("Sessão HTTP desancorada. Resgatando...")
                ESTADO_API["autenticado"] = False
                garantir_sessao_http()
                resultado = consultar_venda_http_completa(
                    ESTADO_API["sessao_http"], contrato
                )

            if resultado["encontrou"]:
                dados_site = resultado["dados"]
                texto_st = (
                    "1º Parcela Paga"
                    if dados_site.get("pago")
                    else "1º Parcela Não Paga"
                )

                registrar_contrato_unificado(
                    contrato=contrato,
                    vendedor_nome=vendedor,
                    nome_planilha="MIGRADO_PRO_SITE",
                    vendedor_tel="N/A",
                    origem="",
                    dados_site=dados_site,
                    aba_original="MIGRADO",
                )

                logger_api.info("[SUCESSO API HTTP] Contrato %s gravado.", contrato)

                cpf_cli = dados_site.get("cpf")
                grupo_cli = dados_site.get("grupo")
                cota_cli = dados_site.get("cota")

                # DISPARO DA TAREFA PESADA EM BACKGROUND
                if cpf_cli and grupo_cli and cota_cli:
                    threading.Thread(
                        target=tarefa_background_boleto,
                        args=(
                            ESTADO_API["sessao_http"],
                            cpf_cli,
                            grupo_cli,
                            cota_cli,
                            contrato,
                            url_webhook_custom,
                        ),
                    ).start()
                else:
                    logger_api.warning(
                        "Sem chaves para boleto no contrato %s", contrato
                    )

                # RESPOSTA IMEDIATA AO FRONT-END
                tel_cliente = str(dados_site.get("telefone", ""))
                numeros_limpos = re.sub(r"\D", "", tel_cliente)
                tel_limpo = f"55{numeros_limpos}" if numeros_limpos else ""

                return (
                    jsonify(
                        {
                            "sucesso": True,
                            "status_pagamento": texto_st,
                            "nome_cliente": str(dados_site.get("nome", "-")),
                            "dados_completos": {
                                "contrato": contrato,
                                "cpf": cpf_cli,
                                "grupo": grupo_cli,
                                "cota": cota_cli,
                                "versao": dados_site.get("versao"),
                                "estado_uf": dados_site.get("estado"),
                                "valor_credito": dados_site.get("credito"),
                                "valor_parcela": dados_site.get("valor_parcela", 0.0),
                                "data_venda_painel": dados_site.get("data_venda"),
                                "primeira_parcela_paga": dados_site.get("pago"),
                                "telefone_cliente": str(tel_limpo),
                            },
                        }
                    ),
                    200,
                )

            logger_api.error(
                "API RECUSADA HTTP | Contrato %s não localizado. Motivo: %s",
                contrato,
                resultado.get("motivo"),
            )
            return (
                jsonify(
                    {
                        "erro": "contrato_nao_encontrado",
                        "mensagem": "Não localizado no painel.",
                    }
                ),
                404,
            )

        except Exception as e_motor:  # pylint: disable=broad-exception-caught
            logger_api.error("Erro interno do motor HTTP: %s", e_motor)
            return jsonify({"erro": "erro_interno", "mensagem": str(e_motor)}), 500


# ============================================================================
# HEARTBEAT HTTP
# ============================================================================
def loop_keep_alive():
    """Ping ultraleve para evitar a derrubada da sessão pelo servidor."""
    logger_api.info("Motor de Keep-Alive HTTP Iniciado.")
    while True:
        time.sleep(60)
        with lock_api:
            if ESTADO_API["autenticado"] and ESTADO_API["sessao_http"]:
                tempo_inativo = time.time() - ESTADO_API["ultimo_keep_alive"]

                if tempo_inativo > TEMPO_INATIVIDADE_MAXIMO:
                    try:
                        url_ka = (
                            "https://intranet.consorciotradicao.com.br"
                            "/autocred/LeftFrame.asp?codigo_modulo=AG"
                        )
                        res = ESTADO_API["sessao_http"].get(url_ka, timeout=10)

                        if "j_username" in res.text.lower() or res.status_code != 200:
                            logger_api.warning("Keep-Alive falhou. Relogin agendado.")
                            ESTADO_API["autenticado"] = False
                        else:
                            logger_api.info("    [SISTEMA] Keep-Alive executado.")
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger_api.warning("Keep-Alive (Queda). Relogin agendado.")
                        ESTADO_API["autenticado"] = False

                    ESTADO_API["ultimo_keep_alive"] = time.time()


if __name__ == "__main__":
    logger_api.info(">>> INICIANDO SISTEMA ENTERPRISE V6 (API HTTP PURA) <<<")

    try:
        logger_api.info("Executando login na inicialização...")
        garantir_sessao_http()
    except Exception as e_inicial:  # pylint: disable=broad-exception-caught
        logger_api.error("Falha ao acessar o portal: %s", e_inicial)

    threading.Thread(target=loop_keep_alive, daemon=True).start()

    logger_api.info("Servidor WSGI pronto na porta 5050.")
    serve(app, host="0.0.0.0", port=5050)
