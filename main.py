"""
Motor Principal de Automação de Consórcio V6.6 (HTTP Puro + Rate Limit Inteligente).

Implementa pausas dinâmicas na reanálise de pendentes:
- 10 segundos durante o pico da manhã (até 2h após o 1º login pós-hibernação).
- 3 minutos (180s) no restante do dia.
"""

import time
import threading
import logging
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify
from waitress import serve
import requests

from motor_navegacao import (
    iniciar_navegador,
    fazer_login_com_ia,
    consultar_venda_http_completa,
    migrar_para_adimplencia,
)

from gerador_dados import (
    PREFIXO_PLANILHA,
    NOME_ABA_GERAL,
    TEMPO_INATIVIDADE_MAXIMO,
    obter_mes_utc4,
    conectar_google_sheets,
    atualizar_planilha_vendedor,
    atualizar_planilha_geral,
    carregar_pendentes,
    salvar_pendentes,
    salvar_historico_concluido,
    verificar_contrato_registrado,
    adicionar_para_reanalise,
    encontrar_linha_do_contrato,
)


# ============================================================================
# CONFIGURAÇÃO DE LOGGING
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

lock_api = threading.Lock()
TOKEN_API_ESPERADO = "Bearer CHAVE_SECRETA_ENTERPRISE_V6"

ESTADO_API = {
    "sessao_http": None,
    "autenticado": False,
    "ultimo_keep_alive": time.time(),
    "ultimo_login": 0,
    "primeiro_login_dia": 0,  # Marca o início do "pico da manhã"
    "cooldown_login": 0,
}


def is_hibernando() -> bool:
    """Verifica se o sistema deve estar congelado (21:00 - 06:30 UTC-4)."""
    fuso_utc4 = timezone(timedelta(hours=-4))
    agora = datetime.now(fuso_utc4)
    hora_decimal = agora.hour + (agora.minute / 60.0)
    return hora_decimal >= 21 or hora_decimal < 6.5


def aquecer_sessao_asp(sessao: requests.Session):
    """Ancora os cookies forjados acessando as molduras primárias do portal."""
    try:
        url_master = (
            "https://intranet.consorciotradicao.com.br/autocred/MasterFrameset.asp"
        )
        url_left = "https://intranet.consorciotradicao.com.br/autocred/LeftFrame.asp?codigo_modulo=AG"
        sessao.get(url_master, timeout=10)
        sessao.get(url_left, timeout=10)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_api.warning("Aviso durante aquecimento da sessão ASP: %s", e)


def garantir_sessao_http():
    """Cria ou renova a sessão HTTP usando Selenium efêmero para transbordo."""
    agora = time.time()
    precisa_relogin = (agora - ESTADO_API["ultimo_login"]) > 14000  # Aprox 4 horas

    if (
        not ESTADO_API["autenticado"]
        or precisa_relogin
        or ESTADO_API["sessao_http"] is None
    ):
        logger_api.info(
            "[SISTEMA] Sessão ausente ou expirada. Invocando IA de Login..."
        )
        driver_temp = iniciar_navegador()

        try:
            if fazer_login_com_ia(driver_temp):
                nova_sessao = requests.Session()
                nova_sessao.headers.update(
                    {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    }
                )

                for cookie in driver_temp.get_cookies():
                    nova_sessao.cookies.set(
                        cookie["name"], cookie["value"], domain=cookie["domain"]
                    )

                aquecer_sessao_asp(nova_sessao)

                ESTADO_API["sessao_http"] = nova_sessao
                ESTADO_API["autenticado"] = True
                ESTADO_API["ultimo_keep_alive"] = agora
                ESTADO_API["ultimo_login"] = agora

                # ==========================================================
                # NOVO CONTROLE: Impede aceleração se reiniciar de tarde
                # ==========================================================
                if ESTADO_API.get("primeiro_login_dia", 0) == 0:
                    fuso_utc4 = timezone(timedelta(hours=-4))
                    hora_atual = datetime.now(fuso_utc4).hour

                    if hora_atual < 12:  # Só ativa o modo turbo se for de manhã
                        ESTADO_API["primeiro_login_dia"] = agora
                        logger_api.info(
                            "[SISTEMA] Início do pico da manhã detetado. Cadência rápida (10s) ativada por 2 horas."
                        )
                    else:
                        # Empurra o relógio 3 horas para trás para que a regra dos 7200s (2h) dê falso imediatamente
                        ESTADO_API["primeiro_login_dia"] = agora - 10000
                        logger_api.info(
                            "[SISTEMA] Reinício fora do pico da manhã. Cadência normal (3 min) ativada."
                        )
                # ==========================================================

                logger_api.info(
                    "[LIBERADO] Sessão ancorada em HTTP. Selenium Destruído."
                )
            else:
                logger_api.error("[BARRADO] Falha crítica de login na IA.")
                raise ConnectionError("Falha de autenticação IA no portal Autocred.")
        finally:
            driver_temp.quit()


