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

# Importações dos módulos gerenciadores de dados e navegação
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
)

from motor_navegacao import (
    iniciar_navegador,
    buscar_contrato,
    extrair_dados_completos,
    verificar_apenas_pagamento,
    manter_sessao_viva,
    fazer_login_com_ia,
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

ESTADO = {"autenticado": False, "ultimo_keep_alive": time.time(), "ultimo_login": 0}


def garantir_sessao():
    """
    Verifica a integridade da sessão web e garante o ciclo de 4 horas.
    Caso o portal esteja desconectado ou o tempo tenha expirado, aciona a
    rotina de Inteligência Artificial para efetuar um novo login na página inicial.
    """
    agora = time.time()
    # 14400 segundos equivalem a exatamente 4 horas
    precisa_relogin = (agora - ESTADO.get("ultimo_login", 0)) > 14400

    if not ESTADO["autenticado"] or precisa_relogin:
        if precisa_relogin and ESTADO["autenticado"]:
            logger.info(
                "[SISTEMA] Ciclo de 4 horas atingido. Executando re-login por segurança."
            )

        if fazer_login_com_ia(driver_global):
            logger.info("[LIBERADO] Acesso restabelecido via Inteligência Artificial.")
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
    """
    Endpoint HTTP POST. Realiza a validação do Bearer Token, previne a
    duplicidade de contratos e comanda o motor Selenium.
    """
    auth_header = request.headers.get("Authorization")
    if auth_header != TOKEN_API_ESPERADO:
        logger.warning("Tentativa de acesso não autorizada. Token: %s", auth_header)
        return jsonify({"erro": "Acesso não autorizado."}), 403

    dados = request.json
    if not dados:
        return jsonify({"erro": "O payload da requisição está vazio."}), 400

    contrato = str(dados.get("contrato")).strip()
    vendedor = str(dados.get("vendedor")).strip()
    telefone_vendedor = str(dados.get("telefone")).strip()
    origem = str(dados.get("origem", "")).strip()

    logger.info("API RECEBIDA | Contrato: %s | Vendedor: %s", contrato, vendedor)

    if verificar_contrato_registrado(contrato):
        logger.info(
            "RECUSADO | O contrato %s já consta no sistema (Duplicidade).", contrato
        )
        return jsonify({"erro": "Este contrato já foi registrado anteriormente."}), 409

    with navegador_lock:
        try:
            garantir_sessao()
            ESTADO["ultimo_keep_alive"] = time.time()

            nome_planilha_vendedor = f"{PREFIXO_PLANILHA}{vendedor}"
            nome_planilha_geral = f"{PREFIXO_PLANILHA}GERAL"

            # Obtém dinamicamente o mês atualizado para não gravar no mês antigo
            aba_atual = obter_mes_utc4()

            sheet_vend = conectar_google_sheets(nome_planilha_vendedor, aba_atual)
            sheet_geral = conectar_google_sheets(nome_planilha_geral, NOME_ABA_GERAL)

            # Lógica de Busca com Contingência Única
            encontrou_contrato = buscar_contrato(driver_global, contrato)
            if not encontrou_contrato:
                logger.warning(
                    "Contrato %s não localizado. Acionando contingência de re-login na API...",
                    contrato,
                )
                ESTADO["autenticado"] = False
                garantir_sessao()
                encontrou_contrato = buscar_contrato(driver_global, contrato)

            if encontrou_contrato:
                dados_site = extrair_dados_completos(driver_global)
                anotou_vend = False
                anotou_geral = False

                if sheet_vend:
                    try:
                        atualizar_planilha_vendedor(
                            sheet_vend, dados, dados_site, contrato
                        )
                        anotou_vend = True
                    except (
                        Exception  # pylint: disable=broad-exception-caught
                    ) as e_vend:
                        logger.error("Falha na planilha do vendedor: %s", e_vend)

                if sheet_geral:
                    try:
                        atualizar_planilha_geral(
                            sheet_geral, dados, dados_site, contrato
                        )
                        anotou_geral = True
                    except (
                        Exception  # pylint: disable=broad-exception-caught
                    ) as e_geral:
                        logger.error("Falha na planilha GERAL: %s", e_geral)

                if anotou_vend or anotou_geral:
                    destino_log = (
                        nome_planilha_vendedor if anotou_vend else nome_planilha_geral
                    )
                    texto_st = (
                        "1º Parcela Paga"
                        if dados_site["pago"]
                        else "1º Parcela Não Paga"
                    )

                    salvar_historico_concluido(
                        contrato, destino_log, vendedor, telefone_vendedor, texto_st
                    )

                    logger.info(
                        "[SUCESSO API] Contrato %s processado e gravado na planilha '%s'. Status: %s",
                        contrato,
                        destino_log,
                        texto_st,
                    )

                    if not dados_site["pago"]:
                        adicionar_para_reanalise(
                            contrato,
                            vendedor,
                            telefone_vendedor,
                            nome_planilha_vendedor,
                            origem,
                            dados,
                        )
                        logger.info(
                            "   -> Contrato %s inserido na fila de reanálise de 30 dias.",
                            contrato,
                        )

                    return (
                        jsonify(
                            {
                                "sucesso": True,
                                "status_pagamento": texto_st,
                                "planilha": destino_log,
                            }
                        ),
                        200,
                    )

                logger.error(
                    "API RECUSADA | Falha ao tentar escrever na API do Google Sheets para o contrato %s.",
                    contrato,
                )
                return jsonify({"erro": "Falha na escrita da API do Google."}), 500

            logger.error(
                "API RECUSADA | Contrato %s definitivamente não localizado após contingência.",
                contrato,
            )
            return jsonify({"erro": "Contrato não localizado no portal."}), 404

        except Exception as e_motor:  # pylint: disable=broad-exception-caught
            logger.error("Erro interno do motor de execução: %s", str(e_motor))
            return jsonify({"erro": f"Erro interno do motor: {str(e_motor)}"}), 500


# ============================================================================
# ROTINA DE BACKGROUND (REANÁLISE DE 1 HORA / 30 DIAS)
# ============================================================================
def loop_reanalise_background():
    """
    Rotina em segundo plano. Varre o arquivo de pendentes a cada 1 hora.
    Se o contrato não for pago em 30 dias, ele é removido da fila.
    """
    logger.info("Motor de Reanálise Independente Iniciado.")

    while True:
        time.sleep(60)

        with navegador_lock:
            try:
                pendentes = carregar_pendentes()
                mudou_pendentes = False

                if pendentes and not ESTADO["autenticado"]:
                    try:
                        garantir_sessao()
                    except Exception:  # pylint: disable=broad-exception-caught
                        continue

                for contrato, info in list(pendentes.items()):
                    agora = time.time()
                    ultima_verificacao = info.get("ultima_verificacao", 0)

                    data_inclusao = info.get("data_inclusao", agora)
                    if "data_inclusao" not in info:
                        info["data_inclusao"] = data_inclusao
                        mudou_pendentes = True

                    # Limite de 30 dias (2.592.000 segundos)
                    if agora - data_inclusao > 2592000:
                        logger.warning(
                            "EXPIROU | Contrato %s atingiu o prazo máximo de 30 dias sem pagamento. Removido.",
                            contrato,
                        )
                        del pendentes[contrato]
                        mudou_pendentes = True
                        continue

                    # Intervalo de reanálise de 1 HORA (3600 segundos)
                    if agora - ultima_verificacao < 3600:
                        continue

                    ESTADO["ultimo_keep_alive"] = agora
                    info["tentativas"] = info.get("tentativas", 0) + 1
                    info["ultima_verificacao"] = agora
                    mudou_pendentes = True

                    logger.info("REANÁLISE | Verificando %s...", contrato)

                    # Lógica de Busca com Contingência Única
                    encontrou_contrato = buscar_contrato(driver_global, contrato)
                    if not encontrou_contrato:
                        logger.warning(
                            "Contrato %s não encontrado na reanálise. Acionando contingência de re-login...",
                            contrato,
                        )
                        ESTADO["autenticado"] = False
                        garantir_sessao()
                        encontrou_contrato = buscar_contrato(driver_global, contrato)

                    if encontrou_contrato:
                        if verificar_apenas_pagamento(driver_global):
                            logger.info(
                                "PAGAMENTO DETECTADO | Atualizando status do contrato %s.",
                                contrato,
                            )
                            anotou_v, anotou_g = False, False

                            # Obtém dinamicamente o mês atualizado para atualizar a aba certa
                            aba_atual = obter_mes_utc4()

                            sheet_v = conectar_google_sheets(
                                info["nome_planilha"], aba_atual
                            )
                            if sheet_v:
                                l_v = encontrar_linha_do_contrato(
                                    sheet_v, contrato, col_idx=13
                                )
                                if l_v:
                                    try:
                                        sheet_v.update_cell(l_v, 2, "1º Parcela Paga")
                                        anotou_v = True
                                    except (
                                        Exception  # pylint: disable=broad-exception-caught
                                    ):
                                        pass

                            sheet_g = conectar_google_sheets(
                                f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                            )
                            if sheet_g:
                                l_g = encontrar_linha_do_contrato(
                                    sheet_g, contrato, col_idx=12
                                )
                                if l_g:
                                    try:
                                        sheet_g.update_cell(l_g, 1, "1º Parcela Paga")
                                        anotou_g = True
                                    except (
                                        Exception  # pylint: disable=broad-exception-caught
                                    ):
                                        pass

                            if anotou_v or anotou_g:
                                dest = (
                                    info["nome_planilha"]
                                    if anotou_v
                                    else f"{PREFIXO_PLANILHA}GERAL"
                                )
                                salvar_historico_concluido(
                                    contrato,
                                    dest,
                                    info["vendedor_nome"],
                                    info["vendedor_tel"],
                                    "1º Parcela Paga (Reanálise)",
                                )
                                del pendentes[contrato]
                    else:
                        logger.warning(
                            "NÃO ENCONTRADO | Cota %s definitivamente indisponível após contingência. Removida da fila.",
                            contrato,
                        )
                        del pendentes[contrato]

                if mudou_pendentes:
                    salvar_pendentes(pendentes)

                if ESTADO["autenticado"]:
                    tempo_inativo = time.time() - ESTADO["ultimo_keep_alive"]
                    if tempo_inativo > TEMPO_INATIVIDADE_MAXIMO:
                        if not manter_sessao_viva(driver_global):
                            logger.warning(
                                "Keep-Alive falhou. Necessário Relogin futuro."
                            )
                            ESTADO["autenticado"] = False
                        ESTADO["ultimo_keep_alive"] = time.time()

            except Exception as e_bg:  # pylint: disable=broad-exception-caught
                logger.error("Erro no loop de background protegido: %s", str(e_bg))


# ============================================================================
# PONTO DE ENTRADA DA APLICAÇÃO (SERVIDOR WSGI PRODUCTION)
# ============================================================================
if __name__ == "__main__":
    logger.info(">>> INICIANDO SISTEMA ENTERPRISE V6 (WAITRESS WSGI + SELENIUM) <<<")
    driver_global = iniciar_navegador()

    logger.info("Executando login imediato na inicialização do sistema...")
    with navegador_lock:
        try:
            garantir_sessao()
        except Exception as e_inicial:  # pylint: disable=broad-exception-caught
            logger.error(
                "Não foi possível efetuar o login automático inicial: %s", e_inicial
            )

    thread_background = threading.Thread(target=loop_reanalise_background, daemon=True)
    thread_background.start()

    logger.info(
        "Servidor WSGI de produção (Waitress) iniciado com sucesso na porta 5000."
    )
    serve(app, host="0.0.0.0", port=5000)
