"""
Motor CDA Enxuto (Versão Anti-Bot / Foco 1ª Parcela).
Sem integrações com Google Sheets. Prioriza Webhooks para o Autocred OS.
"""

import time
import logging
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
import requests

from cda_modulos import (
    consultar_adimplencia_http_v2,
    mapear_relatorios_via_http,
)

from gerador_dados import (
    limpar_inteiro,
    CONFIG,
    carregar_adimplencia,
    salvar_adimplencia,
    carregar_expirados,
    salvar_expirados,
)

from old.motor_navegacao import iniciar_navegador, fazer_login_com_ia

# =================================================================
# CONFIGURAÇÕES DE PERFORMANCE E DEFESA ANTI-BOT
# =================================================================
PAUSA_ANTI_BOT = 60  # Segundos entre requisições (Mude para 0 depois)

# Se True, ignora completamente clientes que já pagaram a 1ª parcela
MODO_1_PAGAMENTO_APENAS = (
    str(CONFIG.get("1_PAGAMENTO_APENAS", "True")).lower() == "true"
)
# =================================================================

log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
logger_crm = logging.getLogger("EnterpriseCRM_Lite")
logger_crm.setLevel(logging.INFO)
handler_crm = RotatingFileHandler(
    "files/crm_lite.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
handler_crm.setFormatter(log_formatter)
logger_crm.addHandler(handler_crm)
logger_crm.addHandler(logging.StreamHandler())

ESTADO_GERAL = {"ultima_sincronizacao_ram": 0, "mapa_inativos_ram": {}}


def comparar_nomes(nome1, nome2):
    """Compara nomes de forma exata, removendo apenas espaços duplos ou acidentais."""
    if not nome1 or not nome2:
        return False
    return " ".join(str(nome1).split()) == " ".join(str(nome2).split())


def aquecer_sessao_asp(sessao: requests.Session) -> bool:
    """Sincroniza a sessão HTTP injetada com os frames do servidor ASP."""
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
        return True
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_crm.error("[SISTEMA] Falha ao aquecer a sessão: %s", e)
        return False


def criar_sessao_hibrida() -> requests.Session:
    """Constrói sessão HTTP falsificando a identidade de um navegador real."""
    logger_crm.info("=== Iniciando Sequestro de Sessão (Híbrido) ===")
    driver = iniciar_navegador()
    sessao_http = requests.Session()

    mascara_agente = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    sessao_http.headers.update({"User-Agent": mascara_agente})

    if fazer_login_com_ia(driver):
        for cookie in driver.get_cookies():
            sessao_http.cookies.set(
                cookie["name"], cookie["value"], domain=cookie["domain"]
            )
        driver.quit()
        aquecer_sessao_asp(sessao_http)
        return sessao_http

    driver.quit()
    return None


def disparar_webhook_em_background(url, dados, cabecalhos):
    """Realiza o envio de dados via POST (Webhook) para atualizar o site."""
    try:
        res = requests.post(url, json=dados, headers=cabecalhos, timeout=10)
        if res.status_code in (200, 201):
            logger_crm.info(
                "    [WEBHOOK SUCESSO] Site atualizado: %s", dados["contrato"]
            )
        else:
            logger_crm.warning("    [WEBHOOK AVISO] Erro %s no site.", res.status_code)
    except requests.exceptions.RequestException:
        logger_crm.warning("    [ERRO WEBHOOK] O site não respondeu ao alerta.")


def loop_reanalise_adimplencia():
    """Motor principal de auditoria, otimizado para não acionar firewalls."""
    logger_crm.info("=== Motor CDA Enxuto Iniciado ===")
    sessao_http = criar_sessao_hibrida()
    ultimo_login = time.time()
    executor_webhooks = ThreadPoolExecutor(max_workers=2)

    while True:
        agora = time.time()
        intervalo_reanalise = int(CONFIG.get("INTERVALO_REANALISE_ADIMPLENCIA", 14400))

        if not sessao_http or (agora - ultimo_login) > 14000:
            sessao_http = criar_sessao_hibrida()
            ultimo_login = time.time()
            if not sessao_http:
                time.sleep(60)
                continue

        intervalo_sinc = int(CONFIG.get("INTERVALO_SINC_RELATORIOS", 3600))
        if agora - ESTADO_GERAL["ultima_sincronizacao_ram"] >= intervalo_sinc:
            ESTADO_GERAL["mapa_inativos_ram"] = mapear_relatorios_via_http(sessao_http)
            aquecer_sessao_asp(sessao_http)
            ESTADO_GERAL["ultima_sincronizacao_ram"] = time.time()

        adimplencia = carregar_adimplencia()
        if not adimplencia:
            time.sleep(5)
            continue

        mudou_json = False
        fila_online = []
        mapa_ram = ESTADO_GERAL["mapa_inativos_ram"]

        # ==========================================================
        # 1. MONTAGEM DA FILA INTELIGENTE E VARREDURA OFFLINE
        # ==========================================================
        for contrato, info in adimplencia.items():
            if info.get("monitorar", True) is False:
                continue

            # OTIMIZAÇÃO: Ignorar se a 1ª parcela já estiver paga e MODO ativo
            if MODO_1_PAGAMENTO_APENAS and info.get("primeira_parcela_paga"):
                continue

            # DETECÇÃO OFFLINE DE CANCELADOS/DESISTENTES
            grupo = limpar_inteiro(info.get("grupo"))
            cota_base = limpar_inteiro(info.get("cota"))
            versao = info.get("versao")
            nome_json = info.get("nome", "")
            status_ram = None

            if versao is not None:
                versao_desistente = versao + 40
                resultado_ram = mapa_ram.get((grupo, cota_base, versao))

                if resultado_ram:
                    status_ram = resultado_ram[0]
                else:
                    resultado_ram = mapa_ram.get((grupo, cota_base, versao_desistente))
                    if resultado_ram and resultado_ram[0] == "DESISTENTE":
                        if comparar_nomes(nome_json, resultado_ram[1]):
                            status_ram = resultado_ram[0]

            if status_ram:
                logger_crm.warning(
                    "    [INATIVO] Contrato %s classificado %s via Relatório.",
                    contrato,
                    status_ram,
                )
                info["monitorar"] = False
                info["ultimo_status"] = status_ram
                mudou_json = True

                # Avisar o site que o cliente cancelou
                payload = {
                    "contrato": contrato,
                    "status_pagamento": status_ram,
                    "parcelas_pagas": info.get("ultimas_parcelas", 0),
                    "status_cliente": status_ram,
                }
                headers = {
                    "Authorization": "Bearer K01GFBehTTheBFG10k",
                    "Content-Type": "application/json",
                }
                executor_webhooks.submit(
                    disparar_webhook_em_background,
                    "https://autocredbrasil.app/api/webhook/cda",
                    payload,
                    headers,
                )
                continue

            # Se sobreviveu, vai para a fila online
            ultima_verificacao = info.get("ultima_verificacao", 0)
            if (agora - ultima_verificacao) >= intervalo_reanalise:
                fila_online.append((ultima_verificacao, contrato, info))

        if mudou_json:
            salvar_adimplencia(adimplencia)
            mudou_json = False

        fila_online.sort(key=lambda x: x[0])
        total_fila = len(fila_online)

        if total_fila == 0:
            time.sleep(5)
            continue

        logger_crm.info(
            "[ONLINE] Fila Otimizada: %d contratos aguardam auditoria.", total_fila
        )
        falhas_inexplicaveis = 0

        # ==========================================================
        # 2. AUDITORIA ONLINE (Com Diagnóstico e Pausa)
        # ==========================================================
        for index, (_, contrato, info) in enumerate(fila_online, start=1):
            logger_crm.info(
                " -> Analisando [%d/%d] | Contrato: %s", index, total_fila, contrato
            )

            dados_rede = consultar_adimplencia_http_v2(sessao_http, contrato, info)

            # --- LOG EXPLÍCITO DE DIAGNÓSTICO ---
            if dados_rede["encontrou"]:
                st_painel = dados_rede.get("status_pagamento", "N/A")
                pc_painel = dados_rede.get("parcelas_pagas", 0)
                logger_crm.info(
                    "    [OK] Lido com Sucesso | Status: %s | Parcelas: %s",
                    st_painel,
                    pc_painel,
                )
            else:
                motivo_falha = dados_rede.get("motivo", "DESCONHECIDO")
                logger_crm.warning(
                    "    [BARRADO] Leitura Falhou | Resposta do Servidor: %s",
                    motivo_falha,
                )
            # ------------------------------------

            if PAUSA_ANTI_BOT > 0:
                time.sleep(PAUSA_ANTI_BOT)

            if dados_rede["encontrou"]:
                falhas_inexplicaveis = 0
                info["falhas_consecutivas"] = 0
                info["ultima_verificacao"] = time.time()

                st_atual = dados_rede.get("status_pagamento", "N/A")
                pc_atual = dados_rede.get("parcelas_pagas", 0)
                st_anterior = info.get("ultimo_status")
                pc_anterior = info.get("ultimas_parcelas")
                is_pago_agora = pc_atual > 0

                # A. CHECK DA 1ª PARCELA
                if not info.get("primeira_parcela_paga"):
                    if is_pago_agora:
                        logger_crm.info(
                            "    [PAGAMENTO 1ª PARC] Contrato %s honrado.", contrato
                        )

                        info["primeira_parcela_paga"] = True
                        info["ultimas_parcelas"] = pc_atual
                        info["ultimo_status"] = st_atual

                        payload = {
                            "contrato": contrato,
                            "status_pagamento": st_atual,
                            "parcelas_pagas": pc_atual,
                            "status_cliente": "Ativo",
                        }
                        headers = {
                            "Authorization": "Bearer K01GFBehTTheBFG10k",
                            "Content-Type": "application/json",
                        }
                        url_webhook = "https://autocredbrasil.app/api/webhook/cda"
                        executor_webhooks.submit(
                            disparar_webhook_em_background,
                            url_webhook,
                            payload,
                            headers,
                        )
                        mudou_json = True
                    else:
                        agora_ts = time.time()
                        data_inc = info.get("data_inclusao", agora_ts)
                        if agora_ts - data_inc > 2592000:
                            logger_crm.warning(
                                "    [EXPIRADO] Contrato %s arquivado (30 dias s/ pgto).",
                                contrato,
                            )
                            info["monitorar"] = False
                            expirados = carregar_expirados()
                            expirados[str(contrato)] = info
                            salvar_expirados(expirados)

                            bd_atual = carregar_adimplencia()
                            if str(contrato) in bd_atual:
                                del bd_atual[str(contrato)]
                            salvar_adimplencia(bd_atual)
                            continue

                # B. DELTA CACHE DE ADIMPLÊNCIA (Longo Prazo)
                elif not MODO_1_PAGAMENTO_APENAS:
                    houve_mutacao = st_atual != st_anterior or pc_atual != pc_anterior
                    if houve_mutacao:
                        logger_crm.info(
                            "    [ATUALIZAÇÃO LONGO PRAZO] %s -> %s", contrato, st_atual
                        )
                        info["ultimo_status"] = st_atual
                        info["ultimas_parcelas"] = pc_atual
                        mudou_json = True

                        payload = {
                            "contrato": contrato,
                            "status_pagamento": st_atual,
                            "parcelas_pagas": pc_atual,
                            "status_cliente": "Ativo",
                        }
                        headers = {
                            "Authorization": "Bearer K01GFBehTTheBFG10k",
                            "Content-Type": "application/json",
                        }
                        url_webhook = "https://autocredbrasil.app/api/webhook/cda"
                        executor_webhooks.submit(
                            disparar_webhook_em_background,
                            url_webhook,
                            payload,
                            headers,
                        )

            else:
                motivo = dados_rede.get("motivo")
                if motivo == "SESSAO_CAIU":
                    sessao_http = None
                    break

                if motivo == "FALHA_REDE":
                    falhas_inexplicaveis += 1
                    info["ultima_verificacao"] = time.time()
                    mudou_json = True

                elif motivo == "NAO_ENCONTRADO":
                    # Punição isolada para o contrato fantasma. Não derruba o servidor.
                    falhas = info.get("falhas_consecutivas", 0) + 1
                    info["falhas_consecutivas"] = falhas
                    info["ultima_verificacao"] = time.time()

                    if falhas >= 3:
                        logger_crm.warning(
                            "    [FANTASMA] Contrato %s s/ resposta 3x. Desativando monitoramento.",
                            contrato,
                        )
                        info["monitorar"] = False
                    mudou_json = True

            bd_atual = carregar_adimplencia()
            if contrato in bd_atual:
                bd_atual[contrato].update(info)
            salvar_adimplencia(bd_atual)
            mudou_json = False

            if falhas_inexplicaveis >= 7:
                logger_crm.error(
                    "    [CIRCUIT BREAKER] 7 Falhas de REDE seguidas. Pausando por 2 min..."
                )
                sessao_http = None
                time.sleep(120)
                break


if __name__ == "__main__":
    loop_reanalise_adimplencia()
