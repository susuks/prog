"""
Motor Principal do Controle de Adimplência (CDA) V2.

Orquestra a auditoria assíncrona dos contratos na Autocred via HTTP puro,
gerindo o ciclo de vida da sessão e delegando a atualização das planilhas
Google para uma fila secundária, garantindo alta performance de varredura.
"""

import time
from concurrent.futures import ThreadPoolExecutor
import re
import logging
from logging.handlers import RotatingFileHandler
import requests

from cda_modulos import (
    carregar_adimplencia,
    salvar_adimplencia,
    consultar_adimplencia_http_v2,
    mapear_relatorios_via_http,
    registrar_adimplencia_planilhas,
    registrar_apenas_situacao_cliente,
)

from gerador_dados import (
    limpar_inteiro,
    CONFIG,
    PREFIXO_PLANILHA,
    NOME_ABA_GERAL,
    conectar_google_sheets,
)
from motor_navegacao import iniciar_navegador, fazer_login_com_ia

log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
logger_crm = logging.getLogger("EnterpriseCRM")
logger_crm.setLevel(logging.INFO)
handler_crm = RotatingFileHandler(
    "files/crm_sistema.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
handler_crm.setFormatter(log_formatter)
logger_crm.addHandler(handler_crm)
logger_crm.addHandler(logging.StreamHandler())

ESTADO_GERAL = {"ultima_sincronizacao_ram": 0, "mapa_inativos_ram": {}}


def tarefa_background_sheets(contrato, nome_planilha, aba_original, st_atual, pc_atual):
    """
    Executa a gravação no Google Sheets em segundo plano.

    Abre conexões independentes para a planilha do vendedor e as planilhas gerais
    (mensal e anual) e processa a atualização utilizando o cache O(1) do CDA.
    O motor principal não fica bloqueado a aguardar a resolução desta thread.
    """
    tempo_inicio = time.time()
    try:
        sheet_v = conectar_google_sheets(nome_planilha, aba_original)
        sheet_g_ano = conectar_google_sheets(f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL)
        sheet_g_mes = conectar_google_sheets(f"{PREFIXO_PLANILHA}GERAL", aba_original)

        if sheet_v:
            registrar_adimplencia_planilhas(
                sheet_v, contrato, st_atual, pc_atual, "Ativo", 13, "Q", "S"
            )
        if sheet_g_ano:
            registrar_adimplencia_planilhas(
                sheet_g_ano, contrato, st_atual, pc_atual, "Ativo", 12, "S", "U"
            )
        if sheet_g_mes:
            registrar_adimplencia_planilhas(
                sheet_g_mes, contrato, st_atual, pc_atual, "Ativo", 12, "S", "U"
            )

        duracao = time.time() - tempo_inicio
        logger_crm.info(
            "    [SHEETS ASYNC] Concluído! %s gravado nas planilhas em %.3fs.",
            contrato,
            duracao,
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_crm.error("    [SHEETS ASYNC ERRO] Falha ao gravar %s: %s", contrato, e)


def aquecer_sessao_asp(sessao: requests.Session) -> bool:
    """
    Sincroniza a sessão HTTP injetada com os frames do servidor ASP.

    Garante que a intranet da Autocred regista a sessão ativa navegando
    silenciosamente pelas estruturas primárias (MasterFrameset e LeftFrame).
    """
    try:
        url_master = (
            "https://intranet.consorciotradicao.com.br/autocred/MasterFrameset.asp"
        )
        url_left = (
            "https://intranet.consorciotradicao.com.br/autocred/"
            "LeftFrame.asp?codigo_modulo=AG"
        )
        sessao.get(url_master, timeout=10)
        sessao.get(url_left, timeout=10)
        logger_crm.info(
            "[SISTEMA] Sessão HTTP aquecida com sucesso (Estado ASP sincronizado)."
        )
        return True
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_crm.error("[SISTEMA] Falha ao aquecer a sessão: %s", e)
        return False


def criar_sessao_hibrida() -> requests.Session:
    """
    Constrói uma sessão HTTP falsificando a identidade de um navegador real.

    Utiliza o motor Selenium para transpor barreiras de autenticação e IA,
    extrai os cookies de liberação e encerra a interface gráfica, devolvendo
    uma sessão `requests` pura, estabilizada e altamente performática.
    """
    logger_crm.info("=== Iniciando Sequestro de Sessão (Híbrido - V2) ===")
    driver = iniciar_navegador()
    sessao_http = requests.Session()

    mascara_agente = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    mascara_accept = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    )

    sessao_http.headers.update(
        {
            "User-Agent": mascara_agente,
            "Accept": mascara_accept,
        }
    )

    if fazer_login_com_ia(driver):
        for cookie in driver.get_cookies():
            sessao_http.cookies.set(
                cookie["name"], cookie["value"], domain=cookie["domain"]
            )
        logger_crm.info("[SUCESSO] Cookies extraídos. Destruindo interface visual.")
        driver.quit()
        aquecer_sessao_asp(sessao_http)
        return sessao_http

    logger_crm.error("[FALHA] Não foi possível forjar a sessão HTTP.")
    driver.quit()
    return None


def loop_reanalise_adimplencia():
    """
    Loop principal ininterrupto do sistema corporativo (CDA).

    Orquestra a renovação de sessões a cada 4 horas, atualiza o mapa RAM
    de relatórios, coordena a auditoria veloz de contratos e encaminha
    modificações financeiras para processamento assíncrono.
    """
    logger_crm.info("=== Motor CDA (HTTP Reconstruído e Assíncrono) Iniciado ===")

    sessao_http = criar_sessao_hibrida()
    ultimo_login = time.time()

    executor_sheets = ThreadPoolExecutor(max_workers=1)

    while True:
        agora = time.time()
        intervalo_reanalise = int(CONFIG.get("INTERVALO_REANALISE_ADIMPLENCIA", 14400))
        intervalo_sinc = int(CONFIG.get("INTERVALO_SINC_RELATORIOS", 3600))
        limite_dias_inativo = int(CONFIG.get("LIMITE_DIAS_DESATIVACAO", 45))

        if not sessao_http or (agora - ultimo_login) > 14000:
            logger_crm.warning("[RENOVAÇÃO] Sessão expirada. Invocando sequestro.")
            sessao_http = criar_sessao_hibrida()
            ultimo_login = time.time()
            if not sessao_http:
                time.sleep(60)
                continue

        if agora - ESTADO_GERAL["ultima_sincronizacao_ram"] >= intervalo_sinc:
            logger_crm.info(
                "[OFFLINE] Atualizando base de Cancelados/Desistentes na RAM..."
            )
            ESTADO_GERAL["mapa_inativos_ram"] = mapear_relatorios_via_http(sessao_http)
            logger_crm.info(
                "[OFFLINE] RAM Sincronizada. %d contratos.",
                len(ESTADO_GERAL["mapa_inativos_ram"]),
            )
            ESTADO_GERAL["ultima_sincronizacao_ram"] = time.time()

        adimplencia = carregar_adimplencia()
        if not adimplencia:
            time.sleep(5)
            continue

        mudou_json = False
        mapa_ram = ESTADO_GERAL["mapa_inativos_ram"]
        fila_online = []

        for contrato, info in adimplencia.items():
            cota_versao = info.get("cota_versao")
            if cota_versao and info.get("monitorar", True) is not False:
                grupo = limpar_inteiro(info.get("grupo"))
                cota_base = limpar_inteiro(
                    str(info.get("cota")).split("-", maxsplit=1)[0]
                )
                match_v = re.search(r"(\d+)\s*-\s*(\d+)", str(cota_versao))
                versao = limpar_inteiro(match_v.group(2)) if match_v else 0

                status_ram = mapa_ram.get((grupo, cota_base, versao))

                if status_ram:
                    logger_crm.warning(
                        "[MUDANÇA DE ESTADO] Contrato %s classificado %s (RAM).",
                        contrato,
                        status_ram,
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
                            status_ram,
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
                            status_ram,
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
                            status_ram,
                            12,
                            "S",
                            "U",
                        )

                    info["monitorar"] = False
                    info["ultimo_status"] = "CANCELADO/DESIST"
                    mudou_json = True
                    continue

            if info.get("monitorar", True):
                ultima_verificacao = info.get("ultima_verificacao", 0)
                if (agora - ultima_verificacao) >= intervalo_reanalise:
                    fila_online.append((ultima_verificacao, contrato, info))

        if mudou_json:
            salvar_adimplencia(adimplencia)

        fila_online.sort(key=lambda x: x[0])
        total_fila = len(fila_online)

        if total_fila == 0:
            time.sleep(5)
            continue

        logger_crm.info(
            "[ONLINE] Fila HTTP pronta: %d contratos aguardam auditoria.", total_fila
        )

        for index, (_, contrato, info) in enumerate(fila_online, start=1):
            logger_crm.info(
                " -> Analisando [%d/%d] | Contrato: %s", index, total_fila, contrato
            )

            tempo_inicio_web = time.time()
            dados_rede = consultar_adimplencia_http_v2(sessao_http, contrato, info)
            duracao_web = time.time() - tempo_inicio_web

            if dados_rede["encontrou"]:
                info["ultima_verificacao"] = time.time()

                if dados_rede.get("cpf") and dados_rede["cpf"] != info.get("cpf"):
                    info["cpf"] = dados_rede["cpf"]

                recuperou_do_limbo = False
                if info.get("data_limbo") is not None:
                    info["data_limbo"] = None
                    recuperou_do_limbo = True

                st_atual = dados_rede.get("status_pagamento", "N/A")
                pc_atual = dados_rede.get("parcelas_pagas", 0)
                st_anterior = info.get("ultimo_status")
                pc_anterior = info.get("ultimas_parcelas")
                fotografia = f"[{st_atual} | {pc_atual} parc. | Ativo]"

                if (
                    st_atual != st_anterior
                    or pc_atual != pc_anterior
                    or recuperou_do_limbo
                ):
                    if recuperou_do_limbo:
                        logger_crm.info(
                            "    [RESSURREIÇÃO] %s saiu do Limbo.", contrato
                        )
                    else:
                        logger_crm.info(
                            "    [ATUALIZAÇÃO] %s -> %s. Fila assíncrona...",
                            contrato,
                            fotografia,
                        )

                    executor_sheets.submit(
                        tarefa_background_sheets,
                        contrato,
                        info["nome_planilha"],
                        info["aba_original"],
                        st_atual,
                        pc_atual,
                    )

                    info["ultimo_status"] = st_atual
                    info["ultimas_parcelas"] = pc_atual
                else:
                    logger_crm.info(
                        "    [IGUAL] %s -> %s. Nenhuma ação.", contrato, fotografia
                    )

                logger_crm.info("    [PROFILER] Tempo do Autocred: %.3fs", duracao_web)

            else:
                motivo = dados_rede.get("motivo")

                if motivo == "SESSAO_CAIU":
                    logger_crm.error("    [QUEDA] Acesso negado. Servidor encerrou.")
                    sessao_http = None
                    break

                if motivo == "NAO_ENCONTRADO":
                    info["ultima_verificacao"] = time.time()
                    if not info.get("data_limbo"):
                        info["data_limbo"] = time.time()
                        logger_crm.warning(
                            "    [LIMBO NOVO] Contrato %s inacessível.", contrato
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
                        dias = int((time.time() - info["data_limbo"]) / 86400)
                        if dias > limite_dias_inativo:
                            logger_crm.error(
                                "    [EXPIRADO] Contrato %s limite atingido.", contrato
                            )
                            info["monitorar"] = False
                        else:
                            logger_crm.info(
                                "    [LIMBO ATIVO] %s carência (%d/%d dias).",
                                contrato,
                                dias,
                                limite_dias_inativo,
                            )

            bd_atual = carregar_adimplencia()
            if contrato in bd_atual:
                bd_atual[contrato].update(info)
            salvar_adimplencia(bd_atual)


if __name__ == "__main__":
    loop_reanalise_adimplencia()
