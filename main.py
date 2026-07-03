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
    TEMPO_INATIVIDADE_MAXIMO,
    PREFIXO_PLANILHA,
    NOME_ABA_GERAL,
    obter_mes_utc4,
    conectar_google_sheets,
    atualizar_planilha_vendedor,
    atualizar_planilha_geral,
    adicionar_para_reanalise,
    carregar_pendentes,
    encontrar_linha_do_contrato,
    salvar_pendentes,
    salvar_historico_concluido,
    verificar_contrato_registrado,
    carregar_adimplencia,
    salvar_adimplencia,
    migrar_para_adimplencia,
    buscar_status_offline_regex,
    registrar_adimplencia_planilhas,
    registrar_apenas_situacao_cliente,
    CONFIG,
)

from motor_navegacao import (
    iniciar_navegador,
    buscar_contrato,
    extrair_dados_completos,
    manter_sessao_viva,
    fazer_login_com_ia,
    buscar_contrato_avancado,
    raspar_dados_adimplencia,
    baixar_relatorios_cache,
)

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

# Logger Isolado (CRM e Adimplência de Longo Prazo)
logger_crm = logging.getLogger("EnterpriseCRM")
logger_crm.setLevel(logging.INFO)
handler_crm = RotatingFileHandler(
    "files/crm_sistema.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
handler_crm.setFormatter(log_formatter)
logger_crm.addHandler(handler_crm)
logger_crm.addHandler(logging.StreamHandler())

# ============================================================================
# INICIALIZAÇÃO DA API E ESTADO GLOBAL
# ============================================================================
app = Flask(__name__)

# Instâncias independentes para concorrência
driver_api = None
driver_crm = None

# Bloqueio estrito apenas para os processos que partilham o driver_api
lock_api = threading.Lock()
TOKEN_API_ESPERADO = "Bearer CHAVE_SECRETA_ENTERPRISE_V6"

ESTADO_API = {
    "autenticado": False,
    "ultimo_keep_alive": time.time(),
    "ultimo_login": 0,
}

ESTADO_CRM = {
    "autenticado": False,
    "ultimo_keep_alive": time.time(),
    "ultimo_login": 0,
    "ultima_sincronizacao": 0,
    "ultima_sincronizacao_diaria": 0,
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
            logger_alvo.info("[LIBERADO] Acesso restabelecido via IA.")
            estado_alvo["autenticado"] = True
            estado_alvo["ultimo_keep_alive"] = agora
            estado_alvo["ultimo_login"] = agora
        else:
            logger_alvo.error("[BARRADO] Falha crítica de login.")
            raise ConnectionError("Falha de autenticação no portal.")


# ============================================================================
# ROTA DA API (RECEPÇÃO DE PEDIDOS DO NODE.JS)
# ============================================================================
@app.route("/processar_venda", methods=["POST"])
def processar_venda():
    """Endpoint HTTP POST. Valida o Bearer Token e cadastra a venda no sistema."""
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
            sheet_geral_mes = conectar_google_sheets(nome_planilha_geral, aba_atual)

            if not sheet_vend or not sheet_geral_ano or not sheet_geral_mes:
                logger_api.error("[ERRO INFRAESTRUTURA] Planilhas inacessíveis.")
                return (
                    jsonify({"erro": "planilha_ausente", "mensagem": "Erro de I/O."}),
                    404,
                )

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
                anotou_geral_mes = False

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

                try:
                    atualizar_planilha_geral(
                        sheet_geral_mes, dados, dados_site, contrato
                    )
                    anotou_geral_mes = True
                except Exception as e_gmes:  # pylint: disable=broad-exception-caught
                    logger_api.error("Falha na gravação GERAL Mensal: %s", e_gmes)

                if anotou_vend and anotou_geral_ano and anotou_geral_mes:
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
                    logger_api.info("[SUCESSO API] Contrato %s gravado.", contrato)

                    if not dados_site.get("pago"):
                        adicionar_para_reanalise(
                            contrato,
                            vendedor,
                            telefone_vendedor,
                            nome_planilha_vendedor,
                            origem,
                            dados,
                            aba_atual,
                        )
                    else:
                        info_migracao = {
                            "vendedor_nome": vendedor,
                            "nome_planilha": nome_planilha_vendedor,
                            "aba_original": aba_atual,
                        }
                        migrar_para_adimplencia(
                            contrato,
                            info_migracao,
                            dados_site.get("grupo", ""),
                            dados_site.get("cota", ""),
                        )

                    return (
                        jsonify(
                            {
                                "sucesso": True,
                                "status": texto_st,
                                "planilha": nome_planilha_vendedor,
                            }
                        ),
                        200,
                    )

            logger_api.error("API RECUSADA | Contrato %s não localizado.", contrato)
            return (
                jsonify({"erro": "nao_encontrado", "mensagem": "Não localizado."}),
                404,
            )

        except Exception as e_motor:  # pylint: disable=broad-exception-caught
            logger_api.error("Erro interno do motor: %s", e_motor)
            return jsonify({"erro": "erro_interno", "mensagem": str(e_motor)}), 500


# ============================================================================
# ROTINA DE BACKGROUND (REANÁLISE DE 1 HORA / 30 DIAS)
# ============================================================================
def loop_reanalise_background():
    """Varre a fila de 1ª parcela, operando exclusivamente na instância driver_api."""
    logger_api.info("Motor de Reanálise Independente Iniciado.")

    while True:
        time.sleep(60)

        try:
            pendentes = carregar_pendentes()
            mudou_pendentes = False

            if pendentes:
                for contrato, info in list(pendentes.items()):
                    agora = time.time()
                    ultima_verificacao = info.get("ultima_verificacao", 0)
                    data_inclusao = info.get("data_inclusao", agora)

                    if "data_inclusao" not in info:
                        info["data_inclusao"] = data_inclusao
                        mudou_pendentes = True

                    if agora - data_inclusao > 2592000:
                        logger_api.warning("EXPIROU | Contrato %s removido.", contrato)
                        del pendentes[contrato]
                        mudou_pendentes = True
                        continue

                    if agora - ultima_verificacao < 3600:
                        continue

                    with lock_api:
                        if not ESTADO_API["autenticado"]:
                            try:
                                garantir_sessao(driver_api, ESTADO_API, logger_api)
                            except Exception:  # pylint: disable=broad-exception-caught
                                continue

                        ESTADO_API["ultimo_keep_alive"] = time.time()
                        info["tentativas"] = info.get("tentativas", 0) + 1
                        info["ultima_verificacao"] = time.time()
                        mudou_pendentes = True

                        logger_api.info("REANÁLISE | Verificando %s...", contrato)

                        try:
                            if buscar_contrato(driver_api, contrato):
                                dados_completos = extrair_dados_completos(driver_api)

                                if dados_completos.get("pago"):
                                    logger_api.info(
                                        "PAGAMENTO DETECTADO | Contrato %s.", contrato
                                    )
                                    aba_salva = info.get(
                                        "aba_original", obter_mes_utc4()
                                    )

                                    sheet_v = conectar_google_sheets(
                                        info["nome_planilha"], aba_salva
                                    )
                                    sheet_g_ano = conectar_google_sheets(
                                        f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                                    )
                                    sheet_g_mes = conectar_google_sheets(
                                        f"{PREFIXO_PLANILHA}GERAL", aba_salva
                                    )

                                    if sheet_v and sheet_g_ano and sheet_g_mes:
                                        l_v = encontrar_linha_do_contrato(
                                            sheet_v, contrato, 13
                                        )
                                        l_g_ano = encontrar_linha_do_contrato(
                                            sheet_g_ano, contrato, 12
                                        )
                                        l_g_mes = encontrar_linha_do_contrato(
                                            sheet_g_mes, contrato, 12
                                        )

                                        if l_v and l_g_ano and l_g_mes:
                                            sheet_v.update_cell(
                                                l_v, 2, "1º Parcela Paga"
                                            )
                                            sheet_g_ano.update_cell(
                                                l_g_ano, 1, "1º Parcela Paga"
                                            )
                                            sheet_g_mes.update_cell(
                                                l_g_mes, 1, "1º Parcela Paga"
                                            )

                                            salvar_historico_concluido(
                                                contrato,
                                                info["nome_planilha"],
                                                info["vendedor_nome"],
                                                info["vendedor_tel"],
                                                "1º Parcela Paga",
                                            )
                                            migrar_para_adimplencia(
                                                contrato,
                                                info,
                                                dados_completos.get("grupo", ""),
                                                dados_completos.get("cota", ""),
                                            )
                                            del pendentes[contrato]
                        except (
                            Exception  # pylint: disable=broad-exception-caught
                        ) as e_indiv:
                            logger_api.error(
                                "Erro ao verificar pendente %s: %s", contrato, e_indiv
                            )

            if mudou_pendentes:
                salvar_pendentes(pendentes)

            with lock_api:
                if ESTADO_API["autenticado"]:
                    tempo_inativo = time.time() - ESTADO_API["ultimo_keep_alive"]
                    if tempo_inativo > TEMPO_INATIVIDADE_MAXIMO:
                        if not manter_sessao_viva(driver_api):
                            ESTADO_API["autenticado"] = False
                        ESTADO_API["ultimo_keep_alive"] = time.time()

        except Exception as e_bg:  # pylint: disable=broad-exception-caught
            logger_api.error("Erro no loop de background: %s", e_bg)


# ============================================================================
# CRM DE ADIMPLÊNCIA (CACHE DIFFERENTIAL UPDATE)
# ============================================================================
def loop_reanalise_adimplencia():
    """Motor de CRM isolado operando na instância driver_crm. Executa Delta Updates."""
    logger_crm.info("Motor de CRM (Gestão de Adimplência e Delta Cache) Iniciado.")
    while True:
        time.sleep(CONFIG.get("INTERVALO_REANALISE_ADIMPLENCIA", 14400))

        agora = time.time()
        intervalo_sinc = CONFIG.get("INTERVALO_SINC_RELATORIOS", 3600)

        # Atualização dos Ficheiros Estáticos
        if agora - ESTADO_CRM.get("ultima_sincronizacao_diaria", 0) >= 86400:
            if not ESTADO_CRM["autenticado"]:
                try:
                    garantir_sessao(driver_crm, ESTADO_CRM, logger_crm)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
            if ESTADO_CRM["autenticado"]:
                baixar_relatorios_cache(driver_crm, tipo="diario")
                ESTADO_CRM["ultima_sincronizacao_diaria"] = time.time()
                ESTADO_CRM["ultima_sincronizacao"] = time.time()

        elif agora - ESTADO_CRM.get("ultima_sincronizacao", 0) >= intervalo_sinc:
            if not ESTADO_CRM["autenticado"]:
                try:
                    garantir_sessao(driver_crm, ESTADO_CRM, logger_crm)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
            if ESTADO_CRM["autenticado"]:
                baixar_relatorios_cache(driver_crm, tipo="frequente")
                ESTADO_CRM["ultima_sincronizacao"] = time.time()

        adimplencia = carregar_adimplencia()

        for contrato, info in adimplencia.items():
            mudou_json = False

            # Parsing Estrutural Rápido
            status_off_rapido = buscar_status_offline_regex(
                info.get("grupo"), info.get("cota"), info.get("cota_versao")
            )

            if status_off_rapido:
                if info.get("monitorar", True) is not False:
                    logger_crm.info(
                        "[CRM] Morte Confirmada: Contrato %s consta offline.", contrato
                    )

                    sheet_v = conectar_google_sheets(
                        info["nome_planilha"], info["aba_original"]
                    )
                    sheet_g_ano = conectar_google_sheets(
                        f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                    )
                    sheet_g_mes = conectar_google_sheets(
                        f"{PREFIXO_PLANILHA}GERAL", info["aba_original"]
                    )

                    if sheet_v and sheet_g_ano and sheet_g_mes:
                        registrar_adimplencia_planilhas(
                            sheet_v,
                            contrato,
                            "CANCELADO/DESIST",
                            0,
                            status_off_rapido,
                            13,
                        )
                        registrar_adimplencia_planilhas(
                            sheet_g_ano,
                            contrato,
                            "CANCELADO/DESIST",
                            0,
                            status_off_rapido,
                            12,
                        )
                        registrar_adimplencia_planilhas(
                            sheet_g_mes,
                            contrato,
                            "CANCELADO/DESIST",
                            0,
                            status_off_rapido,
                            12,
                        )

                        info["monitorar"] = False
                        info["ultimo_status"] = "CANCELADO/DESIST"
                        mudou_json = True

                if mudou_json:
                    salvar_adimplencia(adimplencia)
                continue

            if not info.get("monitorar", True):
                logger_crm.info("[CRM] Ressurreição Detectada: Contrato %s.", contrato)
                info["monitorar"] = True
                mudou_json = True

            try:
                if not ESTADO_CRM["autenticado"]:
                    garantir_sessao(driver_crm, ESTADO_CRM, logger_crm)

                logger_crm.info(
                    "[CRM] Verificando Adimplência ao vivo: %s...", contrato
                )
                ESTADO_CRM["ultimo_keep_alive"] = time.time()

                encontrou, motivo, cota_versao_site = buscar_contrato_avancado(
                    driver_crm, contrato
                )

                if encontrou:
                    if cota_versao_site and info.get("cota_versao") != cota_versao_site:
                        info["cota_versao"] = cota_versao_site
                        mudou_json = True

                    if info.get("data_limbo") is not None:
                        info["data_limbo"] = None
                        mudou_json = True

                    dados_adim = raspar_dados_adimplencia(driver_crm)
                    st_atual = dados_adim.get("status_pagamento")
                    pc_atual = dados_adim.get("parcelas_pagas")

                    # ================================================================
                    # DELTA CACHE: Submissão para a API apenas se o estado for alterado
                    # ================================================================
                    st_anterior = info.get("ultimo_status")
                    pc_anterior = info.get("ultimas_parcelas")

                    if st_atual != st_anterior or pc_atual != pc_anterior:
                        logger_crm.info(
                            "   -> [CRM] Alteração detectada no contrato %s. Atualizando nuvem.",
                            contrato,
                        )

                        sheet_v = conectar_google_sheets(
                            info["nome_planilha"], info["aba_original"]
                        )
                        sheet_g_ano = conectar_google_sheets(
                            f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                        )
                        sheet_g_mes = conectar_google_sheets(
                            f"{PREFIXO_PLANILHA}GERAL", info["aba_original"]
                        )

                        if sheet_v and sheet_g_ano and sheet_g_mes:
                            registrar_adimplencia_planilhas(
                                sheet_v, contrato, st_atual, pc_atual, "Ativo", 13
                            )
                            registrar_adimplencia_planilhas(
                                sheet_g_ano, contrato, st_atual, pc_atual, "Ativo", 12
                            )
                            registrar_adimplencia_planilhas(
                                sheet_g_mes, contrato, st_atual, pc_atual, "Ativo", 12
                            )

                            info["ultimo_status"] = st_atual
                            info["ultimas_parcelas"] = pc_atual
                            mudou_json = True
                    else:
                        logger_crm.info(
                            "   -> [CRM] Dados intactos. Ignorando a requisição à API."
                        )

                else:
                    if motivo == "SESSAO_CAIU":
                        logger_crm.error(
                            "[SISTEMA] Queda de rede detetada. Pausando ciclo."
                        )
                        ESTADO_CRM["autenticado"] = False

                    elif motivo == "NAO_ENCONTRADO":
                        if not info.get("data_limbo"):
                            info["data_limbo"] = time.time()
                            logger_crm.warning("[CRM] %s entrou no Limbo.", contrato)
                            mudou_json = True

                            sheet_v = conectar_google_sheets(
                                info["nome_planilha"], info["aba_original"]
                            )
                            sheet_g_ano = conectar_google_sheets(
                                f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                            )
                            sheet_g_mes = conectar_google_sheets(
                                f"{PREFIXO_PLANILHA}GERAL", info["aba_original"]
                            )

                            if sheet_v and sheet_g_ano and sheet_g_mes:
                                registrar_apenas_situacao_cliente(
                                    sheet_v, contrato, "Inacessível", 13
                                )
                                registrar_apenas_situacao_cliente(
                                    sheet_g_ano, contrato, "Inacessível", 12
                                )
                                registrar_apenas_situacao_cliente(
                                    sheet_g_mes, contrato, "Inacessível", 12
                                )
                        else:
                            dias = (time.time() - info["data_limbo"]) / 86400
                            limite = CONFIG.get("LIMITE_DIAS_DESATIVACAO", 45)
                            if dias > limite:
                                logger_crm.error(
                                    "[CRM] Contrato %s expirou no Limbo.", contrato
                                )
                                info["monitorar"] = False
                                mudou_json = True

                                sheet_v = conectar_google_sheets(
                                    info["nome_planilha"], info["aba_original"]
                                )
                                sheet_g_ano = conectar_google_sheets(
                                    f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                                )
                                sheet_g_mes = conectar_google_sheets(
                                    f"{PREFIXO_PLANILHA}GERAL", info["aba_original"]
                                )

                                if sheet_v and sheet_g_ano and sheet_g_mes:
                                    registrar_apenas_situacao_cliente(
                                        sheet_v, contrato, "Inacessível", 13
                                    )
                                    registrar_apenas_situacao_cliente(
                                        sheet_g_ano, contrato, "Inacessível", 12
                                    )
                                    registrar_apenas_situacao_cliente(
                                        sheet_g_mes, contrato, "Inacessível", 12
                                    )

            except Exception as e_indiv:  # pylint: disable=broad-exception-caught
                logger_crm.error(
                    "[CRM] Erro ao verificar contrato %s: %s", contrato, e_indiv
                )

            # Gravação imediata a cada iteração para blindagem de dados
            if mudou_json:
                salvar_adimplencia(adimplencia)


# ============================================================================
# PONTO DE ENTRADA DA APLICAÇÃO (SERVIDOR WSGI PRODUCTION)
# ============================================================================
if __name__ == "__main__":
    logger_api.info(">>> INICIANDO SISTEMA ENTERPRISE V6 (WAITRESS WSGI) <<<")

    logger_api.info("Instanciando o Motor Alpha (API e Reanálise)...")
    driver_api = iniciar_navegador()

    logger_api.info("Instanciando o Motor Beta (Gestão de CRM)...")
    driver_crm = iniciar_navegador()

    try:
        logger_api.info("Efetuando autenticação do Motor Alpha...")
        garantir_sessao(driver_api, ESTADO_API, logger_api)
        logger_api.info("Efetuando autenticação do Motor Beta...")
        garantir_sessao(driver_crm, ESTADO_CRM, logger_crm)
    except Exception as e_inicial:  # pylint: disable=broad-exception-caught
        logger_api.error("Falha na autenticação inicial: %s", e_inicial)

    # Inicia as threads operacionais
    threading.Thread(target=loop_reanalise_background, daemon=True).start()
    threading.Thread(target=loop_reanalise_adimplencia, daemon=True).start()

    logger_api.info("Servidor WSGI (Waitress) iniciado na porta 5000.")
    serve(app, host="0.0.0.0", port=5000)
