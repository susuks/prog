"""
Motor Principal de Automação de Coleta e Reanálise de Consórcio V2.

Este script gerencia o ciclo de vida dos contratos: desde a captura inicial
no WhatsApp (via arquivo de fila) até o monitoramento contínuo de pagamentos.
Ele orquestra a navegação no portal da Tradição e o registro sincronizado
em múltiplas planilhas do Google Sheets (Individual do Vendedor e Geral).
"""

import os
import time
import shutil
import json
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

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

    if not os.path.exists(arquivo_cookie):
        return False

    try:
        # Acessa a raiz para o Chrome aceitar carregar cookies daquele domínio
        driver.get("https://intranet.consorciotradicao.com.br/autocred/")
        time.sleep(2)

        # Injeta as chaves clonadas
        with open(arquivo_cookie, "r", encoding="utf-8") as f:
            cookies = json.load(f)
            for cookie in cookies:
                driver.add_cookie(cookie)

        # Atualiza a página (se o crachá for válido, ele entra no sistema)
        driver.refresh()
        time.sleep(4)

        # Verifica se estamos na tela de login (procurando o campo de usuário)
        try:
            driver.switch_to.default_content()
            driver.find_element(By.ID, "j_username")
            return False
        except Exception: # pylint: disable=broad-exception-caught
            return True

    except Exception as e: # pylint: disable=broad-exception-caught
        print(f"[ERRO] Falha na manipulação do arquivo de cookies: {e}")
        return False


