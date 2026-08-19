"""
Motor Principal de Automação de Consórcio V6 (Enterprise).

Implementa arquitetura de microsserviços internos com instâncias concorrentes
do WebDriver, gestão diferencial de estado (Delta Updates) e segregação de logs.
"""

import time
import threading
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify
from waitress import serve

from gerador_dados import (
    PREFIXO_PLANILHA,
    NOME_ABA_GERAL,
    obter_mes_utc4,
    conectar_google_sheets,
    atualizar_planilha_vendedor,
    atualizar_planilha_geral,
    carregar_pendentes,
    salvar_pendentes,
    salvar_historico_concluido,
    verificar_contrato_registrado,
    adicionar_para_reanalise,
    TEMPO_INATIVIDADE_MAXIMO,
)

from motor_navegacao import (
    iniciar_navegador,
    buscar_contrato,
    extrair_dados_completos,
    manter_sessao_viva,
    fazer_login_com_ia,
)

from cda_modulos import migrar_para_adimplencia

# ============================================================================
# CONFIGURAÇÃO DE LOGGING ESTRUTURADO ROTATIVO (SEGREGAÇÃO)
# ============================================================================
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# Logger Principal (API e Background de 1ª Parcela)
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

# Instâncias independentes para concorrência
driver_api = None

