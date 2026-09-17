"""
Motor Principal do Controle de Adimplência (CDA) V2 - Enterprise.

Orquestra a auditoria sequencial veloz na Autocred via HTTP puro.
Agora atua como Cérebro Unificado: monitora o pagamento da 1ª parcela (30 dias),
audita inadimplência de longo prazo (Delta Updates) e faz o Arquivamento de Expirados.
"""

import time
import logging
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
import requests

from cda_modulos import (
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
    carregar_adimplencia,
    salvar_adimplencia,
    carregar_expirados,
    salvar_expirados,
    salvar_historico_concluido,
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


def comparar_nomes(nome1, nome2):
    """Compara nomes de forma exata, removendo apenas espaços duplos ou acidentais."""
    if not nome1 or not nome2:
        return False

    n1 = " ".join(str(nome1).split())
    n2 = " ".join(str(nome2).split())

    return n1 == n2


def tarefa_background_lote(lote_dados):
    """
    Descarrega um lote massivo de atualizações para o Google Sheets.
    Suporta atualizações completas (Delta), isoladas (Status) e 1ª Parcela.
    """
    tempo_inicio = time.time()

    mapa_vendedor = {}
    mapa_geral_ano = {}

    for item in lote_dados:
        cv = (item["nome_planilha"], item["aba_original"])
        mapa_vendedor.setdefault(cv, []).append(item)

        cga = (f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL)
        mapa_geral_ano.setdefault(cga, []).append(item)

    try:

        def processar_agrupamento(
            agrupamento,
            col_busca,
            col_inicio,
            col_fim,
            col_situacao_isolada,
            col_primeira,
        ):
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
                        if it.get("atualizar_primeira_parcela"):
                            payload_batch.append(
                                {
                                    "range": f"{col_primeira}{linha}",
                                    "values": [["1º Parcela Paga"]],
                                }
                            )
                        elif it.get("apenas_situacao"):
                            payload_batch.append(
                                {
                                    "range": f"{col_situacao_isolada}{linha}",
                                    "values": [[str(it["status_cliente"])]],
                                }
                            )
                        else:
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

        # Atualizações: Vendedor = Q a S (Situação Isolada: S) (1º Parcela: B)
        # Atualizações: Geral Ano = S a U (Situação Isolada: U) (1º Parcela: A)
        processar_agrupamento(mapa_vendedor, 13, "Q", "S", "S", "B")
        processar_agrupamento(mapa_geral_ano, 12, "S", "U", "U", "A")

        duracao = time.time() - tempo_inicio
        logger_crm.info(
            "    [SHEETS BATCH CDA] Lote de %d atualizações processadas em %.2fs.",
            len(lote_dados),
            duracao,
        )

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_crm.error("    [SHEETS BATCH ERRO] Falha ao processar o lote: %s", e)


def disparar_limbo_async(nome_planilha, aba_original, contrato):
    """Grava o status 'Inacessível' em background sem bloquear o scanner."""
    try:
        sheet_vendedor = conectar_google_sheets(nome_planilha, aba_original)
        if sheet_vendedor:
            registrar_apenas_situacao_cliente(
                sheet_vendedor, contrato, "Inacessível", 13, 19
            )

        sheet_geral = conectar_google_sheets(f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL)
        if sheet_geral:
            registrar_apenas_situacao_cliente(
                sheet_geral, contrato, "Inacessível", 12, 21
            )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_crm.error("    [ERRO LIMBO ASYNC] Falha ao processar gravação: %s", e)


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

        logger_crm.info("[SISTEMA] Sessão HTTP aquecida com validação de módulo ASP.")
        return True
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_crm.error("[SISTEMA] Falha ao aquecer a sessão: %s", e)
        return False


def criar_sessao_hibrida() -> requests.Session:
    """Constrói sessão HTTP falsificando a identidade de um navegador real."""
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

    sessao_http.headers.update({"User-Agent": mascara_agente, "Accept": mascara_accept})

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
    """Loop principal ininterrupto do sistema corporativo (CDA)."""
    logger_crm.info("=== Motor CDA Iniciado ===")

    sessao_http = criar_sessao_hibrida()
    ultimo_login = time.time()
    executor_sheets = ThreadPoolExecutor(max_workers=1)
    executor_webhooks = ThreadPoolExecutor(max_workers=1)

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
            logger_crm.info("[SISTEMA] Reancorando sessão ao módulo de Atendimento...")
            aquecer_sessao_asp(sessao_http)
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
        buffer_atualizacoes = []

        # =====================================================================
        # 1. VARREDURA OFFLINE O(1) (Filtro RAM Estrito por Versão)
        # =====================================================================
        for contrato, info in adimplencia.items():
            if info.get("monitorar", True) is not False:
                grupo = limpar_inteiro(info.get("grupo"))
                cota_base = limpar_inteiro(info.get("cota"))
                versao = info.get("versao")
                nome_json = info.get("nome", "")

                status_ram = None
                versao_morta = None

                if versao is not None:
                    versao_desistente = versao + 40
                    resultado_ram = mapa_ram.get((grupo, cota_base, versao))

                    if resultado_ram:
                        status_ram = resultado_ram[0]
                        versao_morta = versao
                    else:
                        resultado_ram = mapa_ram.get(
                            (grupo, cota_base, versao_desistente)
                        )
                        if resultado_ram and resultado_ram[0] == "DESISTENTE":
                            nome_relatorio = resultado_ram[1]

                            if comparar_nomes(nome_json, nome_relatorio):
                                status_ram = resultado_ram[0]
                                versao_morta = versao_desistente

                if status_ram:
                    logger_crm.warning(
                        "[MUDANÇA] Contrato %s classificado %s via Filtro RAM Estrito (Versão: %s).",
                        contrato,
                        status_ram,
                        versao_morta,
                    )
                    buffer_atualizacoes.append(
                        {
                            "contrato": contrato,
                            "nome_planilha": info["nome_planilha"],
                            "aba_original": info["aba_original"],
                            "status_cliente": status_ram,
                            "apenas_situacao": True,
                        }
                    )
                    info["monitorar"] = False
                    info["ultimo_status"] = status_ram
                    mudou_json = True
                    continue

            if info.get("monitorar", True):
                ultima_verificacao = info.get("ultima_verificacao", 0)
                if (agora - ultima_verificacao) >= intervalo_reanalise:
                    fila_online.append((ultima_verificacao, contrato, info))

        if buffer_atualizacoes:
            executor_sheets.submit(tarefa_background_lote, buffer_atualizacoes.copy())
            buffer_atualizacoes.clear()

        if mudou_json:
            salvar_adimplencia(adimplencia)
            mudou_json = False

        fila_online.sort(key=lambda x: x[0])
        total_fila = len(fila_online)

        if total_fila == 0:
            time.sleep(5)
            continue

        logger_crm.info(
            "[ONLINE] Fila HTTP pronta: %d contratos aguardam auditoria.", total_fila
        )

        tempo_ultimo_lote = time.time()
        tamanho_lote = 20

        # VARIÁVEIS DO DISJUNTOR E DO BUFFER DE LIMBOS
        falhas_inexplicaveis = 0
        limbos_pendentes = []

        # =====================================================================
        # 2. AUDITORIA ONLINE (1ª Parcela e Longo Prazo)
        # =====================================================================
        for index, (_, contrato, info) in enumerate(fila_online, start=1):
            logger_crm.info(
                " -> Analisando [%d/%d] | Contrato: %s", index, total_fila, contrato
            )

            tempo_inicio_web = time.time()
            dados_rede = consultar_adimplencia_http_v2(sessao_http, contrato, info)
            duracao_web = time.time() - tempo_inicio_web

            if dados_rede["encontrou"]:

                falhas_inexplicaveis = 0
                info["falhas_consecutivas"] = 0
                info["ultima_verificacao"] = time.time()

                for lp in limbos_pendentes:
                    executor_sheets.submit(disparar_limbo_async, *lp)
                limbos_pendentes.clear()

                if dados_rede.get("cpf") and dados_rede["cpf"] != info.get("cpf"):
                    info["cpf"] = dados_rede["cpf"]

                nome_site = dados_rede.get("nome")
                if nome_site and info.get("nome") != nome_site:
                    info["nome"] = nome_site
                    mudou_json = True

                versao_site = dados_rede.get("versao")
                if versao_site is not None and info.get("versao") != versao_site:
                    info["versao"] = versao_site
                    mudou_json = True

                recuperou_do_limbo = False
                if info.get("data_limbo") is not None:
                    info["data_limbo"] = None
                    recuperou_do_limbo = True

                st_atual = dados_rede.get("status_pagamento", "N/A")
                pc_atual = dados_rede.get("parcelas_pagas", 0)
                st_anterior = info.get("ultimo_status")
                pc_anterior = info.get("ultimas_parcelas")

                is_pago_agora = pc_atual > 0

                # --- CHECK UNIFICADO: 1ª PARCELA ---
                if not info.get("primeira_parcela_paga"):
                    if is_pago_agora:
                        logger_crm.info(
                            "    [PAGAMENTO 1ª PARC] Contrato %s honrado.", contrato
                        )
                        buffer_atualizacoes.append(
                            {
                                "contrato": contrato,
                                "nome_planilha": info["nome_planilha"],
                                "aba_original": info["aba_original"],
                                "atualizar_primeira_parcela": True,
                            }
                        )
                        salvar_historico_concluido(
                            contrato,
                            info["nome_planilha"],
                            info.get("vendedor_nome", "-"),
                            info.get("vendedor_tel", "-"),
                            "1º Parcela Paga (Reanálise)",
                        )
                        info["primeira_parcela_paga"] = True
                        info["ultimas_parcelas"] = pc_atual
                        info["ultimo_status"] = st_atual
                        mudou_json = True
                    else:
                        agora_ts = time.time()
                        data_inc = info.get("data_inclusao", agora_ts)

                        if agora_ts - data_inc > 2592000:
                            logger_crm.warning(
                                "    [EXPIRADO] Contrato %s arquivado (30 dias sem 1ª parcela).",
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

                # --- DELTA CACHE DE ADIMPLÊNCIA (Longo Prazo) ---
                fotografia = f"[{st_atual} | {pc_atual} parc. | Ativo]"

                # Regra 1: Precisamos preencher as células vazias no Sheets?
                primeira_nao_paga = not info.get("primeira_parcela_paga")

                # Regra 2: Houve uma alteração real no status financeiro do cliente?
                houve_mutacao = (
                    st_atual != st_anterior
                    or pc_atual != pc_anterior
                    or recuperou_do_limbo
                )

                if houve_mutacao or primeira_nao_paga:

                    if recuperou_do_limbo:
                        logger_crm.info(
                            "    [RESSURREIÇÃO] %s saiu do Limbo.", contrato
                        )
                    elif houve_mutacao:
                        # Só grita 'Atualização' se a mutação for real
                        logger_crm.info(
                            "    [ATUALIZAÇÃO] %s -> %s. Adicionado ao cesto...",
                            contrato,
                            fotografia,
                        )
                    else:
                        # Aviso discreto de que está apenas a preencher a planilha
                        logger_crm.info(
                            "    [PLANILHA] Anotando %s pendente na base...", contrato
                        )

                    buffer_atualizacoes.append(
                        {
                            "contrato": contrato,
                            "nome_planilha": info["nome_planilha"],
                            "aba_original": info["aba_original"],
                            "status_pgto": st_atual,
                            "parcelas": pc_atual,
                            "status_cliente": "Ativo",
                            "apenas_situacao": False,
                        }
                    )

                    info["ultimo_status"] = st_atual
                    info["ultimas_parcelas"] = pc_atual

                    # =================================================================
                    # WEBHOOK DE ATUALIZAÇÃO PARA O SITE (Blindado)
                    # =================================================================
                    # Só avisa o site se o cliente pagar, atrasar, ou sair do limbo.

                    if houve_mutacao:
                        url_webhook = "https://autocredbrasil.app/api/webhook/cda"

                        headers_webhook = {
                            "Authorization": "Bearer K01GFBehTTheBFG10k",
                            "Content-Type": "application/json",
                        }

                        payload_webhook = {
                            "contrato": contrato,
                            "status_pagamento": st_atual,
                            "parcelas_pagas": pc_atual,
                            "status_cliente": "Ativo",
                        }

                        def disparar_webhook_em_background(url, dados, cabecalhos):
                            try:
                                # [CORREÇÃO] Injeção do parâmetro headers=cabecalhos
                                res = requests.post(
                                    url, json=dados, headers=cabecalhos, timeout=10
                                )
                                if res.status_code in (200, 201):
                                    logger_crm.info(
                                        "    [WEBHOOK SUCESSO] Site notificado sobre o contrato %s.",
                                        dados["contrato"],
                                    )
                                else:
                                    logger_crm.warning(
                                        "    [WEBHOOK AVISO] Site retornou código %s.",
                                        res.status_code,
                                    )
                            except requests.exceptions.RequestException:
                                logger_crm.warning(
                                    "    [ERRO WEBHOOK] O site não respondeu ao alerta."
                                )

                        # Passamos os cabecalhos como argumento extra para a thread
                        executor_webhooks.submit(
                            disparar_webhook_em_background,
                            url_webhook,
                            payload_webhook,
                            headers_webhook,
                        )
                    # =================================================================

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

                if motivo == "FALHA_REDE":
                    falhas_inexplicaveis += 1
                    logger_crm.warning(
                        "    [INSTABILIDADE] O servidor Autocred falhou silenciosamente."
                    )
                    info["ultima_verificacao"] = time.time()
                    mudou_json = True

                elif motivo == "NAO_ENCONTRADO":
                    info["ultima_verificacao"] = time.time()
                    resolvido_na_cura = False

                    # --- AUTO-CURA INTELIGENTE POR NOME ---
                    grupo = limpar_inteiro(info.get("grupo"))
                    cota_base = limpar_inteiro(info.get("cota"))
                    nome_cliente_json = info.get("nome", "")

                    if nome_cliente_json:
                        for (g_map, c_map, v_map), (
                            st_map,
                            nome_map,
                        ) in mapa_ram.items():
                            if g_map == grupo and c_map == cota_base:
                                if comparar_nomes(nome_cliente_json, nome_map):
                                    logger_crm.warning(
                                        "[AUTO-CURA] Contrato %s validado como %s via cruzamento de Nome.",
                                        contrato,
                                        st_map,
                                    )
                                    info["versao"] = v_map
                                    info["monitorar"] = False
                                    info["ultimo_status"] = st_map
                                    mudou_json = True

                                    buffer_atualizacoes.append(
                                        {
                                            "contrato": contrato,
                                            "nome_planilha": info["nome_planilha"],
                                            "aba_original": info["aba_original"],
                                            "status_cliente": st_map,
                                            "apenas_situacao": True,
                                        }
                                    )
                                    resolvido_na_cura = True

                                    # Se foi curado, não é culpa do servidor.
                                    falhas_inexplicaveis = 0
                                    for lp in limbos_pendentes:
                                        executor_sheets.submit(
                                            disparar_limbo_async, *lp
                                        )
                                    limbos_pendentes.clear()
                                    break

                    # --- TOLERÂNCIA A FALHAS (REGRA DOS 3 STRIKES) ---
                    if not resolvido_na_cura:
                        falhas_inexplicaveis += 1
                        falhas = info.get("falhas_consecutivas", 0) + 1
                        info["falhas_consecutivas"] = falhas

                        if falhas >= 3:
                            if not info.get("data_limbo"):
                                info["data_limbo"] = time.time()
                                logger_crm.warning(
                                    "    [LIMBO PREPARADO] Contrato %s atingiu 3 alertas.",
                                    contrato,
                                )
                                # Em vez de escrever Inacessível direto, guarda no buffer do Disjuntor
                                limbos_pendentes.append(
                                    (
                                        info["nome_planilha"],
                                        info["aba_original"],
                                        contrato,
                                    )
                                )
                            else:
                                dias = int((time.time() - info["data_limbo"]) / 86400)
                                if dias > limite_dias_inativo:
                                    logger_crm.error(
                                        "    [EXPIRADO NO LIMBO] Contrato %s.", contrato
                                    )
                                    info["monitorar"] = False
                                else:
                                    # [NOVO] Log explícito
                                    logger_crm.info(
                                        "    [LIMBO] Contrato %s ignorado silenciosamente (Falha %d).",
                                        contrato,
                                        falhas,
                                    )
                        else:
                            logger_crm.warning(
                                "    [GLITCH SERVER] Contrato %s não encontrado (Alerta %d/3). Mantendo Ativo.",
                                contrato,
                                falhas,
                            )

                    mudou_json = True

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
            mudou_json = False

            # =================================================================
            # O DISJUNTOR GLOBAL (CIRCUIT BREAKER DE MADRUGADA)
            # =================================================================
            if falhas_inexplicaveis >= 7:
                logger_crm.error(
                    "    [CIRCUIT BREAKER] 7 Falhas consecutivas. Manutenção da Autocred detectada!"
                )

                # Despacha o que tiver de sucesso antes de abortar
                if buffer_atualizacoes:
                    executor_sheets.submit(
                        tarefa_background_lote, buffer_atualizacoes.copy()
                    )
                    buffer_atualizacoes.clear()

                # Esconde os limbos para não riscar a planilha
                limbos_pendentes.clear()
                logger_crm.info(
                    "    [CIRCUIT BREAKER] Desfazendo alertas injustos e pausando motor por 20 min..."
                )

                # Reverte os strikes dos 7 coitados no JSON
                bd_revert = carregar_adimplencia()
                inicio_revert = max(0, index - falhas_inexplicaveis)
                for i_revert in range(inicio_revert, index):
                    c_revert = fila_online[i_revert][1]
                    if c_revert in bd_revert:
                        f_atual = bd_revert[c_revert].get("falhas_consecutivas", 1)
                        bd_revert[c_revert]["falhas_consecutivas"] = max(0, f_atual - 1)

                        limbo_ts = bd_revert[c_revert].get("data_limbo")
                        if limbo_ts and (time.time() - limbo_ts < 300):
                            bd_revert[c_revert]["data_limbo"] = None

                salvar_adimplencia(bd_revert)

                # Força a destruição da sessão para re-login depois de 20 minutos (1200 segs)
                sessao_http = None
                time.sleep(120)
                break

        # Fora do Loop For, no fim da fila, descarrega o que sobrou no limbo (se o servidor não caiu)
        for lp in limbos_pendentes:
            executor_sheets.submit(disparar_limbo_async, *lp)
        limbos_pendentes.clear()

        if buffer_atualizacoes:
            logger_crm.info(
                "    [DISPARO FINAL] Enviando últimos %d contratos...",
                len(buffer_atualizacoes),
            )
            executor_sheets.submit(tarefa_background_lote, buffer_atualizacoes.copy())
            buffer_atualizacoes.clear()


if __name__ == "__main__":
    loop_reanalise_adimplencia()