def loop_servico():
    """
    Loop de execução infinita para processamento de filas e reanálise.
    O loop é dividido em três fases principais:
    1. Processamento de Novos Contratos.
    2. Reanálise Contínua.
    3. Manutenção (Keep-Alive).
    """
    print("\n>>> INICIANDO SISTEMA CENTRALIZADO V2 (Bypass por Cookies) <<<")

    # --- CONFIGURAÇÃO DO MODO FANTASMA (HEADLESS) ---
    chrome_options = Options()
    chrome_options.add_argument("--headless=new") # Roda invisível
    chrome_options.add_argument("--no-sandbox") # Essencial para Linux
    chrome_options.add_argument("--disable-dev-shm-usage") # Evita travamento por falta de memória RAM
    chrome_options.add_argument("--window-size=1920,1080") # Engana o site fingindo ter uma tela

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )
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
                    print("[FALHA] Crachá (Cookies) ausente ou expirado.")
                    print("--> AÇÃO NECESSÁRIA: Envie um novo 'cookies.json' para o servidor.")
                    time.sleep(60)
                    continue

            # -------------------------------------------------------------
            # FASE 1: REGISTRO DE NOVOS CONTRATOS (PRIORIDADE)
            # -------------------------------------------------------------
            if os.path.exists(ARQUIVO_FILA):
                try:
                    shutil.move(ARQUIVO_FILA, ARQUIVO_EM_PROCESSAMENTO)
                except OSError:
                    # Aguarda caso o arquivo esteja travado pelo Node.js
                    time.sleep(1)
                    continue

                ultimo_keep_alive = time.time()
                try:
                    df = pd.read_csv(ARQUIVO_EM_PROCESSAMENTO, sep=',', dtype=str)
                    df.columns = [c.strip() for c in df.columns]
                except Exception:  # pylint: disable=broad-exception-caught
                    # Se o CSV estiver vazio ou corrompido, limpa o arquivo temporário
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

                    # Conexão independente com as duas planilhas alvo
                    nome_planilha_vendedor = f"{PREFIXO_PLANILHA}{vendedor}"
                    nome_planilha_geral = f"{PREFIXO_PLANILHA}GERAL"

                    sheet_vend = conectar_google_sheets(nome_planilha_vendedor, NOME_ABA)
                    sheet_geral = conectar_google_sheets(nome_planilha_geral, NOME_ABA_GERAL)

                    if buscar_contrato(driver, contrato):
                        dados = extrair_dados_completos(driver)

                        anotou_vend = False
                        anotou_geral = False

                        # Escrita protegida na planilha do Vendedor
                        if sheet_vend:
                            try:
                                atualizar_planilha_vendedor(sheet_vend, row.to_dict(), dados, contrato)
                                anotou_vend = True
                            except Exception as e:  # pylint: disable=broad-exception-caught
                                print(f"   [ERRO API] Falha ao escrever na planilha do vendedor: {e}")
                        else:
                            print(f"   [AVISO] Planilha '{nome_planilha_vendedor}' não encontrada ou inacessível.")

                        # Escrita protegida na planilha Geral
                        if sheet_geral:
                            try:
                                atualizar_planilha_geral(sheet_geral, row.to_dict(), dados, contrato)
                                anotou_geral = True
                            except Exception as e:  # pylint: disable=broad-exception-caught
                                print(f"   [ERRO API] Falha ao escrever na planilha GERAL: {e}")
                        else:
                            print(f"   [AVISO] Planilha '{nome_planilha_geral}' não encontrada ou inacessível.")

                        # Validação de Sucesso Absoluto
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
                            print("   [CRÍTICO] Nenhuma planilha foi atualizada. O contrato NÃO será salvo no histórico.")
                            print("   [RECUPERAÇÃO] Devolvendo contrato para a fila para nova tentativa no próximo ciclo...")

                            # Recria a fila se ela não existir e devolve o contrato intacto
                            if not os.path.exists(ARQUIVO_FILA):
                                with open(ARQUIVO_FILA, 'w', encoding='utf-8') as f_fila:
                                    f_fila.write("contrato,origem,vendedor,lance livre,telefone\n")

                            with open(ARQUIVO_FILA, 'a', encoding='utf-8') as f_fila:
                                linha_csv = f"{row.get('contrato', '')},{row.get('origem', '')},{row.get('vendedor', '')},{row.get('lance livre', '')},{row.get('telefone', '')}\n"
                                f_fila.write(linha_csv)
                    else:
                        print(f"   [ERRO] Contrato {contrato} não localizado no portal.")

                # Limpeza do lote processado
                if os.path.exists(ARQUIVO_EM_PROCESSAMENTO):
                    os.remove(ARQUIVO_EM_PROCESSAMENTO)

            # -------------------------------------------------------------
            # FASE 2: REANÁLISE DE PAGAMENTOS (LOOP CONTÍNUO)
            # -------------------------------------------------------------
            pendentes = carregar_pendentes()
            mudou_pendentes = False

            # Converte para lista de tuplas para evitar erro de alteração de dicionário durante iteração
            for contrato, info in list(pendentes.items()):
                # Interrupção imediata se chegar lote novo
                if os.path.exists(ARQUIVO_FILA):
                    break

                ultimo_keep_alive = time.time()
                info['tentativas'] = info.get('tentativas', 0) + 1
                mudou_pendentes = True

                print(f"\n[REANÁLISE] Verificando {contrato} (Tentativa {info['tentativas']}/{MAX_TENTATIVAS})...")

                if buscar_contrato(driver, contrato):
                    if verificar_apenas_pagamento(driver):
                        print("   [PAGAMENTO DETECTADO] Atualizando status nas planilhas...")

                        anotou_vend = False
                        anotou_geral = False

                        # Atualiza Planilha Vendedor (Coluna M / 13)
                        sheet_v = conectar_google_sheets(info['nome_planilha'], NOME_ABA)
                        if sheet_v:
                            linha_v = encontrar_linha_do_contrato(sheet_v, contrato, col_idx=13)
                            if linha_v:
                                try:
                                    sheet_v.update_cell(linha_v, 2, "1º Parcela Paga")
                                    anotou_vend = True
                                except Exception as e:  # pylint: disable=broad-exception-caught
                                    print(f"   [ERRO API] Falha Vendedor: {e}")
                            else:
                                print("   [AVISO] Contrato não achado na planilha individual.")

                        # Atualiza Planilha Geral (Coluna L / 12)
                        sheet_g = conectar_google_sheets(f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL)
                        if sheet_g:
                            linha_g = encontrar_linha_do_contrato(sheet_g, contrato, col_idx=12)
                            if linha_g:
                                try:
                                    sheet_g.update_cell(linha_g, 1, "1º Parcela Paga")
                                    anotou_geral = True
                                except Exception as e:  # pylint: disable=broad-exception-caught
                                    print(f"   [ERRO API] Falha GERAL: {e}")
                            else:
                                print("   [AVISO] Contrato não achado na planilha GERAL.")

                        # Validação de Sucesso Absoluto na Reanálise
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
                            # Remove da fila de pendentes apenas se teve sucesso
                            del pendentes[contrato]
                        else:
                            print(" [ERRO] Falha ao atualizar planilhas. Mantendo na fila de reanálise para tentar depois.")
                            # O contrato NÃO é deletado, então ele tentará novamente na próxima rodada
                    else:
                        # Se não pagou, verifica se excedeu o limite de tentativas
                        if info['tentativas'] >= MAX_TENTATIVAS:
                            print(f" [EXPIROU] Contrato {contrato} atingiu o limite de tentativas.")
                            del pendentes[contrato]
                else:
                    # Se não achou o contrato (cota excluída/cancelada), limpa da fila
                    print("   [NÃO ENCONTRADO] Cota indisponível no portal. Removendo da fila.")
                    del pendentes[contrato]

            if mudou_pendentes:
                salvar_pendentes(pendentes)

            # -------------------------------------------------------------
            # FASE 3: MANUTENÇÃO DE SESSÃO (KEEP-ALIVE)
            # -------------------------------------------------------------
            if (time.time() - ultimo_keep_alive) > TEMPO_INATIVIDADE_MAXIMO:
                if not manter_sessao_viva(driver):
                    print("[AVISO] Sessão expirada ou perdida. Forçando Relogin...")
                    autenticado = False # Derruba a sessão para a Portaria barrar no próximo loop
                ultimo_keep_alive = time.time()

            time.sleep(1)

        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"[ERRO GERAL NO LOOP] A execução foi protegida. Detalhe: {e}")
            time.sleep(2)


if __name__ == "__main__":
    loop_servico()
