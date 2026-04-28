"""
Motor Principal de Automação de Coleta e Reanálise de Consórcio V4.
(Versão API Direta CapSolver - Sem Extensões, 100% via Backend)
"""

import os
import time
import shutil
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# Importação explícita do módulo de utilitários local
from gerador_dados import (
    ARQUIVO_FILA,
    ARQUIVO_EM_PROCESSAMENTO,
    PREFIXO_PLANILHA,
    NOME_ABA,
    NOME_ABA_GERAL,
    MAX_TENTATIVAS,
    TEMPO_INATIVIDADE_MAXIMO,
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
    buscar_contrato,
    extrair_dados_completos,
    verificar_apenas_pagamento,
    manter_sessao_viva,
    carregar_chave_capsolver,
    fazer_login_com_ia
)

def loop_servico():
    """Loop de execução contínua com API CapSolver Integrada."""
    print("\n>>> INICIANDO SISTEMA AUTÓNOMO V4 (Selenium + API CapSolver) <<<")

    chave_api = carregar_chave_capsolver()
    if not chave_api:
        print(
            "[CRÍTICO] A linha 'CAPSOLVER_KEY=' não foi encontrada no ficheiro 'config.txt'."
        )
        return

    # --- CONFIGURAÇÃO DO MODO FANTASMA (SELENIUM TRADICIONAL) ---
    chrome_options = Options()
    # No servidor poderá usar "--headless=new", mas localmente deixe visível para ver acontecer
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")

    mascara = "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    chrome_options.add_argument(mascara)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=chrome_options
    )
    # -----------------------------------------------------------

    autenticado = False
    ultimo_keep_alive = time.time()

    while True:
        try:
            # =============================================================
            # PORTARIA: LOGIN AUTÓNOMO COM API
            # =============================================================
            if not autenticado:
                if fazer_login_com_ia(driver):
                    print("[LIBERADO] Estamos dentro! O Robô assumiu o controlo.")
                    autenticado = True
                    ultimo_keep_alive = time.time()
                else:
                    print(
                        "[BARRADO] O login falhou. A tentar novamente em 60 segundos..."
                    )
                    time.sleep(60)
                    continue

            # =============================================================
            # FASE 1: REGISTRO DE NOVOS CONTRATOS
            # =============================================================
            if os.path.exists(ARQUIVO_FILA):
                try:
                    shutil.move(ARQUIVO_FILA, ARQUIVO_EM_PROCESSAMENTO)
                except OSError:
                    time.sleep(1)
                    continue

                ultimo_keep_alive = time.time()
                try:
                    df = pd.read_csv(ARQUIVO_EM_PROCESSAMENTO, sep=",", dtype=str)
                    df.columns = [c.strip() for c in df.columns]
                except Exception:  # pylint: disable=broad-exception-caught
                    if os.path.exists(ARQUIVO_EM_PROCESSAMENTO):
                        os.remove(ARQUIVO_EM_PROCESSAMENTO)
                    continue

                for _, row in df.iterrows():
                    contrato = str(row.get("contrato")).strip()
                    vendedor = str(row.get("vendedor")).strip()
                    telefone_vendedor = str(row.get("telefone")).strip()
                    origem = str(row.get("origem", "")).strip()

                    if pd.isna(contrato) or not contrato or contrato == "nan":
                        continue

                    print(
                        f"\n[NOVO] A processar Contrato: {contrato} (Vendedor: {vendedor})..."
                    )

                    nome_planilha_vendedor = f"{PREFIXO_PLANILHA}{vendedor}"
                    nome_planilha_geral = f"{PREFIXO_PLANILHA}GERAL"

                    sheet_vend = conectar_google_sheets(
                        nome_planilha_vendedor, NOME_ABA
                    )
                    sheet_geral = conectar_google_sheets(
                        nome_planilha_geral, NOME_ABA_GERAL
                    )

                    if buscar_contrato(driver, contrato):
                        dados = extrair_dados_completos(driver)
                        anotou_vend = False
                        anotou_geral = False

                        if sheet_vend:
                            try:
                                atualizar_planilha_vendedor(
                                    sheet_vend, row.to_dict(), dados, contrato
                                )
                                anotou_vend = True
                            except (
                                Exception # pylint: disable=broad-exception-caught
                            ) as e:
                                print(f"   [ERRO API] Falha na folha do vendedor: {e}")

                        if sheet_geral:
                            try:
                                atualizar_planilha_geral(
                                    sheet_geral, row.to_dict(), dados, contrato
                                )
                                anotou_geral = True
                            except (
                                Exception # pylint: disable=broad-exception-caught
                            ) as e:
                                print(f"   [ERRO API] Falha na folha GERAL: {e}")

                        if anotou_vend or anotou_geral:
                            destino_log = (
                                f"{nome_planilha_vendedor} + GERAL"
                                if (anotou_vend and anotou_geral)
                                else (
                                    nome_planilha_vendedor
                                    if anotou_vend
                                    else f"{PREFIXO_PLANILHA}GERAL"
                                )
                            )
                            print(f"   [OK] Registado com sucesso em: {destino_log}")

                            texto_status = (
                                "1º Parcela Paga"
                                if dados["pago"]
                                else "1º Parcela Não Paga"
                            )
                            salvar_historico_concluido(
                                contrato,
                                destino_log,
                                vendedor,
                                telefone_vendedor,
                                texto_status,
                            )

                            if not dados["pago"]:
                                adicionar_para_reanalise(
                                    contrato,
                                    vendedor,
                                    telefone_vendedor,
                                    nome_planilha_vendedor,
                                    origem,
                                    row.to_dict(),
                                )
                        else:
                            print(
                                "   [CRÍTICO] Falha ao atualizar. A devolver contrato para a fila..."
                            )
                            if not os.path.exists(ARQUIVO_FILA):
                                with open(
                                    ARQUIVO_FILA, "w", encoding="utf-8"
                                ) as f_fila:
                                    f_fila.write(
                                        "contrato,origem,vendedor,lance livre,telefone\n"
                                    )
                            with open(ARQUIVO_FILA, "a", encoding="utf-8") as f_fila:
                                f_fila.write(
                                    f"{row.get('contrato', '')},{row.get('origem', '')},{row.get('vendedor', '')},{row.get('lance livre', '')},{row.get('telefone', '')}\n"
                                )
                    else:
                        print(
                            f"   [ERRO] Contrato {contrato} não localizado no portal."
                        )

                if os.path.exists(ARQUIVO_EM_PROCESSAMENTO):
                    os.remove(ARQUIVO_EM_PROCESSAMENTO)

            # =============================================================
            # FASE 2: REANÁLISE DE PAGAMENTOS
            # =============================================================
            pendentes = carregar_pendentes()
            mudou_pendentes = False

            for contrato, info in list(pendentes.items()):
                if os.path.exists(ARQUIVO_FILA):
                    break

                ultimo_keep_alive = time.time()
                info["tentativas"] = info.get("tentativas", 0) + 1
                mudou_pendentes = True

                print(
                    f"\n[REANÁLISE] A verificar {contrato} (Tentativa {info['tentativas']}/{MAX_TENTATIVAS})..."
                )

                if buscar_contrato(driver, contrato):
                    if verificar_apenas_pagamento(driver):
                        print(
                            "   [PAGAMENTO DETETADO] A atualizar o status nas planilhas..."
                        )
                        anotou_vend, anotou_geral = False, False

                        sheet_v = conectar_google_sheets(
                            info["nome_planilha"], NOME_ABA
                        )
                        if sheet_v:
                            linha_v = encontrar_linha_do_contrato(
                                sheet_v, contrato, col_idx=13
                            )
                            if linha_v:
                                try:
                                    sheet_v.update_cell(linha_v, 2, "1º Parcela Paga")
                                    anotou_vend = True
                                except (
                                    Exception # pylint: disable=broad-exception-caught
                                ):
                                    pass

                        sheet_g = conectar_google_sheets(
                            f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL
                        )
                        if sheet_g:
                            linha_g = encontrar_linha_do_contrato(
                                sheet_g, contrato, col_idx=12
                            )
                            if linha_g:
                                try:
                                    sheet_g.update_cell(linha_g, 1, "1º Parcela Paga")
                                    anotou_geral = True
                                except (
                                    Exception # pylint: disable=broad-exception-caught
                                ):
                                    pass

                        if anotou_vend or anotou_geral:
                            destino_log = (
                                f"{info['nome_planilha']} + GERAL"
                                if (anotou_vend and anotou_geral)
                                else (
                                    info["nome_planilha"]
                                    if anotou_vend
                                    else f"{PREFIXO_PLANILHA}GERAL"
                                )
                            )
                            salvar_historico_concluido(
                                contrato,
                                destino_log,
                                info["vendedor_nome"],
                                info["vendedor_tel"],
                                "1º Parcela Paga (Reanálise)",
                            )
                            del pendentes[contrato]
                    else:
                        if info["tentativas"] >= MAX_TENTATIVAS:
                            print(
                                f" [EXPIROU] Contrato {contrato} atingiu o limite de tentativas."
                            )
                            del pendentes[contrato]
                else:
                    print("   [NÃO ENCONTRADO] Cota indisponível no portal.")
                    del pendentes[contrato]

            if mudou_pendentes:
                salvar_pendentes(pendentes)

            # =============================================================
            # FASE 3: MANUTENÇÃO DE SESSÃO
            # =============================================================
            if (time.time() - ultimo_keep_alive) > TEMPO_INATIVIDADE_MAXIMO:
                if not manter_sessao_viva(driver):
                    print(
                        "[AVISO] Sessão expirada ou perdida. A forçar Relogin da IA..."
                    )
                    autenticado = False
                ultimo_keep_alive = time.time()

            time.sleep(1)

        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"[ERRO GERAL NO LOOP] A execução foi protegida. Detalhe: {e}")
            time.sleep(2)


if __name__ == "__main__":
    loop_servico()
