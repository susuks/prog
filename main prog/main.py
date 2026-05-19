"""
Motor Principal de Automação de Consórcio V5 (API REST + Selenium).

Este módulo substitui o antigo loop de leitura de ficheiros por um servidor
web (Flask). Fica à escuta de pedidos HTTP na porta 5000 enviados pelo Node.js,
orquestra a execução no Selenium com controlo de concorrência (Locks) e processa
a reanálise de contratos pendentes numa thread de segundo plano.
"""

import time
import threading
from flask import Flask, request, jsonify

# Importações dos módulos previamente refatorados
from gerador_dados import (
    MAX_TENTATIVAS,
    TEMPO_INATIVIDADE_MAXIMO,
    PREFIXO_PLANILHA,
    NOME_ABA,
    NOME_ABA_GERAL,
    conectar_google_sheets,
    atualizar_planilha_vendedor,
    atualizar_planilha_geral,
    adicionar_para_reanalise,
    carregar_pendentes,
    encontrar_linha_do_contrato,
    salvar_pendentes,
    salvar_historico_concluido
)

from motor_navegacao import (
    iniciar_navegador,
    buscar_contrato,
    extrair_dados_completos,
    verificar_apenas_pagamento,
    manter_sessao_viva,
    fazer_login_com_ia
)

# ============================================================================
# INICIALIZAÇÃO DA API E ESTADO GLOBAL
# ============================================================================
app = Flask(__name__)
driver_global = None

# O Lock previne que a API e a Reanálise de fundo mexam no rato ao mesmo tempo
navegador_lock = threading.Lock()

ESTADO = {
    "autenticado": False,
    "ultimo_keep_alive": time.time()
}


def garantir_sessao():
    """
    Verifica se a sessão do portal está ativa. Caso contrário, aciona
    o módulo de Inteligência Artificial para efetuar um novo login.
    """
    if not ESTADO["autenticado"]:
        if fazer_login_com_ia(driver_global):
            print("[LIBERADO] Acesso restabelecido via Inteligência Artificial.")
            ESTADO["autenticado"] = True
            ESTADO["ultimo_keep_alive"] = time.time()
        else:
            print("[BARRADO] Falha crítica de login.")
            raise ConnectionError("Falha de autenticação no portal Autocred.")


# ============================================================================
# ROTA DA API (RECEPÇÃO DE PEDIDOS DO NODE.JS)
# ============================================================================
@app.route('/processar_venda', methods=['POST'])
def processar_venda():
    """
    Endpoint HTTP do tipo POST. Recebe o payload JSON do WhatsApp e
    devolve uma resposta em tempo real sobre o sucesso da gravação.
    """
    dados = request.json
    if not dados:
        return jsonify({"erro": "O payload da requisição está vazio."}), 400

    contrato = str(dados.get("contrato")).strip()
    vendedor = str(dados.get("vendedor")).strip()
    telefone_vendedor = str(dados.get("telefone")).strip()
    origem = str(dados.get("origem", "")).strip()

    print(f"\n[API RECEBIDA] Nova Venda | Contrato: {contrato} | Vendedor: {vendedor}")

    # Adquire o bastão de controlo do navegador
    with navegador_lock:
        try:
            garantir_sessao()
            ESTADO["ultimo_keep_alive"] = time.time()

            nome_planilha_vendedor = f"{PREFIXO_PLANILHA}{vendedor}"
            nome_planilha_geral = f"{PREFIXO_PLANILHA}GERAL"

            sheet_vend = conectar_google_sheets(nome_planilha_vendedor, NOME_ABA)
            sheet_geral = conectar_google_sheets(nome_planilha_geral, NOME_ABA_GERAL)

            if buscar_contrato(driver_global, contrato):
                dados_site = extrair_dados_completos(driver_global)
                anotou_vend = False
                anotou_geral = False

                if sheet_vend:
                    try:
                        atualizar_planilha_vendedor(sheet_vend, dados, dados_site, contrato)
                        anotou_vend = True
                    except Exception as e:  # pylint: disable=broad-exception-caught
                        print(f"   [ERRO API] Falha na folha do vendedor: {e}")

                if sheet_geral:
                    try:
                        atualizar_planilha_geral(sheet_geral, dados, dados_site, contrato)
                        anotou_geral = True
                    except Exception as e:  # pylint: disable=broad-exception-caught
                        print(f"   [ERRO API] Falha na folha GERAL: {e}")

                if anotou_vend or anotou_geral:
                    if anotou_vend and anotou_geral:
                        destino_log = f"{nome_planilha_vendedor} + GERAL"
                    else:
                        destino_log = nome_planilha_vendedor if anotou_vend else f"{PREFIXO_PLANILHA}GERAL"

                    texto_st = "1º Parcela Paga" if dados_site["pago"] else "1º Parcela Não Paga"
                    salvar_historico_concluido(
                        contrato, destino_log, vendedor, telefone_vendedor, texto_st
                    )

                    if not dados_site["pago"]:
                        adicionar_para_reanalise(
                            contrato, vendedor, telefone_vendedor,
                            nome_planilha_vendedor, origem, dados
                        )

                    return jsonify({
                        "sucesso": True,
                        "status_pagamento": texto_st,
                        "planilha": destino_log
                    }), 200

                return jsonify({"erro": "Falha na escrita da API do Google."}), 500
            
            return jsonify({"erro": "Contrato não localizado no portal."}), 404

        except Exception as e:  # pylint: disable=broad-exception-caught
            return jsonify({"erro": f"Erro interno do motor: {str(e)}"}), 500


