"""
Motor Principal de Automação de Consórcio V6 (Enterprise).

Arquitetura Server-to-Server com Ingestão HTTP Pura (requests).
Inclui validação de Schemas, endpoint de observabilidade (/health) e Cooldown.
"""

import hmac
import time
import threading
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from waitress import serve

from core.config import (
    TEMPO_INATIVIDADE_MAXIMO,
    TOKEN_API,
    URL_BASE_PORTAL,
    URL_WEBHOOK_BOLETO,
    CORS_ORIGIN,
    configurar_logger,
)
from core.utils import is_hibernando
from core.autenticacao import criar_sessao_http_autenticada
from core.extrator_portal import consultar_venda_http_completa
from core.extrator_boleto import extrair_pdf_boleto_memoria
from core.schemas import DadosVendaSchema

# ============================================================================
# CONFIGURAÇÃO DE LOGGING E INICIALIZAÇÃO DA API
# ============================================================================
logger_api = configurar_logger("SiteAPI", "files/site_sistema.log")

app = Flask(__name__)
CORS(app, origins=CORS_ORIGIN)

lock_api = threading.Lock()
INICIO_SERVIDOR = time.time()

ESTADO_API = {
    "sessao_http": None,
    "autenticado": False,
    "ultimo_keep_alive": time.time(),
    "ultimo_login": 0,
    "cooldown_ate": 0,
}


def garantir_sessao_http():
    """Cria ou renova a sessão HTTP usando o módulo central de autenticação."""
    agora = time.time()

    if agora < ESTADO_API["cooldown_ate"]:
        restante = int(ESTADO_API["cooldown_ate"] - agora)
        raise ConnectionError(f"Login em cooldown de proteção ({restante}s restantes).")

    precisa_relogin = (agora - ESTADO_API["ultimo_login"]) > 14000

    if (
        not ESTADO_API["autenticado"]
        or precisa_relogin
        or ESTADO_API["sessao_http"] is None
    ):
        logger_api.info("[SISTEMA] Sessão expirada. Invocando IA de Login...")
        try:
            nova_sessao = criar_sessao_http_autenticada()
        except Exception as e:
            ESTADO_API["cooldown_ate"] = time.time() + 600
            logger_api.error("[COOLDOWN] Falha no login. Pausa de 10 min ativada.")
            raise e

        ESTADO_API["sessao_http"] = nova_sessao
        ESTADO_API["autenticado"] = True
        ESTADO_API["ultimo_keep_alive"] = agora
        ESTADO_API["ultimo_login"] = agora
        ESTADO_API["cooldown_ate"] = 0
        logger_api.info("[LIBERADO] Sessão ancorada. Selenium Destruído.")


# ============================================================================
# TAREFAS EM BACKGROUND
# ============================================================================
def tarefa_background_boleto(cpf, grupo, cota, contrato, url_custom=None):
    """Extrai PDF em memória e dispara o Webhook respeitando o lock principal."""
    logger_api.info(
        "[FILA BOLETO] Contrato %s aguardando liberação do lock...", contrato
    )

    with lock_api:
        logger_api.info(
            "[FILA BOLETO] lock_api Liberada. Extraindo PDF do contrato %s...",
            contrato,
        )
        try:
            garantir_sessao_http()
            boleto = extrair_pdf_boleto_memoria(
                ESTADO_API["sessao_http"], cpf, grupo, cota
            )
        except Exception as e_bol:  # pylint: disable=broad-exception-caught
            logger_api.error(
                "[ERRO BOLETO] Falha na sessão ao extrair PDF (%s): %s",
                contrato,
                e_bol,
            )
            boleto = {"qr_code": "", "codigo_barras": ""}

    url_webhook_boleto = url_custom or URL_WEBHOOK_BOLETO

    payload = {
        "contrato": str(contrato),
        "link_qrcode": boleto.get("qr_code", ""),
        "numero_barcode": boleto.get("codigo_barras", ""),
    }

    headers_webhook = {"Authorization": TOKEN_API, "Content-Type": "application/json"}

    try:
        requests.post(
            url_webhook_boleto, json=payload, headers=headers_webhook, timeout=25
        )
        logger_api.info("[WEBHOOK BOLETO] Sucesso para o contrato: %s", contrato)
    except Exception as e_hook:  # pylint: disable=broad-exception-caught
        logger_api.warning("[ERRO WEBHOOK BOLETO] %s: %s", contrato, e_hook)


