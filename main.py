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
)

from motor_navegacao import (
    iniciar_navegador,
    buscar_contrato,
    extrair_dados_completos,
    verificar_apenas_pagamento,
    manter_sessao_viva,
    fazer_login_com_ia,
)

from cda_modulos import migrar_para_adimplencia

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
@app.route("/processar_venda", methods=["POST"])
def processar_venda():
    """
    Endpoint HTTP POST. Realiza a validação do Bearer Token, previne a
    duplicidade de contratos e comanda o motor Selenium.
    """
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
        logger.info(
            "RECUSADO | O contrato %s já consta no sistema (Duplicidade).", contrato
        )
        return (
            jsonify(
                {
                    "erro": "duplicidade",
                    "mensagem": f"O contrato {contrato} já foi processado e gravado anteriormente no sistema.",
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
                erros_infra.append(
                    f"Planilha do Vendedor ({nome_planilha_vendedor}) -> Aba: {aba_atual}"
                )
            if not sheet_geral_ano:
                erros_infra.append(f"Planilha GERAL -> Aba: {NOME_ABA_GERAL}")
            if not sheet_geral_mes:
                erros_infra.append(f"Planilha GERAL -> Aba: {aba_atual}")

            if erros_infra:
                msg_erro = f"Infraestrutura inválida. Não foi possível localizar: {', '.join(erros_infra)}."
                logger.error("[ERRO INFRAESTRUTURA] %s", msg_erro)
                return jsonify({"erro": "planilha_ausente", "mensagem": msg_erro}), 404

            garantir_sessao()
            ESTADO["ultimo_keep_alive"] = time.time()

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
                anotou_geral_ano = False
                anotou_geral_mes = False

                try:
                    atualizar_planilha_vendedor(sheet_vend, dados, dados_site, contrato)
                    anotou_vend = True
                except Exception as e_vend:  # pylint: disable=broad-exception-caught
                    logger.error(
                        "Falha na gravação da planilha do vendedor: %s", e_vend
                    )

                try:
                    atualizar_planilha_geral(
                        sheet_geral_ano, dados, dados_site, contrato
                    )
                    anotou_geral_ano = True
                except (
                    Exception  # pylint: disable=broad-exception-caught
                ) as e_geral_ano:
                    logger.error(
                        "Falha na gravação da planilha GERAL (Aba Anual): %s",
                        e_geral_ano,
                    )

                try:
                    atualizar_planilha_geral(
                        sheet_geral_mes, dados, dados_site, contrato
                    )
                    anotou_geral_mes = True
                except (
                    Exception  # pylint: disable=broad-exception-caught
                ) as e_geral_mes:
                    logger.error(
                        "Falha na gravação da planilha GERAL (Aba Mensal): %s",
                        e_geral_mes,
                    )

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

                    logger.info(
                        "[SUCESSO API] Contrato %s gravado com sucesso em todas as planilhas e abas.",
                        contrato,
                    )

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
                        logger.info(
                            "   -> Contrato %s inserido na fila de reanálise de 30 dias.",
                            contrato,
                        )
                    else:
                        # [PONTE CDA] Contrato pago na hora vai direto para monitorização do CRM
                        migrar_para_adimplencia(
                            contrato,
                            {
                                "vendedor_nome": vendedor,
                                "nome_planilha": nome_planilha_vendedor,
                                "aba_original": aba_atual,
                            },
                            str(dados_site.get("grupo", "")),
                            str(dados_site.get("cota", "")),
                        )
                        logger.info(
                            "   -> Contrato %s transferido para o CDA.", contrato
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
                        erros_gravacao.append("Planilha GERAL (Aba Anual)")
                    if not anotou_geral_mes:
                        erros_gravacao.append("Planilha GERAL (Aba Mensal)")

                    msg_falha = f"Falha na gravação física dos dados. Erro ao atualizar: {', '.join(erros_gravacao)}."
                    logger.error("[ERRO GRAVAÇÃO] %s", msg_falha)
                    return (
                        jsonify({"erro": "falha_gravacao", "mensagem": msg_falha}),
                        500,
                    )

            logger.error(
                "API RECUSADA | Contrato %s definitivamente não localizado após contingência.",
                contrato,
            )
            return (
                jsonify(
                    {
                        "erro": "contrato_nao_encontrado",
                        "mensagem": f"O contrato {contrato} não foi localizado no portal Autocred.",
                    }
                ),
                404,
            )

        except Exception as e_motor:  # pylint: disable=broad-exception-caught
            logger.error("Erro interno do motor de execução: %s", str(e_motor))
            return (
                jsonify(
                    {
                        "erro": "erro_interno",
                        "mensagem": f"Erro interno no motor Python: {str(e_motor)}",
                    }
                ),
                500,
            )


# ============================================================================
# ROTINA DE BACKGROUND (REANÁLISE DE 1 HORA / 30 DIAS)
# ============================================================================
def loop_reanalise_background():
    """
    Rotina em segundo plano. Varre a fila de contratos e executa
    a atualização garantida caso o pagamento seja detetado.
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

                    if agora - data_inclusao > 2592000:
                        logger.warning(
                            "EXPIROU | Contrato %s atingiu o prazo máximo de 30 dias sem pagamento. Removido.",
                            contrato,
                        )
                        del pendentes[contrato]
                        mudou_pendentes = True
                        continue

                    if agora - ultima_verificacao < 3600:
                        continue

                    ESTADO["ultimo_keep_alive"] = agora
                    info["tentativas"] = info.get("tentativas", 0) + 1
                    info["ultima_verificacao"] = agora
                    mudou_pendentes = True

                    logger.info("REANÁLISE | Verificando %s...", contrato)

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

                            # Extração completa para capturar Grupo e Cota e entregar ao CDA
                            dados_completos = extrair_dados_completos(driver_global)
                            aba_salva = info.get("aba_original", obter_mes_utc4())

                            # Conecta as planilhas (o conectar_google_sheets cuida do log em caso de erro na infraestrutura)
                            sheet_v = conectar_google_sheets(
                                info["nome_planilha"], aba_salva
                            )
                            sheet_g_ano = conectar_google_sheets(
                                f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                            )
                            sheet_g_mes = conectar_google_sheets(
                                f"{PREFIXO_PLANILHA}GERAL", aba_salva
                            )

                            # [1] Busca e atualiza na Planilha do Vendedor
                            if sheet_v:
                                l_v = encontrar_linha_do_contrato(
                                    sheet_v, contrato, col_idx=13
                                )
                                if l_v:
                                    try:
                                        sheet_v.update_cell(l_v, 2, "1º Parcela Paga")
                                    except Exception as e:  # pylint: disable=broad-exception-caught
                                        logger.error(
                                            "Falha ao gravar na planilha do Vendedor: %s",
                                            e,
                                        )
                                else:
                                    logger.warning(
                                        "Contrato %s não encontrado na aba '%s' do Vendedor.",
                                        contrato,
                                        aba_salva,
                                    )

                            # [2] Busca e atualiza na Planilha GERAL (Aba do Ano)
                            if sheet_g_ano:
                                l_g_ano = encontrar_linha_do_contrato(
                                    sheet_g_ano, contrato, col_idx=12
                                )
                                if l_g_ano:
                                    try:
                                        sheet_g_ano.update_cell(
                                            l_g_ano, 1, "1º Parcela Paga"
                                        )
                                    except Exception as e:  # pylint: disable=broad-exception-caught
                                        logger.error(
                                            "Falha ao gravar na planilha GERAL (Ano): %s",
                                            e,
                                        )
                                else:
                                    logger.warning(
                                        "Contrato %s não encontrado na aba '%s' da GERAL.",
                                        contrato,
                                        NOME_ABA_GERAL,
                                    )

                            # [3] Busca e atualiza na Planilha GERAL (Aba do Mês)
                            if sheet_g_mes:
                                l_g_mes = encontrar_linha_do_contrato(
                                    sheet_g_mes, contrato, col_idx=12
                                )
                                if l_g_mes:
                                    try:
                                        sheet_g_mes.update_cell(
                                            l_g_mes, 1, "1º Parcela Paga"
                                        )
                                    except Exception as e:  # pylint: disable=broad-exception-caught
                                        logger.error(
                                            "Falha ao gravar na planilha GERAL (Mês): %s",
                                            e,
                                        )
                                else:
                                    logger.warning(
                                        "Contrato %s não encontrado na aba '%s' da GERAL.",
                                        contrato,
                                        aba_salva,
                                    )

                            # [4] Limpeza Forçada da Fila Curta
                            salvar_historico_concluido(
                                contrato,
                                info["nome_planilha"],
                                info["vendedor_nome"],
                                info["vendedor_tel"],
                                "1º Parcela Paga (Reanálise)",
                            )

                            # [PONTE CDA] Transfere o cliente limpo para a base longa do CRM
                            migrar_para_adimplencia(
                                contrato,
                                info,
                                str(dados_completos.get("grupo", "")),
                                str(dados_completos.get("cota", "")),
                            )

                            del pendentes[contrato]
                            logger.info(
                                "[SUCESSO REANÁLISE] Contrato %s concluído e transferido ao CDA.",
                                contrato,
                            )

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
                "Não foi possível acessar o portal de forma automatizada: %s", e_inicial
            )

    thread_background = threading.Thread(target=loop_reanalise_background, daemon=True)
    thread_background.start()

    logger.info(
        "Servidor WSGI de produção (Waitress) iniciado com sucesso na porta 5000."
    )
    serve(app, host="0.0.0.0", port=5000)
