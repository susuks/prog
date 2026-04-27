"""
Motor Principal de Automação de Coleta e Reanálise de Consórcio V2.
(Versão Stealth com Undetected Chromedriver)
"""

import os
import time
import shutil
import json
import pandas as pd
from selenium.webdriver.common.by import By
import undetected_chromedriver as uc

# Importação explícita do módulo de utilitários local
from coletor_utils import (
    ARQUIVO_FILA, ARQUIVO_EM_PROCESSAMENTO, PREFIXO_PLANILHA, NOME_ABA,
    NOME_ABA_GERAL, MAX_TENTATIVAS, TEMPO_INATIVIDADE_MAXIMO,
    conectar_google_sheets, buscar_contrato, extrair_dados_completos,
    atualizar_planilha_vendedor, atualizar_planilha_geral,
    adicionar_para_reanalise, carregar_pendentes, verificar_apenas_pagamento,
    encontrar_linha_do_contrato, salvar_pendentes, manter_sessao_viva,
    salvar_historico_concluido
)


def injetar_cookies(driver):
    """Lê o arquivo cookies.json, injeta no navegador e valida o acesso."""
    arquivo_cookie = "cookies.json"

    print(f"   -> Procurando '{arquivo_cookie}' no diretório: {os.getcwd()}")

    if not os.path.exists(arquivo_cookie):
        print("   -> [ERRO] O arquivo cookies.json NÃO ESTÁ AQUI!")
        print("   -> Dica: Envie o arquivo gerado pelo PC via SCP.")
        return False

    try:
        print("   -> Arquivo encontrado! Abrindo a porta do site...")
        driver.get("https://intranet.consorciotradicao.com.br/autocred/")
        time.sleep(2)

        with open(arquivo_cookie, "r", encoding="utf-8") as f:
            cookies = json.load(f)
            print(f"   -> Lidos {len(cookies)} cookies. Colocando o crachá no robô...")
            for cookie in cookies:
                if 'sameSite' in cookie:
                    del cookie['sameSite']
                driver.add_cookie(cookie)

        print("   -> Crachá colocado! Atualizando a página (Modo Stealth)...")
        driver.refresh()
        time.sleep(4)

        try:
            driver.switch_to.default_content()
            driver.find_element(By.ID, "j_username")
            print("   -> [FALHA] O site recusou o crachá mesmo no modo stealth.")
            return False
        except Exception:  # pylint: disable=broad-exception-caught
            print("   -> [SUCESSO] O campo de login sumiu. Estamos dentro!")
            return True

    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"   -> [ERRO GRAVE] Falha ao processar os cookies: {e}")
        return False