# Bloqueio estrito apenas para os processos que partilham o driver_api
lock_api = threading.Lock()
TOKEN_API_ESPERADO = "Bearer CHAVE_SECRETA_ENTERPRISE_V6"

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
    duplicidade de contratos e comanda o motor Selenium.
    """
    auth_header = request.headers.get("Authorization")
    if auth_header != TOKEN_API_ESPERADO:
        logger_api.warning("Tentativa de acesso não autorizada. Token: %s", auth_header)
        return jsonify({"erro": "nao_autorizado", "mensagem": "Acesso negado."}), 403

    dados = request.json
    if not dados:
        return jsonify({"erro": "payload_vazio", "mensagem": "Payload vazio."}), 400

    contrato = str(dados.get("contrato")).strip()
    vendedor = str(dados.get("vendedor")).strip()
    telefone_vendedor = str(dados.get("telefone")).strip()
    origem = str(dados.get("origem", "")).strip()

    logger_api.info("API RECEBIDA | Contrato: %s | Vendedor: %s", contrato, vendedor)

    if verificar_contrato_registrado(contrato):
        logger_api.info("RECUSADO | O contrato %s já consta no sistema.", contrato)
        return jsonify({"erro": "duplicidade", "mensagem": "Já processado."}), 409

    with lock_api:
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

                    salvar_historico_concluido(
                        contrato,
                        nome_planilha_vendedor,
                        vendedor,
                        telefone_vendedor,
                        texto_st,
                    )

                    logger_api.info(
                        "[SUCESSO API] Contrato %s gravado no sistema.", contrato
                    )

                    if not dados_site.get("pago"):
                        adicionar_para_reanalise(
                            contrato,
                            vendedor,
                            nome_planilha_vendedor,
                            telefone_vendedor,
                            origem,
                            dados,
                            aba_atual,
                            nome_cliente=dados_site.get("nome"),
                            versao=dados_site.get("versao"),
                        )
                    else:
                        migrar_para_adimplencia(
                            contrato,
                            {
                                "vendedor_nome": vendedor,
                                "nome_planilha": nome_planilha_vendedor,
                                "aba_original": aba_atual,
                                "nome": dados_site.get("nome"),
                                "versao": dados_site.get("versao"),
                                "dados_originais": dados,
                            },
                            str(dados_site.get("grupo", "")),
                            str(dados_site.get("cota", "")),
                        )

                    return (
                        jsonify(
                            {
                                "sucesso": True,
                                "status_pagamento": texto_st,
                                "planilha": nome_planilha_vendedor,
                                "nome_cliente": str(dados_site.get("nome", "-")),
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
# ROTINA DE BACKGROUND (REANÁLISE DE 1 HORA / 30 DIAS)
# ============================================================================
def loop_reanalise_background():
    """
    Varre a fila de 1ª parcela, operando exclusivamente na instância driver_api.
    Protegido contra condições de corrida através do recarregamento de estado
    diretamente na memória física por iteração.
    """
    logger_api.info("Motor de Reanálise Independente Iniciado.")

    while True:
        time.sleep(60)

        try:
            # 1. Limpeza de Expirados (Rápida e Isolada)
            pend_limpeza = carregar_pendentes()
            if not pend_limpeza:
                continue

            agora = time.time()
            mudou_limpeza = False
            for contrato, info in list(pend_limpeza.items()):
                data_inclusao = info.get("data_inclusao", agora)
                if "data_inclusao" not in info:
                    info["data_inclusao"] = data_inclusao
                    mudou_limpeza = True

                # Expira ao fim de 30 dias
                if agora - data_inclusao > 2592000:
                    logger_api.warning(
                        "EXPIROU | Contrato %s atingiu limite de 30 dias. Removido.",
                        contrato,
                    )
                    del pend_limpeza[contrato]
                    mudou_limpeza = True

            if mudou_limpeza:
                salvar_pendentes(pend_limpeza)

            # 2. Varredura Protegida Contra Condição de Corrida
            chaves_para_verificar = list(carregar_pendentes().keys())

            for contrato in chaves_para_verificar:
                pendentes_frescos = carregar_pendentes()

                if contrato not in pendentes_frescos:
                    continue

                info = pendentes_frescos[contrato]
                ultima_verificacao = info.get("ultima_verificacao", 0)

                if time.time() - ultima_verificacao < 3600:
                    continue

                with lock_api:
                    if not ESTADO_API["autenticado"]:
                        try:
                            garantir_sessao(driver_api, ESTADO_API, logger_api)
                        except Exception:  # pylint: disable=broad-exception-caught
                            continue

                    ESTADO_API["ultimo_keep_alive"] = time.time()
                    logger_api.info("REANÁLISE | Verificando %s...", contrato)

                    sucesso_verificacao = False
                    pago = False
                    dados_completos = None

                    try:
                        if buscar_contrato(driver_api, contrato):
                            dados_completos = extrair_dados_completos(driver_api)
                            if dados_completos and dados_completos.get("pago"):
                                pago = True
                            sucesso_verificacao = True
                        else:
                            logger_api.warning(
                                "NÃO ENCONTRADO | Cota %s temporariamente indisponível no portal.",
                                contrato,
                            )
                            info["falhas_site"] = info.get("falhas_site", 0) + 1
                            pendentes_frescos[contrato]["falhas_site"] = info[
                                "falhas_site"
                            ]
                            pendentes_frescos[contrato][
                                "ultima_verificacao"
                            ] = time.time()
                            salvar_pendentes(pendentes_frescos)
                            sucesso_verificacao = False
                    except (
                        Exception  # pylint: disable=broad-exception-caught
                    ) as e_indiv:
                        logger_api.error(
                            "Erro ao verificar pendente %s: %s", contrato, e_indiv
                        )

                    if sucesso_verificacao:
                        pend_final = carregar_pendentes()
                        if contrato in pend_final:
                            if pago:
                                logger_api.info(
                                    "PAGAMENTO DETECTADO | Contrato %s.", contrato
                                )

                                aba_salva = info.get("aba_original", obter_mes_utc4())

                                from gerador_dados import encontrar_linha_do_contrato

                                sheet_v = conectar_google_sheets(
                                    info["nome_planilha"], aba_salva
                                )
                                sheet_g_ano = conectar_google_sheets(
                                    f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                                )

                                if sheet_v and sheet_g_ano:
                                    l_v = encontrar_linha_do_contrato(
                                        sheet_v, contrato, 13
                                    )
                                    l_g_ano = encontrar_linha_do_contrato(
                                        sheet_g_ano, contrato, 12
                                    )

                                    if l_v and l_g_ano:
                                        try:
                                            sheet_v.update_cell(
                                                l_v, 2, "1º Parcela Paga"
                                            )
                                            sheet_g_ano.update_cell(
                                                l_g_ano, 1, "1º Parcela Paga"
                                            )

                                            salvar_historico_concluido(
                                                contrato,
                                                info["nome_planilha"],
                                                info["vendedor_nome"],
                                                info["vendedor_tel"],
                                                "1º Parcela Paga (Reanálise)",
                                            )
                                            migrar_para_adimplencia(
                                                contrato,
                                                info,
                                                str(dados_completos.get("grupo", "")),
                                                str(dados_completos.get("cota", "")),
                                            )
                                            del pend_final[contrato]
                                            logger_api.info(
                                                "[SUCESSO REANÁLISE] Contrato %s transferido ao CDA.",
                                                contrato,
                                            )
                                        except (
                                            Exception  # pylint: disable=broad-exception-caught
                                        ) as e_sheet:
                                            logger_api.error(
                                                "Falha ao gravar sucesso do contrato %s: %s",
                                                contrato,
                                                e_sheet,
                                            )
                            else:
                                pend_final[contrato]["ultima_verificacao"] = time.time()
                                pend_final[contrato]["tentativas"] = (
                                    pend_final[contrato].get("tentativas", 0) + 1
                                )

                            salvar_pendentes(pend_final)

            with lock_api:
                if ESTADO_API["autenticado"]:
                    tempo_inativo = time.time() - ESTADO_API["ultimo_keep_alive"]
                    if tempo_inativo > TEMPO_INATIVIDADE_MAXIMO:
                        if not manter_sessao_viva(driver_api):
                            logger_api.warning(
                                "Keep-Alive falhou. Necessário Relogin futuro."
                            )
                            ESTADO_API["autenticado"] = False
                        ESTADO_API["ultimo_keep_alive"] = time.time()

        except Exception as e_bg:  # pylint: disable=broad-exception-caught
            logger_api.error("Erro no loop de background: %s", str(e_bg))


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

    threading.Thread(target=loop_reanalise_background, daemon=True).start()

    logger_api.info(
        "Servidor WSGI de produção (Waitress) iniciado com sucesso na porta 5000."
    )
    serve(app, host="0.0.0.0", port=5000)