# ============================================================================
# ROTINA DE BACKGROUND (REANÁLISE INDEPENDENTE)
# ============================================================================
def loop_reanalise_background():
    """
    Operação executada numa thread separada. Verifica as pendências a cada
    minuto e assegura que a sessão no portal se mantém ativa, utilizando
    o lock para respeitar as chamadas da API.
    """
    print("[BACKGROUND] Motor de Reanálise Independente Iniciado.")

    while True:
        time.sleep(60)
        
        # Adquire o bastão de controlo do navegador
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
                    ESTADO["ultimo_keep_alive"] = time.time()
                    info["tentativas"] = info.get("tentativas", 0) + 1
                    mudou_pendentes = True

                    progresso = f"(Tentativa {info['tentativas']}/{MAX_TENTATIVAS})"
                    print(f"\n[REANÁLISE] A verificar {contrato} {progresso}...")

                    if buscar_contrato(driver_global, contrato):
                        if verificar_apenas_pagamento(driver_global):
                            print("   [PAGAMENTO DETETADO] A atualizar o status nas planilhas...")
                            anotou_v, anotou_g = False, False

                            sheet_v = conectar_google_sheets(info["nome_planilha"], NOME_ABA)
                            if sheet_v:
                                l_v = encontrar_linha_do_contrato(sheet_v, contrato, col_idx=13)
                                if l_v:
                                    try:
                                        sheet_v.update_cell(l_v, 2, "1º Parcela Paga")
                                        anotou_v = True
                                    except Exception:  # pylint: disable=broad-exception-caught
                                        pass

                            sheet_g = conectar_google_sheets(f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL)
                            if sheet_g:
                                l_g = encontrar_linha_do_contrato(sheet_g, contrato, col_idx=12)
                                if l_g:
                                    try:
                                        sheet_g.update_cell(l_g, 1, "1º Parcela Paga")
                                        anotou_g = True
                                    except Exception:  # pylint: disable=broad-exception-caught
                                        pass

                            if anotou_v or anotou_g:
                                if anotou_v and anotou_g:
                                    dest = f"{info['nome_planilha']} + GERAL"
                                else:
                                    dest = info['nome_planilha'] if anotou_v else f"{PREFIXO_PLANILHA}GERAL"
                                
                                salvar_historico_concluido(
                                    contrato, dest, info["vendedor_nome"],
                                    info["vendedor_tel"], "1º Parcela Paga (Reanálise)"
                                )
                                del pendentes[contrato]
                        else:
                            if info["tentativas"] >= MAX_TENTATIVAS:
                                print(f" [EXPIROU] Contrato {contrato} ultrapassou as tentativas limite.")
                                del pendentes[contrato]
                    else:
                        print("   [NÃO ENCONTRADO] Cota indisponível. Removida da fila.")
                        del pendentes[contrato]

                if mudou_pendentes:
                    salvar_pendentes(pendentes)

                # Avaliação de Keep-Alive
                if ESTADO["autenticado"]:
                    tempo_inativo = time.time() - ESTADO["ultimo_keep_alive"]
                    if tempo_inativo > TEMPO_INATIVIDADE_MAXIMO:
                        if not manter_sessao_viva(driver_global):
                            print("[AVISO] Keep-Alive falhou. Necessário Relogin futuro.")
                            ESTADO["autenticado"] = False
                        ESTADO["ultimo_keep_alive"] = time.time()

            except Exception as e:  # pylint: disable=broad-exception-caught
                print(f"[ERRO NO BACKGROUND] O loop foi protegido: {str(e)}")


# ============================================================================
# PONTO DE ENTRADA DA APLICAÇÃO
# ============================================================================
if __name__ == '__main__':
    print("\n>>> INICIANDO SISTEMA ENTERPRISE V5 (API FLASK + SELENIUM) <<<")
    driver_global = iniciar_navegador()

    # Inicia a thread secundária para o processamento contínuo
    thread_background = threading.Thread(target=loop_reanalise_background, daemon=True)
    thread_background.start()

    # Inicia o servidor Web principal na porta 5000 (Acessível localmente pelo Node.js)
    # A diretiva use_reloader=False previne a duplicação do WebDriver em ambiente de testes.
    app.run(host='0.0.0.0', port=5000, use_reloader=False)