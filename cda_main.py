"""
Motor Principal do Controle de Adimplência (CDA) V2.

Orquestra a auditoria sequencial veloz na Autocred via HTTP puro,
acumulando as respostas em buffers de memória e disparando as atualizações
para o Google Sheets em Lotes (Batch Update), obliterando a latência da API.
"""

import time
import re
import logging
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
import requests

from cda_modulos import (
    carregar_adimplencia,
    salvar_adimplencia,
    consultar_adimplencia_http_v2,
    mapear_relatorios_via_http,
    registrar_apenas_situacao_cliente,
    cache_cda,
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
    "files/crm_sistema.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
handler_crm.setFormatter(log_formatter)
logger_crm.addHandler(handler_crm)
logger_crm.addHandler(logging.StreamHandler())

ESTADO_GERAL = {"ultima_sincronizacao_ram": 0, "mapa_inativos_ram": {}}


def tarefa_background_lote(lote_dados):
    """
    Descarrega um lote massivo de atualizações para o Google Sheets.
    Agrupa os dados por Planilha/Aba para abrir a conexão apenas uma vez por
    alvo, e utiliza a função 'batch_update' para escrever tudo numa só chamada.
    """
    tempo_inicio = time.time()

    mapa_vendedor = {}
    mapa_geral_mes = {}
    mapa_geral_ano = {}

    for item in lote_dados:
        cv = (item["nome_planilha"], item["aba_original"])
        mapa_vendedor.setdefault(cv, []).append(item)

        cgm = (f"{PREFIXO_PLANILHA}GERAL", item["aba_original"])
        mapa_geral_mes.setdefault(cgm, []).append(item)

        cga = (f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL)
        mapa_geral_ano.setdefault(cga, []).append(item)

    try:

        def processar_agrupamento(agrupamento, col_busca, col_inicio, col_fim):
            for (nome, aba), itens in agrupamento.items():
                if not nome or not aba:
                    continue
                sheet = conectar_google_sheets(nome, aba)
                if not sheet:
                    continue

                payload_batch = []
                for it in itens:
                    linha = cache_cda.obter_linha(sheet, it["contrato"], col_busca)
                    if linha:
                        payload_batch.append(
                            {
                                "range": f"{col_inicio}{linha}:{col_fim}{linha}",
                                "values": [
                                    [
                                        str(it["status_pgto"]),
                                        int(it["parcelas"]),
                                        str(it["status_cliente"]),
                                    ]
                                ],
                            }
                        )

                if payload_batch:
                    sheet.batch_update(payload_batch, value_input_option="USER_ENTERED")
                    time.sleep(1)

        processar_agrupamento(mapa_vendedor, 13, "Q", "S")
        processar_agrupamento(mapa_geral_mes, 12, "S", "U")
        processar_agrupamento(mapa_geral_ano, 12, "S", "U")

        duracao = time.time() - tempo_inicio
        logger_crm.info(
            "    [SHEETS BATCH] Lote de %d contratos sincronizado massivamente "
            "em %.2fs.",
            len(lote_dados),
            duracao,
        )

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_crm.error("    [SHEETS BATCH ERRO] Falha ao processar o lote: %s", e)


def aquecer_sessao_asp(sessao: requests.Session) -> bool:
    """
    Sincroniza a sessão HTTP injetada com os frames do servidor ASP.
    """
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

        logger_crm.info("[SISTEMA] Sessão HTTP aquecida com validação de módulo ASP.")
        return True
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_crm.error("[SISTEMA] Falha ao aquecer a sessão: %s", e)
        return False


def criar_sessao_hibrida() -> requests.Session:
    """
    Constrói uma sessão HTTP falsificando a identidade de um navegador real.
    """
    logger_crm.info("=== Iniciando Sequestro de Sessão (Híbrido - V2) ===")
    driver = iniciar_navegador()
    sessao_http = requests.Session()

    mascara_agente = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
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
        logger_crm.info("[SUCESSO] Cookies extraídos. Destruindo a interface.")
        driver.quit()
        aquecer_sessao_asp(sessao_http)
        return sessao_http

    logger_crm.error("[FALHA] Não foi possível forjar a sessão HTTP.")
    driver.quit()
    return None


def loop_reanalise_adimplencia():
    """
    Loop principal ininterrupto do sistema corporativo (CDA).
    """
    logger_crm.info("=== Motor CDA (Estabilizado e Fisiológico) Iniciado ===")

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
                "[OFFLINE] Sincronizando relatórios de Cancelados/Desistentes..."
            )
            ESTADO_GERAL["mapa_inativos_ram"] = mapear_relatorios_via_http(sessao_http)
            logger_crm.info(
                "[OFFLINE] RAM Populada. Total de %d contratos inativados.",
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

        buffer_inativos = []

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
                        "[MUDANÇA] Contrato %s classificado %s via Filtro RAM.",
                        contrato,
                        status_ram,
                    )

                    buffer_inativos.append(
                        {
                            "contrato": contrato,
                            "nome_planilha": info["nome_planilha"],
                            "aba_original": info["aba_original"],
                            "status_pgto": "CANCELADO/DESIST",
                            "parcelas": 0,
                            "status_cliente": status_ram,
                        }
                    )

                    info["monitorar"] = False
                    info["ultimo_status"] = "CANCELADO/DESIST"
                    mudou_json = True
                    continue

            if info.get("monitorar", True):
                ultima_verificacao = info.get("ultima_verificacao", 0)
                if (agora - ultima_verificacao) >= intervalo_reanalise:
                    fila_online.append((ultima_verificacao, contrato, info))

        if buffer_inativos:
            executor_sheets.submit(tarefa_background_lote, buffer_inativos.copy())

        if mudou_json:
            salvar_adimplencia(adimplencia)

        fila_online.sort(key=lambda x: x[0])
        total_fila = len(fila_online)

        if total_fila == 0:
            time.sleep(5)
            continue

        logger_crm.info(
            "[ONLINE] Fila HTTP pronta: %d contratos aguardam auditoria.",
            total_fila,
        )

        buffer_atualizacoes = []
        tempo_ultimo_lote = time.time()
        tamanho_lote = 20

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
                            "    [ATUALIZAÇÃO] %s -> %s. Adicionado ao cesto...",
                            contrato,
                            fotografia,
                        )

                    buffer_atualizacoes.append(
                        {
                            "contrato": contrato,
                            "nome_planilha": info["nome_planilha"],
                            "aba_original": info["aba_original"],
                            "status_pgto": st_atual,
                            "parcelas": pc_atual,
                            "status_cliente": "Ativo",
                        }
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
                    logger_crm.error("    [QUEDA] Acesso negado. Servidor caiu.")
                    sessao_http = None
                    break

                if motivo == "NAO_ENCONTRADO":
                    info["ultima_verificacao"] = time.time()
                    if not info.get("data_limbo"):
                        info["data_limbo"] = time.time()
                        logger_crm.warning(
                            "    [LIMBO NOVO] Contrato %s inacessível.", contrato
                        )

                        sheet_morta = conectar_google_sheets(
                            info["nome_planilha"], info["aba_original"]
                        )
                        registrar_apenas_situacao_cliente(
                            sheet_morta, contrato, "Inacessível", 13, 19
                        )
                    else:
                        dias = int((time.time() - info["data_limbo"]) / 86400)
                        if dias > limite_dias_inativo:
                            logger_crm.error(
                                "    [EXPIRADO] Contrato %s atingiu limite.", contrato
                            )
                            info["monitorar"] = False
                        else:
                            logger_crm.info(
                                "    [LIMBO ATIVO] %s carência (%d/%d dias).",
                                contrato,
                                dias,
                                limite_dias_inativo,
                            )

            condicao_tempo = (time.time() - tempo_ultimo_lote) > 60
            if len(buffer_atualizacoes) >= tamanho_lote or condicao_tempo:
                if buffer_atualizacoes:
                    logger_crm.info(
                        "    [DISPARO] Enviando lote com %d atualizações...",
                        len(buffer_atualizacoes),
                    )
                    executor_sheets.submit(
                        tarefa_background_lote, buffer_atualizacoes.copy()
                    )
                    buffer_atualizacoes.clear()
                tempo_ultimo_lote = time.time()

            bd_atual = carregar_adimplencia()
            if contrato in bd_atual:
                bd_atual[contrato].update(info)
            salvar_adimplencia(bd_atual)

        if buffer_atualizacoes:
            logger_crm.info(
                "    [DISPARO FINAL] Enviando últimos %d contratos...",
                len(buffer_atualizacoes),
            )
            executor_sheets.submit(tarefa_background_lote, buffer_atualizacoes.copy())
            buffer_atualizacoes.clear()


if __name__ == "__main__":
    loop_reanalise_adimplencia()
