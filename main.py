"""
Motor Principal de Automação de Consórcio V6 (Enterprise).

Implementa autenticação Bearer Token, logging estruturado rotativo, integração
com o servidor de produção Waitress e mantém o controle de concorrência com
o Selenium através de travas de thread (Locks).
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
# CONFIGURAÇÃO DE LOGGING ESTRUTURADO ROTATIVO
# ============================================================================
logger = logging.getLogger("EnterpriseBot")
logger.setLevel(logging.INFO)

log_handler = RotatingFileHandler(
    "files/python_sistema.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
log_handler.setFormatter(log_formatter)
logger.addHandler(log_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# ============================================================================
# INICIALIZAÇÃO DA API E ESTADO GLOBAL
# ============================================================================
app = Flask(__name__)
driver_global = None

navegador_lock = threading.Lock()
TOKEN_API_ESPERADO = "Bearer CHAVE_SECRETA_ENTERPRISE_V6"

ESTADO = {
    "autenticado": False,
    "ultimo_keep_alive": time.time(),
    "ultimo_login": 0,
    "ultima_sincronizacao": 0,
    "ultima_sincronizacao_diaria": 0,
}


def garantir_sessao():
    """Garante o acesso e efetua login preventivo após 4 horas de ciclo."""
    agora = time.time()
    precisa_relogin = (agora - ESTADO.get("ultimo_login", 0)) > 14400

    if not ESTADO["autenticado"] or precisa_relogin:
        if precisa_relogin and ESTADO["autenticado"]:
            logger.info(
                "[SISTEMA] Ciclo de 4 horas atingido. Executando re-login preventivo."
            )

        if fazer_login_com_ia(driver_global):
            logger.info("[LIBERADO] Acesso restabelecido via IA.")
            ESTADO["autenticado"] = True
            ESTADO["ultimo_keep_alive"] = agora
            ESTADO["ultimo_login"] = agora
        else:
            logger.error("[BARRADO] Falha crítica de login.")
            raise ConnectionError("Falha de autenticação no portal Autocred.")


# ============================================================================
# ROTA DA API (RECEPÇÃO DE PEDIDOS DO NODE.JS)
# ============================================================================
@app.route("/processar_venda", methods=["POST"])
def processar_venda():
    """Endpoint HTTP POST. Valida o Bearer Token e cadastra a venda no sistema."""
    auth_header = request.headers.get("Authorization")
    if auth_header != TOKEN_API_ESPERADO:
        logger.warning("Tentativa de acesso não autorizada. Token: %s", auth_header)
        return (
            jsonify({"erro": "nao_autorizado", "mensagem": "Acesso não autorizado."}),
            403,
        )

    dados = request.json
    if not dados:
        return (
            jsonify(
                {
                    "erro": "payload_vazio",
                    "mensagem": "O payload da requisição está vazio.",
                }
            ),
            400,
        )

    contrato = str(dados.get("contrato")).strip()
    vendedor = str(dados.get("vendedor")).strip()
    telefone_vendedor = str(dados.get("telefone")).strip()
    origem = str(dados.get("origem", "")).strip()

    logger.info("API RECEBIDA | Contrato: %s | Vendedor: %s", contrato, vendedor)

    if verificar_contrato_registrado(contrato):
        logger.info("RECUSADO | O contrato %s já consta no sistema.", contrato)
        return (
            jsonify(
                {
                    "erro": "duplicidade",
                    "mensagem": f"O contrato {contrato} já foi processado e gravado.",
                }
            ),
            409,
        )

    with navegador_lock:
        try:
            nome_planilha_vendedor = f"{PREFIXO_PLANILHA}{vendedor}"
            nome_planilha_geral = f"{PREFIXO_PLANILHA}GERAL"
            aba_atual = obter_mes_utc4()

            sheet_vend = conectar_google_sheets(nome_planilha_vendedor, aba_atual)
            sheet_geral_ano = conectar_google_sheets(
                nome_planilha_geral, NOME_ABA_GERAL
            )
            sheet_geral_mes = conectar_google_sheets(nome_planilha_geral, aba_atual)

            erros_infra = []
            if not sheet_vend:
                erros_infra.append(f"Planilha Vendedor ({nome_planilha_vendedor})")
            if not sheet_geral_ano:
                erros_infra.append(f"Planilha GERAL -> Aba: {NOME_ABA_GERAL}")
            if not sheet_geral_mes:
                erros_infra.append(f"Planilha GERAL -> Aba: {aba_atual}")

            if erros_infra:
                msg_erro = (
                    "Infraestrutura inválida. Não foi possível localizar: "
                    f"{', '.join(erros_infra)}."
                )
                logger.error("[ERRO INFRAESTRUTURA] %s", msg_erro)
                return jsonify({"erro": "planilha_ausente", "mensagem": msg_erro}), 404

            garantir_sessao()
            ESTADO["ultimo_keep_alive"] = time.time()

            encontrou_contrato = buscar_contrato(driver_global, contrato)
            if not encontrou_contrato:
                logger.warning(
                    "Contrato %s não localizado. Acionando re-login...", contrato
                )
                ESTADO["autenticado"] = False
                garantir_sessao()
                encontrou_contrato = buscar_contrato(driver_global, contrato)

            if encontrou_contrato:
                dados_site = extrair_dados_completos(driver_global)
                anotou_vend = False
                anotou_geral_ano = False
                anotou_geral_mes = False

                try:
                    atualizar_planilha_vendedor(sheet_vend, dados, dados_site, contrato)
                    anotou_vend = True
                except Exception as e_vend:  # pylint: disable=broad-exception-caught
                    logger.error("Falha na gravação do vendedor: %s", e_vend)

                try:
                    atualizar_planilha_geral(
                        sheet_geral_ano, dados, dados_site, contrato
                    )
                    anotou_geral_ano = True
                except Exception as e_gano:  # pylint: disable=broad-exception-caught
                    logger.error("Falha na gravação GERAL Anual: %s", e_gano)

                try:
                    atualizar_planilha_geral(
                        sheet_geral_mes, dados, dados_site, contrato
                    )
                    anotou_geral_mes = True
                except Exception as e_gmes:  # pylint: disable=broad-exception-caught
                    logger.error("Falha na gravação GERAL Mensal: %s", e_gmes)

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
                    logger.info("[SUCESSO API] Contrato %s gravado.", contrato)

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
                                "status_pagamento": texto_st,
                                "planilha": nome_planilha_vendedor,
                            }
                        ),
                        200,
                    )

                erros_gravacao = []
                if not anotou_vend:
                    erros_gravacao.append("Planilha do Vendedor")
                if not anotou_geral_ano:
                    erros_gravacao.append("Planilha GERAL Anual")
                if not anotou_geral_mes:
                    erros_gravacao.append("Planilha GERAL Mensal")

                msg_falha = (
                    "Falha na gravação física. Erro ao atualizar: "
                    f"{', '.join(erros_gravacao)}."
                )
                logger.error("[ERRO GRAVAÇÃO] %s", msg_falha)
                return jsonify({"erro": "falha_gravacao", "mensagem": msg_falha}), 500

            logger.error("API RECUSADA | Contrato %s não localizado.", contrato)
            return (
                jsonify(
                    {
                        "erro": "contrato_nao_encontrado",
                        "mensagem": f"O contrato {contrato} não foi localizado.",
                    }
                ),
                404,
            )

        except Exception as e_motor:  # pylint: disable=broad-exception-caught
            logger.error("Erro interno do motor: %s", e_motor)
            return (
                jsonify(
                    {
                        "erro": "erro_interno",
                        "mensagem": f"Erro interno no motor Python: {e_motor}",
                    }
                ),
                500,
            )


# ============================================================================
# ROTINA DE BACKGROUND (REANÁLISE DE 1 HORA / 30 DIAS)
# ============================================================================
def loop_reanalise_background():
    """Varre a fila de 1ª parcela, mantendo a sessão viva em ociosidade."""
    logger.info("Motor de Reanálise Independente Iniciado.")

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
                        logger.warning("EXPIROU | Contrato %s removido.", contrato)
                        del pendentes[contrato]
                        mudou_pendentes = True
                        continue

                    if agora - ultima_verificacao < 3600:
                        continue

                    with navegador_lock:
                        if not ESTADO["autenticado"]:
                            try:
                                garantir_sessao()
                            except Exception:  # pylint: disable=broad-exception-caught
                                continue

                        ESTADO["ultimo_keep_alive"] = time.time()
                        info["tentativas"] = info.get("tentativas", 0) + 1
                        info["ultima_verificacao"] = time.time()
                        mudou_pendentes = True

                        logger.info("REANÁLISE | Verificando %s...", contrato)

                        try:
                            encontrou = buscar_contrato(driver_global, contrato)

                            if encontrou:
                                dados_completos = extrair_dados_completos(driver_global)

                                if dados_completos.get("pago"):
                                    logger.info(
                                        "PAGAMENTO DETECTADO | Contrato %s.", contrato
                                    )
                                    anotou_v = False
                                    anotou_g_ano = False
                                    anotou_g_mes = False
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
                                            try:
                                                sheet_v.update_cell(
                                                    l_v, 2, "1º Parcela Paga"
                                                )
                                                anotou_v = True
                                            except (
                                                Exception  # pylint: disable=broad-exception-caught
                                            ):
                                                pass

                                            try:
                                                sheet_g_ano.update_cell(
                                                    l_g_ano, 1, "1º Parcela Paga"
                                                )
                                                anotou_g_ano = True
                                            except (
                                                Exception  # pylint: disable=broad-exception-caught
                                            ):
                                                pass

                                            try:
                                                sheet_g_mes.update_cell(
                                                    l_g_mes, 1, "1º Parcela Paga"
                                                )
                                                anotou_g_mes = True
                                            except (
                                                Exception  # pylint: disable=broad-exception-caught
                                            ):
                                                pass

                                            if (
                                                anotou_v
                                                and anotou_g_ano
                                                and anotou_g_mes
                                            ):
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
                                    else:
                                        logger.critical(
                                            "[CRÍTICO] Planilhas corrompidas."
                                        )
                            else:
                                logger.warning(
                                    "NÃO ENCONTRADO | Cota %s indisponível.", contrato
                                )

                        except (
                            Exception  # pylint: disable=broad-exception-caught
                        ) as e_indiv:
                            logger.error(
                                "Erro ao verificar pendente %s: %s", contrato, e_indiv
                            )

            if mudou_pendentes:
                salvar_pendentes(pendentes)

            # Keep-Alive isolado, rodando independente da fila
            with navegador_lock:
                if ESTADO["autenticado"]:
                    tempo_inativo = time.time() - ESTADO["ultimo_keep_alive"]
                    if tempo_inativo > TEMPO_INATIVIDADE_MAXIMO:
                        if not manter_sessao_viva(driver_global):
                            ESTADO["autenticado"] = False
                        ESTADO["ultimo_keep_alive"] = time.time()

        except Exception as e_bg:  # pylint: disable=broad-exception-caught
            logger.error("Erro no loop de background protegido: %s", e_bg)


# ============================================================================
# NOVA THREAD: CRM DE ADIMPLÊNCIA (COM DOWNLOAD SINCRONIZADO E OTIMIZADO)
# ============================================================================
def loop_reanalise_adimplencia():
    """CRM de longo prazo com sincronização diária profunda e frequente rápida."""
    logger.info("Motor de CRM (Gestão de Adimplência) Iniciado.")
    while True:
        time.sleep(CONFIG.get("INTERVALO_REANALISE_ADIMPLENCIA", 14400))

        agora = time.time()
        intervalo_sinc = CONFIG.get("INTERVALO_SINC_RELATORIOS", 3600)

        if agora - ESTADO.get("ultima_sincronizacao_diaria", 0) >= 86400:
            with navegador_lock:
                if not ESTADO["autenticado"]:
                    try:
                        garantir_sessao()
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
                if ESTADO["autenticado"]:
                    baixar_relatorios_cache(driver_global, tipo="diario")
                    ESTADO["ultima_sincronizacao_diaria"] = time.time()
                    ESTADO["ultima_sincronizacao"] = time.time()

        elif agora - ESTADO.get("ultima_sincronizacao", 0) >= intervalo_sinc:
            with navegador_lock:
                if not ESTADO["autenticado"]:
                    try:
                        garantir_sessao()
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
                if ESTADO["autenticado"]:
                    baixar_relatorios_cache(driver_global, tipo="frequente")
                    ESTADO["ultima_sincronizacao"] = time.time()

        adimplencia = carregar_adimplencia()
        mudou = False

        for contrato, info in list(adimplencia.items()):

            # =========================================================================
            # OTIMIZAÇÃO ESTRUTURAL E REATIVAÇÃO (Parsing Offline)
            # =========================================================================

            status_off_rapido = buscar_status_offline_regex(
                info.get("grupo"), info.get("cota"), info.get("cota_versao")
            )

            if status_off_rapido:
                if info.get("monitorar", True) is not False:
                    logger.info(
                        "[CRM] Morte Confirmada: Contrato %s consta como %s offline.",
                        contrato,
                        status_off_rapido,
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
                        mudou = True

                continue

            # =========================================================================

            if not info.get("monitorar", True):
                logger.info(
                    "[CRM] Ressurreição Detectada: Contrato %s não consta mais "
                    "nos cancelamentos. Reativando rastreio.",
                    contrato,
                )
                info["monitorar"] = True
                mudou = True

            with navegador_lock:
                try:
                    if not ESTADO["autenticado"]:
                        garantir_sessao()

                    logger.info(
                        "[CRM] Verificando Adimplência ao vivo: %s...", contrato
                    )
                    ESTADO["ultimo_keep_alive"] = time.time()

                    encontrou, motivo, cota_versao_site = buscar_contrato_avancado(
                        driver_global, contrato
                    )

                    if encontrou:
                        if cota_versao_site:
                            info["cota_versao"] = cota_versao_site

                        info["data_limbo"] = None
                        mudou = True

                        dados_adim = raspar_dados_adimplencia(driver_global)

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
                                dados_adim["status_pagamento"],
                                dados_adim["parcelas_pagas"],
                                "Ativo",
                                13,
                            )
                            registrar_adimplencia_planilhas(
                                sheet_g_ano,
                                contrato,
                                dados_adim["status_pagamento"],
                                dados_adim["parcelas_pagas"],
                                "Ativo",
                                12,
                            )
                            registrar_adimplencia_planilhas(
                                sheet_g_mes,
                                contrato,
                                dados_adim["status_pagamento"],
                                dados_adim["parcelas_pagas"],
                                "Ativo",
                                12,
                            )

                    else:
                        if motivo == "SESSAO_CAIU":
                            logger.error(
                                "[SISTEMA] Queda de conexão detectada ao vivo pela rede. "
                                "Interrompendo a cascata de limbos falsos."
                            )
                            ESTADO["autenticado"] = False
                            continue

                        elif motivo == "NAO_ENCONTRADO":
                            if not info.get("data_limbo"):
                                info["data_limbo"] = time.time()
                                logger.warning(
                                    "[CRM] %s entrou no Limbo (Inacessível, "
                                    "mas não oficializado).",
                                    contrato,
                                )
                                mudou = True

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
                                    logger.error(
                                        "[CRM] Contrato %s expirou no Limbo.", contrato
                                    )
                                    info["monitorar"] = False
                                    mudou = True

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
                    logger.error(
                        "[CRM] Erro ao verificar contrato %s: %s", contrato, e_indiv
                    )

        if mudou:
            salvar_adimplencia(adimplencia)


# ============================================================================
# PONTO DE ENTRADA DA APLICAÇÃO (SERVIDOR WSGI PRODUCTION)
# ============================================================================
if __name__ == "__main__":
    logger.info(">>> INICIANDO SISTEMA ENTERPRISE V6 (WAITRESS WSGI) <<<")
    driver_global = iniciar_navegador()

    logger.info("Executando login imediato na inicialização do sistema...")
    with navegador_lock:
        try:
            garantir_sessao()
        except Exception as e_inicial:  # pylint: disable=broad-exception-caught
            logger.error("Não foi possível efetuar o login inicial: %s", e_inicial)

    threading.Thread(target=loop_reanalise_background, daemon=True).start()
    threading.Thread(target=loop_reanalise_adimplencia, daemon=True).start()

    logger.info("Servidor WSGI (Waitress) iniciado na porta 5000.")
    serve(app, host="0.0.0.0", port=5000)
