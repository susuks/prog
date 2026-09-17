"""
Motor Principal de Automação de Consórcio V6 (Enterprise).

Implementa arquitetura de microsserviços internos, atuando exclusivamente como
uma API REST de ingestão de dados. Extrai dados via Selenium e injeta no JSON Unificado.
"""

import time
import threading
import logging
import re
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify
from waitress import serve
from flask_cors import CORS

from gerador_dados import (
    PREFIXO_PLANILHA,
    NOME_ABA_GERAL,
    obter_mes_utc4,
    conectar_google_sheets,
    atualizar_planilha_vendedor,
    atualizar_planilha_geral,
    salvar_historico_concluido,
    verificar_contrato_registrado,
    registrar_contrato_unificado,
    TEMPO_INATIVIDADE_MAXIMO,
    TOKEN_API,
)

from motor_navegacao import (
    iniciar_navegador,
    buscar_contrato,
    extrair_dados_completos,
    manter_sessao_viva,
    fazer_login_com_ia,
)

# ============================================================================
# CONFIGURAÇÃO DE LOGGING ESTRUTURADO ROTATIVO
# ============================================================================
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

logger_api = logging.getLogger("EnterpriseAPI")
logger_api.setLevel(logging.INFO)
handler_api = RotatingFileHandler(
    "files/python_sistema.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
handler_api.setFormatter(log_formatter)
logger_api.addHandler(handler_api)
logger_api.addHandler(logging.StreamHandler())

# ============================================================================
# INICIALIZAÇÃO DA API E ESTADO GLOBAL
# ============================================================================
app = Flask(__name__)
CORS(app, origins="https://autocredbrasil.app")

driver_api = None
lock_api = threading.Lock()

ESTADO_API = {
    "autenticado": False,
    "ultimo_keep_alive": time.time(),
    "ultimo_login": 0,
}


def garantir_sessao(driver_alvo, estado_alvo, logger_alvo):
    """Garante o acesso e efetua login preventivo após 4 horas de ciclo."""
    agora = time.time()
    precisa_relogin = (agora - estado_alvo.get("ultimo_login", 0)) > 14400

    if not estado_alvo["autenticado"] or precisa_relogin:
        if precisa_relogin and estado_alvo["autenticado"]:
            logger_alvo.info(
                "[SISTEMA] Ciclo de 4 horas atingido. Executando re-login preventivo."
            )

        if fazer_login_com_ia(driver_alvo):
            logger_alvo.info(
                "[LIBERADO] Acesso restabelecido via Inteligência Artificial."
            )
            estado_alvo["autenticado"] = True
            estado_alvo["ultimo_keep_alive"] = agora
            estado_alvo["ultimo_login"] = agora
        else:
            logger_alvo.error("[BARRADO] Falha crítica de login.")
            raise ConnectionError("Falha de autenticação no portal Autocred.")


# ============================================================================
# ROTA DA API (RECEPÇÃO DE PEDIDOS)
# ============================================================================
@app.route("/processar_venda", methods=["POST"])
def processar_venda():
    """
    Endpoint HTTP POST. Realiza a validação do Bearer Token, previne a
    duplicidade de contratos e comanda o motor de ingestão Selenium.
    """
    auth_header = request.headers.get("Authorization")
    if auth_header != TOKEN_API:
        logger_api.warning("Tentativa de acesso não autorizada. Token: %s", auth_header)
        return jsonify({"erro": "nao_autorizado", "mensagem": "Acesso negado."}), 403

    dados = request.json
    if not dados:
        return jsonify({"erro": "payload_vazio", "mensagem": "Payload vazio."}), 400

    contrato = str(dados.get("contrato")).strip()
    vendedor = str(dados.get("vendedor")).strip()
    telefone_vendedor = str(dados.get("telefone", "")).strip()
    origem = str(dados.get("origem", "")).strip()

    logger_api.info("API RECEBIDA | Contrato: %s | Vendedor: %s", contrato, vendedor)

    with lock_api:

        # PREVINE CONDIÇÃO DE CORRIDA: Consulta a base após a porta trancada
        if verificar_contrato_registrado(contrato):
            logger_api.info("RECUSADO | O contrato %s já consta no sistema.", contrato)
            return (
                jsonify(
                    {
                        "erro": "duplicidade",
                        "mensagem": f"O contrato {contrato} já foi gravado no sistema.",
                    }
                ),
                409,
            )

        try:
            nome_planilha_vendedor = f"{PREFIXO_PLANILHA}{vendedor}"
            nome_planilha_geral = f"{PREFIXO_PLANILHA}GERAL"
            aba_atual = obter_mes_utc4()

            sheet_vend = conectar_google_sheets(nome_planilha_vendedor, aba_atual)
            sheet_geral_ano = conectar_google_sheets(
                nome_planilha_geral, NOME_ABA_GERAL
            )

            erros_infra = []
            if not sheet_vend:
                erros_infra.append(f"Planilha Vendedor ({nome_planilha_vendedor})")
            if not sheet_geral_ano:
                erros_infra.append(f"Planilha GERAL -> Aba: {NOME_ABA_GERAL}")

            if erros_infra:
                msg_erro = f"Infraestrutura inválida. Falta: {', '.join(erros_infra)}."
                logger_api.error("[ERRO INFRAESTRUTURA] %s", msg_erro)
                return jsonify({"erro": "planilha_ausente", "mensagem": msg_erro}), 404

            garantir_sessao(driver_api, ESTADO_API, logger_api)
            ESTADO_API["ultimo_keep_alive"] = time.time()

            encontrou_contrato = buscar_contrato(driver_api, contrato)
            if not encontrou_contrato:
                logger_api.warning("Contrato %s não localizado. Re-login...", contrato)
                ESTADO_API["autenticado"] = False
                garantir_sessao(driver_api, ESTADO_API, logger_api)
                encontrou_contrato = buscar_contrato(driver_api, contrato)

            if encontrou_contrato:
                dados_site = extrair_dados_completos(driver_api)

                anotou_vend = False
                anotou_geral_ano = False

                try:
                    atualizar_planilha_vendedor(sheet_vend, dados, dados_site, contrato)
                    anotou_vend = True
                except Exception as e_vend:  # pylint: disable=broad-exception-caught
                    logger_api.error("Falha na gravação do vendedor: %s", e_vend)

                try:
                    atualizar_planilha_geral(
                        sheet_geral_ano, dados, dados_site, contrato
                    )
                    anotou_geral_ano = True
                except Exception as e_gano:  # pylint: disable=broad-exception-caught
                    logger_api.error("Falha na gravação GERAL Anual: %s", e_gano)

                if anotou_vend and anotou_geral_ano:
                    texto_st = (
                        "1º Parcela Paga"
                        if dados_site.get("pago")
                        else "1º Parcela Não Paga"
                    )

                    if dados_site.get("pago"):
                        salvar_historico_concluido(
                            contrato,
                            nome_planilha_vendedor,
                            vendedor,
                            telefone_vendedor,
                            texto_st,
                        )

                    registrar_contrato_unificado(
                        contrato=contrato,
                        vendedor_nome=vendedor,
                        nome_planilha=nome_planilha_vendedor,
                        vendedor_tel=telefone_vendedor,
                        origem=origem,
                        dados_site=dados_site,
                        aba_original=aba_atual,
                    )

                    logger_api.info(
                        "[SUCESSO API] Contrato %s gravado e inserido na base unificada.",
                        contrato,
                    )

                    # [NOVO] Formatar Telefone do Cliente com DDI 55
                    tel_cliente = str(dados_site.get("telefone", ""))
                    numeros_limpos = re.sub(r"\D", "", tel_cliente)
                    tel_cliente_limpo = f"55{numeros_limpos}" if numeros_limpos else ""

                    # [A BANDEJA DE DEVOLUÇÃO PARA O SITE]
                    return (
                        jsonify(
                            {
                                "sucesso": True,
                                "status_pagamento": texto_st,
                                "planilha": nome_planilha_vendedor,
                                "nome_cliente": str(dados_site.get("nome", "-")),
                                "dados_completos": {
                                    "contrato": contrato,
                                    "vendedor": vendedor,
                                    "origem": origem,
                                    "cpf": dados_site.get("cpf"),
                                    "grupo": dados_site.get("grupo"),
                                    "cota": dados_site.get("cota"),
                                    "versao": dados_site.get("versao"),
                                    "estado_uf": dados_site.get("estado"),
                                    "valor_credito": dados_site.get("credito"),
                                    "data_venda_painel": dados_site.get("data_venda"),
                                    "primeira_parcela_paga": dados_site.get("pago"),
                                    "telefone_cliente": str(tel_cliente_limpo),
                                },
                            }
                        ),
                        200,
                    )

                else:
                    erros_gravacao = []
                    if not anotou_vend:
                        erros_gravacao.append("Planilha do Vendedor")
                    if not anotou_geral_ano:
                        erros_gravacao.append("Planilha GERAL Anual")

                    msg_falha = f"Falha na gravação física. Erro em: {', '.join(erros_gravacao)}."
                    logger_api.error("[ERRO GRAVAÇÃO] %s", msg_falha)
                    return (
                        jsonify({"erro": "falha_gravacao", "mensagem": msg_falha}),
                        500,
                    )

            logger_api.error("API RECUSADA | Contrato %s não localizado.", contrato)
            return (
                jsonify(
                    {"erro": "contrato_nao_encontrado", "mensagem": "Não localizado."}
                ),
                404,
            )

        except Exception as e_motor:  # pylint: disable=broad-exception-caught
            logger_api.error("Erro interno do motor: %s", e_motor)
            return jsonify({"erro": "erro_interno", "mensagem": str(e_motor)}), 500


# ============================================================================
# ROTINA DE BACKGROUND (HEARTBEAT / KEEP-ALIVE)
# ============================================================================
def loop_keep_alive():
    """
    Thread microscópica dedicada exclusivamente a manter a sessão ASP viva
    durante períodos de ociosidade sem vendas, poupando chamadas à API da CapSolver.
    """
    logger_api.info("Motor de Keep-Alive Iniciado.")
    while True:
        time.sleep(60)
        with lock_api:
            if ESTADO_API["autenticado"]:
                tempo_inativo = time.time() - ESTADO_API["ultimo_keep_alive"]
                if tempo_inativo > TEMPO_INATIVIDADE_MAXIMO:
                    if not manter_sessao_viva(driver_api):
                        logger_api.warning(
                            "Keep-Alive falhou. O sistema forçará re-login na próxima venda."
                        )
                        ESTADO_API["autenticado"] = False
                    else:
                        logger_api.info(
                            "    [SISTEMA] Keep-Alive executado (Sessão preservada)."
                        )

                    ESTADO_API["ultimo_keep_alive"] = time.time()


if __name__ == "__main__":
    logger_api.info(
        ">>> INICIANDO SISTEMA ENTERPRISE V6 (WAITRESS WSGI + SELENIUM) <<<"
    )

    driver_api = iniciar_navegador()

    try:
        logger_api.info("Executando login imediato na inicialização do sistema...")
        garantir_sessao(driver_api, ESTADO_API, logger_api)
    except Exception as e_inicial:  # pylint: disable=broad-exception-caught
        logger_api.error(
            "Não foi possível acessar o portal de forma automatizada: %s", e_inicial
        )

    # Inicia a Thread do Keep-Alive
    threading.Thread(target=loop_keep_alive, daemon=True).start()

    logger_api.info(
        "Servidor WSGI de produção (Waitress) iniciado com sucesso na porta 5000."
    )
    serve(app, host="0.0.0.0", port=5000)