# ============================================================================
# ROTA DA API (RECEPÇÃO DE PEDIDOS DO WHATSAPP)
# ============================================================================
@app.route("/processar_venda", methods=["POST"])
def processar_venda():
    if is_hibernando():
        return (
            jsonify(
                {"erro": "hibernacao", "mensagem": "Sistema em manutenção diária."}
            ),
            503,
        )

    auth_header = request.headers.get("Authorization")
    if auth_header != TOKEN_API_ESPERADO:
        return jsonify({"erro": "nao_autorizado", "mensagem": "Acesso negado."}), 403

    dados = request.json
    if not dados:
        return jsonify({"erro": "payload_vazio", "mensagem": "Payload vazio."}), 400

    contrato = str(dados.get("contrato")).strip()
    vendedor = str(dados.get("vendedor")).strip()
    telefone_vendedor = str(dados.get("telefone")).strip()
    origem = str(dados.get("origem", "")).strip()

    logger_api.info("API WHATSAPP | Contrato: %s | Vendedor: %s", contrato, vendedor)

    if not lock_api.acquire(timeout=10):
        logger_api.warning(
            "COLISÃO HTTP: O sistema está ocupado processando outra requisição."
        )
        return (
            jsonify(
                {
                    "erro": "sistema_ocupado",
                    "mensagem": "O portal está processando outra requisição. Tente novamente.",
                }
            ),
            429,
        )

    try:
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

        nome_planilha_vendedor = f"{PREFIXO_PLANILHA}{vendedor}"
        nome_planilha_geral = f"{PREFIXO_PLANILHA}GERAL"
        aba_atual = obter_mes_utc4()

        sheet_vend = conectar_google_sheets(nome_planilha_vendedor, aba_atual)
        sheet_geral_ano = conectar_google_sheets(nome_planilha_geral, NOME_ABA_GERAL)

        erros_infra = []
        if not sheet_vend:
            erros_infra.append(f"Planilha Vendedor ({nome_planilha_vendedor})")
        if not sheet_geral_ano:
            erros_infra.append(f"Planilha GERAL -> Aba: {NOME_ABA_GERAL}")

        if erros_infra:
            msg_erro = f"Infraestrutura inválida. Falta: {', '.join(erros_infra)}."
            logger_api.error("[ERRO INFRAESTRUTURA] %s", msg_erro)
            return jsonify({"erro": "planilha_ausente", "mensagem": msg_erro}), 404

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
            anotou_vend = False
            anotou_geral_ano = False

            try:
                atualizar_planilha_vendedor(sheet_vend, dados, dados_site, contrato)
                anotou_vend = True
            except Exception as e_vend:  # pylint: disable=broad-exception-caught
                logger_api.error("Falha na gravação do vendedor: %s", e_vend)

            try:
                atualizar_planilha_geral(sheet_geral_ano, dados, dados_site, contrato)
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
                    "[SUCESSO HTTP] Contrato %s gravado no sistema.", contrato
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
                return (
                    jsonify(
                        {
                            "erro": "falha_gravacao",
                            "mensagem": "Erro de gravação física.",
                        }
                    ),
                    500,
                )

        logger_api.error(
            "API RECUSADA HTTP | Contrato %s não localizado. Motivo: %s",
            contrato,
            resultado.get("motivo"),
        )
        return (
            jsonify({"erro": "contrato_nao_encontrado", "mensagem": "Não localizado."}),
            404,
        )

    except Exception as e_motor:  # pylint: disable=broad-exception-caught
        logger_api.error("Erro interno do motor HTTP: %s", e_motor)
        return jsonify({"erro": "erro_interno", "mensagem": str(e_motor)}), 500
    finally:
        lock_api.release()


