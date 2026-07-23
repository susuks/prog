"""
Servidor Independente do Controle de Adimplência (CDA).

Executa a leitura em massa de relatórios offline e gere uma fila
de monitorização inteligente e ininterrupta baseada em sessão persistente.
"""

import time
import re
import logging
from logging.handlers import RotatingFileHandler

from cda_modulos import (
    carregar_adimplencia,
    salvar_adimplencia,
    mapear_todos_offline,
    registrar_adimplencia_planilhas,
    registrar_apenas_situacao_cliente,
    buscar_contrato_avancado,
    raspar_dados_adimplencia,
    baixar_relatorios_cache,
)

from gerador_dados import (
    limpar_inteiro,
    CONFIG,
    PREFIXO_PLANILHA,
    NOME_ABA_GERAL,
    conectar_google_sheets,
)
from motor_navegacao import iniciar_navegador, fazer_login_com_ia

# ============================================================================
# CONFIGURAÇÃO DE LOGGING ESTRUTURADO E DESCRITIVO
# ============================================================================
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

logger_crm = logging.getLogger("EnterpriseCRM")
logger_crm.setLevel(logging.INFO)
handler_crm = RotatingFileHandler(
    "files/crm_sistema.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
handler_crm.setFormatter(log_formatter)
logger_crm.addHandler(handler_crm)
logger_crm.addHandler(logging.StreamHandler())


driver_crm = None
ESTADO_CRM = {
    "autenticado": False,
    "ultimo_login": 0,
    "ultima_sincronizacao": 0,
    "ultima_sincronizacao_diaria": 0,
}


def garantir_sessao_crm():
    """Gere o ciclo de 4 horas e o login do navegador exclusivo do CDA."""
    agora = time.time()
    precisa_relogin = (agora - ESTADO_CRM.get("ultimo_login", 0)) > 14400

    if not ESTADO_CRM["autenticado"] or precisa_relogin:
        if precisa_relogin and ESTADO_CRM["autenticado"]:
            logger_crm.info(
                "[SISTEMA] Ciclo expirado. Efetuando renovação do Token de Sessão..."
            )

        if fazer_login_com_ia(driver_crm):
            logger_crm.info(
                "[LIBERADO] Acesso restabelecido via IA. Portal pronto para leitura."
            )
            ESTADO_CRM["autenticado"] = True
            ESTADO_CRM["ultimo_login"] = agora
        else:
            logger_crm.error(
                "[BARRADO] Falha crítica ao intercetar o login no portal Autocred."
            )
            raise ConnectionError("Falha de autenticação no portal.")


def loop_reanalise_adimplencia():
    """Fila Contínua (Worker): Monitoriza a vida financeira sem pausas bloqueantes."""
    logger_crm.info("=== Motor CDA em Arquitetura de Fila Contínua Iniciado ===")

    while True:
        # Pausa curta para permitir circulação de CPU e encerramentos do PM2
        time.sleep(30)

        intervalo_reanalise = int(CONFIG.get("INTERVALO_REANALISE_ADIMPLENCIA", 14400))
        intervalo_sinc = int(CONFIG.get("INTERVALO_SINC_RELATORIOS", 3600))
        limite_dias_inativo = int(CONFIG.get("LIMITE_DIAS_DESATIVACAO", 45))
        agora = time.time()

        # ====================================================================
        # 1. SINCRONIZAÇÃO DE CACHES (BAIXAR RELATÓRIOS SE NECESSÁRIO)
        # ====================================================================
        if agora - ESTADO_CRM.get("ultima_sincronizacao_diaria", 0) >= 86400:
            if not ESTADO_CRM["autenticado"]:
                try:
                    garantir_sessao_crm()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
            if ESTADO_CRM["autenticado"]:
                logger_crm.info("[SINC] Iniciando rotina Diária de relatórios pesados.")
                baixar_relatorios_cache(driver_crm, tipo="diario")
                ESTADO_CRM["ultima_sincronizacao_diaria"] = time.time()
                ESTADO_CRM["ultima_sincronizacao"] = time.time()

        elif agora - ESTADO_CRM.get("ultima_sincronizacao", 0) >= intervalo_sinc:
            if not ESTADO_CRM["autenticado"]:
                try:
                    garantir_sessao_crm()
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
            if ESTADO_CRM["autenticado"]:
                logger_crm.info("[SINC] Atualizando relatórios dinâmicos offline.")
                baixar_relatorios_cache(driver_crm, tipo="frequente")
                ESTADO_CRM["ultima_sincronizacao"] = time.time()

        adimplencia = carregar_adimplencia()
        if not adimplencia:
            continue

        mudou_json_global = False

        # ====================================================================
        # 2. OTIMIZAÇÃO: VARREDURA OFFLINE EM MASSA
        # ====================================================================
        logger_crm.info("[OFFLINE] Convertendo relatórios locais em Memória RAM...")
        mapa_offline = mapear_todos_offline()
        logger_crm.info(
            "[OFFLINE] Parsing finalizado. %s suspeitos identificados na memória.",
            len(mapa_offline),
        )

        for contrato, info in adimplencia.items():
            cota_versao = info.get("cota_versao")
            if not cota_versao:
                continue

            grupo = limpar_inteiro(info.get("grupo"))
            cota_base = limpar_inteiro(str(info.get("cota")).split("-", maxsplit=1)[0])

            # Extrai versão da string: ex "123 - 01" -> 1
            match_v = re.search(r"(\d+)\s*-\s*(\d+)", str(cota_versao))
            versao = limpar_inteiro(match_v.group(2)) if match_v else 0

            status_off_rapido = mapa_offline.get((grupo, cota_base, versao))

            if status_off_rapido:
                if info.get("monitorar", True) is not False:
                    logger_crm.warning(
                        "[MUDANÇA DE ESTADO] Contrato %s declarado %s via offline.",
                        contrato,
                        status_off_rapido.upper(),
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

                    if sheet_v:
                        registrar_adimplencia_planilhas(
                            sheet_v,
                            contrato,
                            "CANCELADO/DESIST",
                            0,
                            status_off_rapido,
                            13,
                            "Q",
                            "S",
                        )
                    if sheet_g_ano:
                        registrar_adimplencia_planilhas(
                            sheet_g_ano,
                            contrato,
                            "CANCELADO/DESIST",
                            0,
                            status_off_rapido,
                            12,
                            "S",
                            "U",
                        )
                    if sheet_g_mes:
                        registrar_adimplencia_planilhas(
                            sheet_g_mes,
                            contrato,
                            "CANCELADO/DESIST",
                            0,
                            status_off_rapido,
                            12,
                            "S",
                            "U",
                        )

                    info["monitorar"] = False
                    info["ultimo_status"] = "CANCELADO/DESIST"
                    mudou_json_global = True
            else:
                if not info.get("monitorar", True):
                    logger_crm.info(
                        "[RESSURREIÇÃO] Contrato %s regressou à base. Restaurando.",
                        contrato,
                    )
                    info["monitorar"] = True
                    mudou_json_global = True

        if mudou_json_global:
            salvar_adimplencia(adimplencia)

        # ====================================================================
        # 3. CONSTRUÇÃO DA FILA DE SESSÃO
        # ====================================================================
        fila_online = []
        for contrato, info in adimplencia.items():
            if info.get("monitorar", True):
                ultima_verificacao = info.get("ultima_verificacao", 0)
                if (agora - ultima_verificacao) >= intervalo_reanalise:
                    fila_online.append((ultima_verificacao, contrato, info))

        # Ordena: os clientes ignorados há mais tempo ficam no topo (Resume State)
        fila_online.sort(key=lambda x: x[0])
        total_fila = len(fila_online)

        if total_fila == 0:
            continue

        logger_crm.info(
            "[ONLINE] Fila de trabalho pronta: %s contratos aguardam auditoria.",
            total_fila,
        )

        # ====================================================================
        # 4. EXECUÇÃO CONTÍNUA (WORKER LOOP)
        # ====================================================================
        for index, (_, contrato, info) in enumerate(fila_online, start=1):
            logger_crm.info(
                " -> Analisando [%s/%s] | Contrato: %s", index, total_fila, contrato
            )

            try:
                if not ESTADO_CRM["autenticado"]:
                    garantir_sessao_crm()

                encontrou, motivo, cota_versao_site = buscar_contrato_avancado(
                    driver_crm, contrato
                )

                if encontrou:
                    if cota_versao_site and info.get("cota_versao") != cota_versao_site:
                        info["cota_versao"] = cota_versao_site

                    if info.get("data_limbo") is not None:
                        info["data_limbo"] = None

                    dados_adim = raspar_dados_adimplencia(driver_crm)
                    st_atual = dados_adim.get("status_pagamento")
                    pc_atual = dados_adim.get("parcelas_pagas")

                    st_anterior = info.get("ultimo_status")
                    pc_anterior = info.get("ultimas_parcelas")

                    if st_atual != st_anterior or pc_atual != pc_anterior:
                        logger_crm.info(
                            "    [ATUALIZAÇÃO] Progresso no contrato %s. Sincronizando.",
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

                        if sheet_v:
                            registrar_adimplencia_planilhas(
                                sheet_v,
                                contrato,
                                st_atual,
                                pc_atual,
                                "Ativo",
                                13,
                                "Q",
                                "S",
                            )
                        if sheet_g_ano:
                            registrar_adimplencia_planilhas(
                                sheet_g_ano,
                                contrato,
                                st_atual,
                                pc_atual,
                                "Ativo",
                                12,
                                "S",
                                "U",
                            )
                        if sheet_g_mes:
                            registrar_adimplencia_planilhas(
                                sheet_g_mes,
                                contrato,
                                st_atual,
                                pc_atual,
                                "Ativo",
                                12,
                                "S",
                                "U",
                            )

                        info["ultimo_status"] = st_atual
                        info["ultimas_parcelas"] = pc_atual
                    else:
                        logger_crm.info(
                            "    [IGUAL] Dados inalterados. Evitando request no Sheets."
                        )

                else:
                    if motivo == "SESSAO_CAIU":
                        logger_crm.error(
                            "    [QUEDA] Navegação colapsou. Fila pausada para reconexão."
                        )
                        ESTADO_CRM["autenticado"] = False
                        break  # Interrompe o For Loop para recomeçar o While e reconectar

                    elif motivo == "NAO_ENCONTRADO":
                        if not info.get("data_limbo"):
                            info["data_limbo"] = time.time()
                            logger_crm.warning(
                                "    [LIMBO] O contrato %s perdeu o acesso. Contagem ativa.",
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

                            if sheet_v:
                                registrar_apenas_situacao_cliente(
                                    sheet_v, contrato, "Inacessível", 13, 19
                                )
                            if sheet_g_ano:
                                registrar_apenas_situacao_cliente(
                                    sheet_g_ano, contrato, "Inacessível", 12, 21
                                )
                            if sheet_g_mes:
                                registrar_apenas_situacao_cliente(
                                    sheet_g_mes, contrato, "Inacessível", 12, 21
                                )
                        else:
                            dias = (time.time() - info["data_limbo"]) / 86400
                            if dias > limite_dias_inativo:
                                logger_crm.error(
                                    "    [EXPIRADO] Limbo ultrapassado. Contrato %s revogado.",
                                    contrato,
                                )
                                info["monitorar"] = False

                                sheet_v = conectar_google_sheets(
                                    info["nome_planilha"], info["aba_original"]
                                )
                                sheet_g_ano = conectar_google_sheets(
                                    f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                                )
                                sheet_g_mes = conectar_google_sheets(
                                    f"{PREFIXO_PLANILHA}GERAL", info["aba_original"]
                                )

                                if sheet_v:
                                    registrar_apenas_situacao_cliente(
                                        sheet_v, contrato, "Inacessível", 13, 19
                                    )
                                if sheet_g_ano:
                                    registrar_apenas_situacao_cliente(
                                        sheet_g_ano, contrato, "Inacessível", 12, 21
                                    )
                                if sheet_g_mes:
                                    registrar_apenas_situacao_cliente(
                                        sheet_g_mes, contrato, "Inacessível", 12, 21
                                    )

            except Exception as e_indiv:  # pylint: disable=broad-exception-caught
                logger_crm.error(
                    "    [ERRO VITAL] Colapso na verificação do contrato %s: %s",
                    contrato,
                    e_indiv,
                )

            # Gravação obrigatória. Garante o salvamento do progresso da fila (Resume State)
            info["ultima_verificacao"] = time.time()
            salvar_adimplencia(adimplencia)


if __name__ == "__main__":
    logger_crm.info(">>> INICIANDO SERVIDOR CDA (FILA CONTÍNUA OTIMIZADA) <<<")
    driver_crm = iniciar_navegador()

    try:
        garantir_sessao_crm()
    except Exception as e_inicial:  # pylint: disable=broad-exception-caught
        logger_crm.error("Falha na autenticação inicial do CDA: %s", e_inicial)

    loop_reanalise_adimplencia()