def loop_servico():
    """
    Loop de execução infinita para processamento de filas e reanálise.
    """
    print("\n>>> INICIANDO SISTEMA CENTRALIZADO V2 (Modo Stealth UC) <<<")

    # --- CONFIGURAÇÃO DO MODO STEALTH (UNDETECTED) ---
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    driver = uc.Chrome(options=options, version_main=147, headless=True)
    # ------------------------------------------------

    autenticado = False
    ultimo_keep_alive = time.time()

    while True:
        try:
            # -------------------------------------------------------------
            # FASE 0: PORTARIA E VALIDAÇÃO DE SESSÃO
            # -------------------------------------------------------------
            if not autenticado:
                print("\n[PORTARIA] Sistema bloqueado. Tentando ler 'cookies.json'...")
                if injetar_cookies(driver):
                    print("[SUCESSO] Crachá aceito! Acesso liberado ao portal.")
                    autenticado = True
                    ultimo_keep_alive = time.time()
                else:
                    print("[FALHA] Crachá (Cookies) ausente ou rejeitado.")
                    print("--> AÇÃO: Envie um novo 'cookies.json' via gerador local.")
                    time.sleep(60)
                    continue

            # -------------------------------------------------------------
            # FASE 1: REGISTRO DE NOVOS CONTRATOS
            # -------------------------------------------------------------
            if os.path.exists(ARQUIVO_FILA):
                try:
                    shutil.move(ARQUIVO_FILA, ARQUIVO_EM_PROCESSAMENTO)
                except OSError:
                    time.sleep(1)
                    continue

                ultimo_keep_alive = time.time()
                try:
                    df = pd.read_csv(ARQUIVO_EM_PROCESSAMENTO, sep=',', dtype=str)
                    df.columns = [c.strip() for c in df.columns]
                except Exception:  # pylint: disable=broad-exception-caught
                    if os.path.exists(ARQUIVO_EM_PROCESSAMENTO):
                        os.remove(ARQUIVO_EM_PROCESSAMENTO)
                    continue

                for _, row in df.iterrows():
                    contrato = str(row.get('contrato')).strip()
                    vendedor = str(row.get('vendedor')).strip()
                    telefone_vendedor = str(row.get('telefone')).strip()
                    origem = str(row.get('origem', '')).strip()

                    if pd.isna(contrato) or not contrato or contrato == 'nan':
                        continue

                    print(f"\n[NOVO] Processando Contrato: {contrato} (Vendedor: {vendedor})...")

                    nome_planilha_vendedor = f"{PREFIXO_PLANILHA}{vendedor}"
                    nome_planilha_geral = f"{PREFIXO_PLANILHA}GERAL"

                    sheet_vend = conectar_google_sheets(nome_planilha_vendedor, NOME_ABA)
                    sheet_geral = conectar_google_sheets(nome_planilha_geral, NOME_ABA_GERAL)

                    if buscar_contrato(driver, contrato):
                        dados = extrair_dados_completos(driver)
                        anotou_vend = False
                        anotou_geral = False

                        if sheet_vend:
                            try:
                                atualizar_planilha_vendedor(sheet_vend, row.to_dict(), dados, contrato)
                                anotou_vend = True
                            except Exception as e:  # pylint: disable=broad-exception-caught
                                print(f"   [ERRO API] Falha planilha vendedor: {e}")

                        if sheet_geral:
                            try:
                                atualizar_planilha_geral(sheet_geral, row.to_dict(), dados, contrato)
                                anotou_geral = True
                            except Exception as e:  # pylint: disable=broad-exception-caught
                                print(f"   [ERRO API] Falha planilha GERAL: {e}")

                        if anotou_vend or anotou_geral:
                            destino_log = ""
                            if anotou_vend and anotou_geral:
                                destino_log = f"{nome_planilha_vendedor} + GERAL"
                            elif anotou_vend:
                                destino_log = nome_planilha_vendedor
                            elif anotou_geral:
                                destino_log = f"{PREFIXO_PLANILHA}GERAL"

                            print(f"   [OK] Registrado com sucesso em: {destino_log}")

                            texto_status = "1º Parcela Paga" if dados['pago'] else "1º Parcela Não Paga"
                            salvar_historico_concluido(
                                contrato, destino_log,
                                vendedor, telefone_vendedor, texto_status
                            )

                            if not dados['pago']:
                                adicionar_para_reanalise(
                                    contrato, vendedor, telefone_vendedor,
                                    nome_planilha_vendedor, origem, row.to_dict()
                                )
                        else:
                            print("   [CRÍTICO] Falha ao gravar. Devolvendo à fila...")
                            if not os.path.exists(ARQUIVO_FILA):
                                with open(ARQUIVO_FILA, 'w', encoding='utf-8') as f_fila:
                                    f_fila.write("contrato,origem,vendedor,lance livre,telefone\n")

                            with open(ARQUIVO_FILA, 'a', encoding='utf-8') as f_fila:
                                f_fila.write(f"{row.get('contrato', '')},{row.get('origem', '')},{row.get('vendedor', '')},{row.get('lance livre', '')},{row.get('telefone', '')}\n")
                    else:
                        print(f"   [ERRO] Contrato {contrato} não localizado no portal.")

                if os.path.exists(ARQUIVO_EM_PROCESSAMENTO):
                    os.remove(ARQUIVO_EM_PROCESSAMENTO)

            # -------------------------------------------------------------
            # FASE 2: REANÁLISE DE PAGAMENTOS
            # -------------------------------------------------------------
            pendentes = carregar_pendentes()
            mudou_pendentes = False

            for contrato, info in list(pendentes.items()):
                if os.path.exists(ARQUIVO_FILA):
                    break

                ultimo_keep_alive = time.time()
                info['tentativas'] = info.get('tentativas', 0) + 1
                mudou_pendentes = True

                print(f"\n[REANÁLISE] Verificando {contrato} (Tentativa {info['tentativas']}/{MAX_TENTATIVAS})...")

                if buscar_contrato(driver, contrato):
                    if verificar_apenas_pagamento(driver):
                        print("   [PAGAMENTO DETECTADO] Atualizando planilhas...")
                        anotou_vend = False
                        anotou_geral = False

                        sheet_v = conectar_google_sheets(info['nome_planilha'], NOME_ABA)
                        if sheet_v:
                            linha_v = encontrar_linha_do_contrato(sheet_v, contrato, col_idx=13)
                            if linha_v:
                                try:
                                    sheet_v.update_cell(linha_v, 2, "1º Parcela Paga")
                                    anotou_vend = True
                                except Exception:  # pylint: disable=broad-exception-caught
                                    pass

                        sheet_g = conectar_google_sheets(f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL)
                        if sheet_g:
                            linha_g = encontrar_linha_do_contrato(sheet_g, contrato, col_idx=12)
                            if linha_g:
                                try:
                                    sheet_g.update_cell(linha_g, 1, "1º Parcela Paga")
                                    anotou_geral = True
                                except Exception:  # pylint: disable=broad-exception-caught
                                    pass

                        if anotou_vend or anotou_geral:
                            destino_log = ""
                            if anotou_vend and anotou_geral:
                                destino_log = f"{info['nome_planilha']} + GERAL"
                            elif anotou_vend:
                                destino_log = info['nome_planilha']
                            elif anotou_geral:
                                destino_log = f"{PREFIXO_PLANILHA}GERAL"

                            salvar_historico_concluido(
                                contrato, destino_log,
                                info['vendedor_nome'], info['vendedor_tel'], "1º Parcela Paga (Reanálise)"
                            )
                            del pendentes[contrato]
                    else:
                        if info['tentativas'] >= MAX_TENTATIVAS:
                            print(f" [EXPIROU] Contrato {contrato} atingiu o limite.")
                            del pendentes[contrato]
                else:
                    print("   [NÃO ENCONTRADO] Cota indisponível no portal.")
                    del pendentes[contrato]

            if mudou_pendentes:
                salvar_pendentes(pendentes)

            # -------------------------------------------------------------
            # FASE 3: MANUTENÇÃO DE SESSÃO
            # -------------------------------------------------------------
            if (time.time() - ultimo_keep_alive) > TEMPO_INATIVIDADE_MAXIMO:
                if not manter_sessao_viva(driver):
                    print("[AVISO] Sessão expirada. Voltando para Portaria...")
                    autenticado = False
                ultimo_keep_alive = time.time()

            time.sleep(1)

        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"[ERRO GERAL NO LOOP] Execução protegida. Detalhe: {e}")
            time.sleep(2)


if __name__ == "__main__":
    loop_servico()