# ============================================================================
# ROTINA DE BACKGROUND (REANÁLISE DE 1 HORA / RATE LIMIT INTELIGENTE)
# ============================================================================
def loop_reanalise_background():
    """Varre a fila de 1ª parcela aplicando cadências de segurança no HTTP."""
    logger_api.info("Motor de Reanálise Independente (HTTP) Iniciado.")

    while True:
        time.sleep(5)  # Pequeno respiro base para não sobrecarregar CPU

        if is_hibernando():
            with lock_api:
                if ESTADO_API["sessao_http"] is not None:
                    logger_api.info("[HIBERNAÇÃO] Destruindo Sessão HTTP...")
                    try:
                        ESTADO_API["sessao_http"].close()
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
                    ESTADO_API["sessao_http"] = None
                    ESTADO_API["autenticado"] = False
                    ESTADO_API["ultimo_login"] = 0
                    ESTADO_API["primeiro_login_dia"] = 0  # Reseta a flag da manhã

            logger_api.info("[HIBERNAÇÃO] Sistema dormindo...")
            time.sleep(300)
            continue

        if time.time() < ESTADO_API.get("cooldown_login", 0):
            continue

        try:
            with lock_api:
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

                if agora - data_inclusao > 2592000:
                    logger_api.warning("EXPIROU | Contrato %s removido.", contrato)
                    del pend_limpeza[contrato]
                    mudou_limpeza = True

            if mudou_limpeza:
                salvar_pendentes(pend_limpeza)

            chaves_para_verificar = list(carregar_pendentes().keys())

            for contrato in chaves_para_verificar:
                if is_hibernando():
                    break

                pendentes_frescos = carregar_pendentes()
                if contrato not in pendentes_frescos:
                    continue

                info = pendentes_frescos[contrato]
                ultima_verificacao = info.get("ultima_verificacao", 0)

                # Mantém a regra de 1 hora
                if time.time() - ultima_verificacao < 3600:
                    continue

                pesquisa_realizada = False

                with lock_api:
                    if not ESTADO_API["autenticado"]:
                        try:
                            garantir_sessao_http()
                        except Exception:  # pylint: disable=broad-exception-caught
                            logger_api.warning(
                                "Falha ao ancorar sessão. Cooldown de 10 minutos (IA BARRADA)."
                            )
                            ESTADO_API["cooldown_login"] = time.time() + 600
                            break

                    ESTADO_API["ultimo_keep_alive"] = time.time()
                    logger_api.info("REANÁLISE HTTP | Verificando %s...", contrato)

                    pesquisa_realizada = True
                    resultado = consultar_venda_http_completa(
                        ESTADO_API["sessao_http"], contrato
                    )

                    if resultado["motivo"] == "SESSAO_CAIU":
                        ESTADO_API["autenticado"] = False
                        # Se caiu, o ciclo irá tentar novamente. Não aplicamos a pausa agora.
                        pesquisa_realizada = False
                        continue

                    sucesso_verificacao = resultado["encontrou"]
                    dados_completos = resultado.get("dados", {})
                    pago = dados_completos.get("pago", False)

                    if not sucesso_verificacao:
                        logger_api.warning(
                            "NÃO ENCONTRADO | Cota %s indisponível no portal.", contrato
                        )
                        pend_atual = carregar_pendentes()
                        if contrato in pend_atual:
                            pend_atual[contrato]["falhas_site"] = (
                                pend_atual[contrato].get("falhas_site", 0) + 1
                            )
                            pend_atual[contrato]["ultima_verificacao"] = time.time()
                            salvar_pendentes(pend_atual)

                    if sucesso_verificacao:
                        pend_final = carregar_pendentes()
                        if contrato in pend_final:
                            if pago:
                                logger_api.info(
                                    "PAGAMENTO DETECTADO | Contrato %s.", contrato
                                )
                                aba_salva = info.get("aba_original", obter_mes_utc4())
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
                                                "[SUCESSO REANÁLISE HTTP] Contrato %s transferido ao CDA.",
                                                contrato,
                                            )
                                        except (
                                            Exception  # pylint: disable=broad-exception-caught
                                        ) as e_sheet:
                                            logger_api.error(
                                                "Falha na gravação do Sheets %s: %s",
                                                contrato,
                                                e_sheet,
                                            )
                            else:
                                pend_final[contrato]["ultima_verificacao"] = time.time()
                                pend_final[contrato]["tentativas"] = (
                                    pend_final[contrato].get("tentativas", 0) + 1
                                )
                            salvar_pendentes(pend_final)

                # ========================================================
                # CONTROLE DE CADÊNCIA (Fora do Cadeado)
                # ========================================================
                if pesquisa_realizada:
                    tempo_ativo = time.time() - ESTADO_API.get("primeiro_login_dia", 0)

                    # Se estamos nas primeiras 2 horas (7200s) após o primeiro login bem sucedido
                    if (
                        ESTADO_API.get("primeiro_login_dia", 0) > 0
                        and tempo_ativo <= 7200
                    ):
                        espera = 10
                        modo = "Pico da Manhã (10s)"
                    else:
                        espera = 180
                        modo = "Normal (3 min)"

                    logger_api.info(
                        "Cadência Ativa [%s]: Aguardando o próximo contrato...", modo
                    )
                    time.sleep(espera)
                # ========================================================

            with lock_api:
                if ESTADO_API["autenticado"] and ESTADO_API["sessao_http"]:
                    tempo_inativo = time.time() - ESTADO_API["ultimo_keep_alive"]
                    if tempo_inativo > TEMPO_INATIVIDADE_MAXIMO:
                        try:
                            logger_api.info("Executando Keep-Alive na sessão HTTP...")
                            url_ka = "https://intranet.consorciotradicao.com.br/autocred/LeftFrame.asp?codigo_modulo=AG"
                            res = ESTADO_API["sessao_http"].get(url_ka, timeout=10)
                            if (
                                "j_username" in res.text.lower()
                                or res.status_code != 200
                            ):
                                logger_api.warning(
                                    "Sessão HTTP expirada no portal. Será forjada nova sessão no próximo ciclo."
                                )
                                ESTADO_API["autenticado"] = False
                            else:
                                ESTADO_API["ultimo_keep_alive"] = time.time()
                                logger_api.info("Keep-Alive concluído com sucesso.")
                        except (
                            Exception  # pylint: disable=broad-exception-caught
                        ) as e_ka:
                            logger_api.warning(
                                "Falha de rede no Keep-Alive: %s", str(e_ka)
                            )
                            ESTADO_API["autenticado"] = False

        except Exception as e_bg:  # pylint: disable=broad-exception-caught
            logger_api.error("Erro no loop de background HTTP: %s", str(e_bg))


if __name__ == "__main__":
    logger_api.info(
        ">>> INICIANDO SISTEMA ENTERPRISE V6.6 (API HTTP PURA + CADÊNCIA INTELIGENTE) <<<"
    )

    if not is_hibernando():
        try:
            logger_api.info("Executando login imediato...")
            garantir_sessao_http()
        except Exception as e_inicial:  # pylint: disable=broad-exception-caught
            logger_api.error("Portal indisponível na inicialização: %s", e_inicial)

    threading.Thread(target=loop_reanalise_background, daemon=True).start()

    logger_api.info("Servidor WSGI pronto na porta 5000.")
    serve(app, host="0.0.0.0", port=5000)