# ============================================================================
# ROTAS DA API (SAÚDE E PROCESSAMENTO DE VENDAS)
# ============================================================================
@app.route("/health", methods=["GET"])
def health_check():
    """Endpoint de observabilidade em tempo real (não bloqueante)."""
    agora = time.time()
    hibernando = is_hibernando()
    em_cooldown = agora < ESTADO_API["cooldown_ate"]

    status_geral = (
        "hibernating"
        if hibernando
        else ("degraded" if em_cooldown or not ESTADO_API["autenticado"] else "online")
    )

    return (
        jsonify(
            {
                "status": status_geral,
                "sessao_ativa": bool(ESTADO_API["autenticado"]),
                "hibernando": hibernando,
                "em_cooldown": em_cooldown,
                "ocupado": lock_api.locked(),
                "uptime_segundos": int(agora - INICIO_SERVIDOR),
                "ultimo_login_segundos": (
                    int(agora - ESTADO_API["ultimo_login"])
                    if ESTADO_API["ultimo_login"] > 0
                    else None
                ),
            }
        ),
        200,
    )


@app.route("/processar_venda", methods=["POST"])
def processar_venda():
    """Endpoint HTTP POST para extração veloz (1-2s) com tratamento de colisão."""
    if is_hibernando():
        return (
            jsonify(
                {
                    "erro": "hibernacao",
                    "mensagem": "O Tradição está em manutenção (21:00 às 06:30 UTC-4).",
                }
            ),
            503,
        )
    auth_header = request.headers.get("Authorization") or ""
    if not TOKEN_API or not hmac.compare_digest(auth_header, TOKEN_API):
        logger_api.warning("Acesso não autorizado. Token inválido.")
        return jsonify({"erro": "nao_autorizado", "mensagem": "Acesso negado."}), 403

    dados = request.json
    if not dados:
        return jsonify({"erro": "payload_vazio", "mensagem": "Payload vazio."}), 400

    contrato = str(dados.get("contrato")).strip()
    vendedor = str(dados.get("vendedor")).strip()
    url_webhook_custom = dados.get("webhook_url")

    logger_api.info("API RECEBIDA | Contrato: %s | Vendedor: %s", contrato, vendedor)

    obteve_acesso = lock_api.acquire(timeout=15)

    if not obteve_acesso:
        logger_api.warning(
            "COLISÃO DETETADA | Sistema ocupado. Contrato %s em espera.",
            contrato,
        )
        return (
            jsonify(
                {
                    "erro": "sistema_ocupado",
                    "mensagem": "O servidor está ocupado neste momento. Aguarde.",
                }
            ),
            429,
        )

    try:
        garantir_sessao_http()
        ESTADO_API["ultimo_keep_alive"] = time.time()

        resultado = consultar_venda_http_completa(ESTADO_API["sessao_http"], contrato)

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
                "1º Parcela Paga" if dados_site.get("pago") else "1º Parcela Não Paga"
            )

            logger_api.info("[SUCESSO API HTTP] Contrato %s extraído.", contrato)

            cpf_cli = dados_site.get("cpf")
            grupo_cli = dados_site.get("grupo")
            cota_cli = dados_site.get("cota")

            if cpf_cli and grupo_cli and cota_cli:
                threading.Thread(
                    target=tarefa_background_boleto,
                    args=(
                        cpf_cli,
                        grupo_cli,
                        cota_cli,
                        contrato,
                        url_webhook_custom,
                    ),
                ).start()
            else:
                logger_api.warning("Sem chaves para boleto no contrato %s", contrato)

            contrato_validado = DadosVendaSchema.criar_de_dados_site(
                contrato, dados_site
            )

            return (
                jsonify(
                    {
                        "sucesso": True,
                        "status_pagamento": texto_st,
                        "nome_cliente": str(dados_site.get("nome", "-")),
                        "dados_completos": contrato_validado.para_dict(),
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

    finally:
        lock_api.release()


# ============================================================================
# HEARTBEAT HTTP E MANUTENÇÃO PROATIVA DE SESSÃO
# ============================================================================
def loop_keep_alive():
    """
    Ping ultraleve para evitar queda da sessão e Motor Proativo de Login.
    """
    logger_api.info("Motor de Keep-Alive HTTP e Vigilância Proativa Iniciados.")
    while True:
        time.sleep(60)
        agora = time.time()

        if is_hibernando():
            with lock_api:
                if ESTADO_API["autenticado"] or ESTADO_API["sessao_http"] is not None:
                    logger_api.info(
                        "[HIBERNAÇÃO] Horário limite atingido. Pausando motor..."
                    )
                    try:
                        if ESTADO_API["sessao_http"]:
                            ESTADO_API["sessao_http"].close()
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
                    ESTADO_API["sessao_http"] = None
                    ESTADO_API["autenticado"] = False
                    ESTADO_API["ultimo_login"] = 0

            time.sleep(300)
            continue

        if agora < ESTADO_API["cooldown_ate"]:
            continue

        precisa_relogin = (agora - ESTADO_API.get("ultimo_login", 0)) > 14000

        if not ESTADO_API["autenticado"] or precisa_relogin:
            logger_api.info(
                "[BACKGROUND] Sessão ausente ou expirada. Iniciando login proativo..."
            )
            with lock_api:
                try:
                    garantir_sessao_http()
                except Exception as e_login:  # pylint: disable=broad-exception-caught
                    logger_api.error(
                        "[BACKGROUND] Falha no login: %s. Aguardando cooldown.",
                        e_login,
                    )
            continue

        with lock_api:
            if ESTADO_API["autenticado"] and ESTADO_API["sessao_http"]:
                tempo_inativo = agora - ESTADO_API["ultimo_keep_alive"]

                if tempo_inativo > TEMPO_INATIVIDADE_MAXIMO:
                    try:
                        url_ka = f"{URL_BASE_PORTAL}/LeftFrame.asp?codigo_modulo=AG"
                        res = ESTADO_API["sessao_http"].get(url_ka, timeout=10)

                        if "j_username" in res.text.lower() or res.status_code != 200:
                            logger_api.warning(
                                "Keep-Alive falhou. Sessão invalidada para resgate."
                            )
                            ESTADO_API["autenticado"] = False
                        else:
                            logger_api.info(
                                "    [SISTEMA] Keep-Alive executado com sucesso."
                            )
                            ESTADO_API["ultimo_keep_alive"] = time.time()
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger_api.warning(
                            "Keep-Alive (Queda de Rede). Sessão marcada como inválida."
                        )
                        ESTADO_API["autenticado"] = False


if __name__ == "__main__":
    logger_api.info(">>> INICIANDO SISTEMA ENTERPRISE V4.0 (API HTTP PURA) <<<")

    if not is_hibernando():
        try:
            logger_api.info("Executando login na inicialização...")
            garantir_sessao_http()
        except Exception as e_inicial:  # pylint: disable=broad-exception-caught
            logger_api.error("Falha ao acessar o portal: %s", e_inicial)
    else:
        logger_api.info(
            "Sistema iniciado em horário de hibernação. Login postergado para 06:30."
        )

    threading.Thread(target=loop_keep_alive, daemon=True).start()

    logger_api.info("Servidor WSGI pronto na porta 5050.")
    serve(app, host="0.0.0.0", port=5050)
