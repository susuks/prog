"""
Motor Principal de Automação de Coleta e Reanálise de Consórcio V3.

Este script gerencia o ciclo de vida dos contratos: desde a captura inicial
no WhatsApp (via arquivo de fila) até o monitoramento contínuo de pagamentos.
Ele orquestra a navegação no portal da Tradição e o registro sincronizado
em múltiplas planilhas do Google Sheets (Individual do Vendedor e Geral).
"""

import os
import time
import shutil
import json
import urllib.request
import zipfile
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# Importação explícita do módulo de utilitários local
from coletor_utils import (
    ARQUIVO_FILA, ARQUIVO_EM_PROCESSAMENTO, PREFIXO_PLANILHA, NOME_ABA,
    NOME_ABA_GERAL, MAX_TENTATIVAS, TEMPO_INATIVIDADE_MAXIMO,
    conectar_google_sheets, buscar_contrato, extrair_dados_completos,
    atualizar_planilha_vendedor, atualizar_planilha_geral,
    adicionar_para_reanalise, carregar_pendentes, verificar_apenas_pagamento,
    encontrar_linha_do_contrato, salvar_pendentes, manter_sessao_viva,
    salvar_historico_concluido, USUARIO_LOGIN, SENHA_LOGIN
)

def carregar_chave_capsolver() -> str:
    """Extrai a chave de API do CapSolver do config.txt."""
    if os.path.exists("config.txt"):
        with open("config.txt", "r", encoding="utf-8") as f:
            for linha in f:
                if linha.startswith("CAPSOLVER_KEY="):
                    return linha.split("=", 1)[1].strip()
    return ""

def preparar_capsolver(api_key: str) -> str:
    """Baixa e configura a extensão oficial do CapSolver com a sua chave."""
    ext_dir = os.path.join(os.getcwd(), "capsolver_ext")
    zip_path = os.path.join(os.getcwd(), "capsolver.zip")

    if not os.path.exists(ext_dir):
        print("\n[SISTEMA] Instalando a Extensão de IA do CapSolver...")
        url = "https://github.com/capsolver/capsolver-browser-extension/releases/latest/download/capsolver-chrome-extension.zip"
        try:
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(ext_dir)
            os.remove(zip_path)
            print("[SISTEMA] Download concluído com sucesso!")
        except Exception as e: # pylint: disable=broad-exception-caught
            print(f"[ERRO FATAL] Falha ao baixar CapSolver: {e}")
            return ""

    # Injeta a chave de API dentro das configurações da extensão
    config_file = os.path.join(ext_dir, "assets", "config.json")
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)

        config["apiKey"] = api_key
        config["enabledForCloudflare"] = True
        config["enabledForRecaptchaV2"] = True

        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    return ext_dir


def fazer_login_com_ia(driver):
    """
    Loop de execução infinita para processamento de filas e reanálise.
    O loop é dividido em três fases principais:
    1. Processamento de Novos Contratos.
    2. Reanálise Contínua.
    3. Manutenção (Keep-Alive).
    """
    print("\n[PORTARIA] Acessando a página de login da Tradição...")
    driver.get("https://intranet.consorciotradicao.com.br/autocred/")

    try:
        # Tratamento do frame da Tradição (Evita erros de não achar o campo)
        try:
            WebDriverWait(driver, 5).until(
                EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
            )
            driver.switch_to.default_content()
        except Exception: pass # pylint: disable=broad-exception-caught

        # Espera o Cloudflare deixar passar até chegar na tela de usuário
        campo_user = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "j_username"))
        )

        campo_user.clear()
        campo_user.send_keys(USUARIO_LOGIN)

        campo_senha = driver.find_element(By.ID, "j_password")
        campo_senha.clear()
        campo_senha.send_keys(SENHA_LOGIN)

        print("   -> Credenciais inseridas. Aguardando a IA devorar o Captcha...")

        # Aguarda a Extensão preencher o token secreto do Google ou Cloudflare (timeout de 2 min)
        try:
            WebDriverWait(driver, 120).until(
                lambda d: (
                    (d.find_elements(By.ID, "g-recaptcha-response") and d.find_element(By.ID, "g-recaptcha-response").get_attribute("value") != "") or
                    (d.find_elements(By.NAME, "cf-turnstile-response") and d.find_element(By.NAME, "cf-turnstile-response").get_attribute("value") != "")
                )
            )
            print("   -> [SUCESSO IA] O enigma foi resolvido! Processando entrada...")
        except Exception: # pylint: disable=broad-exception-caught
            print("   -> [AVISO] Timeout aguardando a resposta da IA ou Captcha invisível.")

        time.sleep(2)
        campo_senha.send_keys(Keys.ENTER) # Aperta enter para enviar o formulário
        time.sleep(6) # Tempo de tolerância para a página interna carregar

        # Verificação definitiva de Sucesso
        driver.switch_to.default_content()
        try:
            # Procura por um menu que só existe na área logada
            driver.find_element(By.NAME, "LeftFrame")
            return True
        except Exception: # pylint: disable=broad-exception-caught
            # Segunda checagem pela URL
            if "login" not in driver.current_url.lower():
                return True
            return False

    except Exception as e: # pylint: disable=broad-exception-caught
        print(f"   -> [ERRO PORTARIA] Sequência de login falhou: {e}")
        return False


def loop_servico():
    """Loop de execução contínua com IA Integrada."""
    print("\n>>> INICIANDO SISTEMA AUTÔNOMO V3 (Selenium + CapSolver IA) <<<")

    chave_api = carregar_chave_capsolver()
    if not chave_api:
        print("[CRÍTICO] A linha 'CAPSOLVER_KEY=' não foi encontrada no 'config.txt'.")
        return

    # Baixa e configura o Cérebro do CapSolver
    caminho_extensao = preparar_capsolver(chave_api)

    # --- CONFIGURAÇÃO DO MODO FANTASMA (SELENIUM TRADICIONAL) ---
    chrome_options = Options()
    chrome_options.add_argument("--headless=new") # Suporta extensões perfeitamente
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")

    if caminho_extensao:
        chrome_options.add_argument(f"--load-extension={caminho_extensao}")

    # Máscara do Windows para manter a pontuação de confiança alta
    mascara = "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    chrome_options.add_argument(mascara)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )
    # -----------------------------------------------------------

    autenticado = False
    ultimo_keep_alive = time.time()

    while True:
        try:
            # =============================================================
            # PORTARIA: LOGIN AUTÔNOMO COM IA
            # =============================================================
            if not autenticado:
                if fazer_login_com_ia(driver):
                    print("[LIBERADO] Estamos dentro! O Robô assumiu o controle.")
                    autenticado = True
                    ultimo_keep_alive = time.time()
                else:
                    print("[BARRADO] O login falhou. Tentando novamente em 60 segundos...")
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
                    df = pd.read_csv(ARQUIVO_EM_PROCESSAMENTO, sep=',', dtype=str)
                    df.columns = [c.strip() for c in df.columns]
                except Exception: # pylint: disable=broad-exception-caught
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
                            except Exception as e: # pylint: disable=broad-exception-caught
                                print(f"   [ERRO API] Falha na planilha do vendedor: {e}")

                        if sheet_geral:
                            try:
                                atualizar_planilha_geral(sheet_geral, row.to_dict(), dados, contrato)
                                anotou_geral = True
                            except Exception as e: # pylint: disable=broad-exception-caught
                                print(f"   [ERRO API] Falha na planilha GERAL: {e}")

                        if anotou_vend or anotou_geral:
                            destino_log = f"{nome_planilha_vendedor} + GERAL" if (anotou_vend and anotou_geral) else (nome_planilha_vendedor if anotou_vend else f"{PREFIXO_PLANILHA}GERAL")
                            print(f"   [OK] Registrado com sucesso em: {destino_log}")

                            texto_status = "1º Parcela Paga" if dados['pago'] else "1º Parcela Não Paga"
                            salvar_historico_concluido(contrato, destino_log, vendedor, telefone_vendedor, texto_status)

                            if not dados['pago']:
                                adicionar_para_reanalise(contrato, vendedor, telefone_vendedor, nome_planilha_vendedor, origem, row.to_dict())
                        else:
                            print("   [CRÍTICO] Falha ao atualizar. Devolvendo contrato para a fila...")
                            if not os.path.exists(ARQUIVO_FILA):
                                with open(ARQUIVO_FILA, 'w', encoding='utf-8') as f_fila:
                                    f_fila.write("contrato,origem,vendedor,lance livre,telefone\n")
                            with open(ARQUIVO_FILA, 'a', encoding='utf-8') as f_fila:
                                f_fila.write(f"{row.get('contrato', '')},{row.get('origem', '')},{row.get('vendedor', '')},{row.get('lance livre', '')},{row.get('telefone', '')}\n")
                    else:
                        print(f"   [ERRO] Contrato {contrato} não localizado no portal.")

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
                info['tentativas'] = info.get('tentativas', 0) + 1
                mudou_pendentes = True

                print(f"\n[REANÁLISE] Verificando {contrato} (Tentativa {info['tentativas']}/{MAX_TENTATIVAS})...")

                if buscar_contrato(driver, contrato):
                    if verificar_apenas_pagamento(driver):
                        print("   [PAGAMENTO DETECTADO] Atualizando status nas planilhas...")
                        anotou_vend, anotou_geral = False, False

                        sheet_v = conectar_google_sheets(info['nome_planilha'], NOME_ABA)
                        if sheet_v:
                            linha_v = encontrar_linha_do_contrato(sheet_v, contrato, col_idx=13)
                            if linha_v:
                                try: sheet_v.update_cell(linha_v, 2, "1º Parcela Paga"); anotou_vend = True
                                except Exception: # pylint: disable=broad-exception-caught
                                    pass

                        sheet_g = conectar_google_sheets(f"{PREFIXO_PLANILHA}GERAL", NOME_ABA_GERAL)
                        if sheet_g:
                            linha_g = encontrar_linha_do_contrato(sheet_g, contrato, col_idx=12)
                            if linha_g:
                                try: sheet_g.update_cell(linha_g, 1, "1º Parcela Paga"); anotou_geral = True
                                except Exception: # pylint: disable=broad-exception-caught
                                    pass

                        if anotou_vend or anotou_geral:
                            destino_log = f"{info['nome_planilha']} + GERAL" if (anotou_vend and anotou_geral) else (info['nome_planilha'] if anotou_vend else f"{PREFIXO_PLANILHA}GERAL")
                            salvar_historico_concluido(contrato, destino_log, info['vendedor_nome'], info['vendedor_tel'], "1º Parcela Paga (Reanálise)")
                            del pendentes[contrato]
                    else:
                        if info['tentativas'] >= MAX_TENTATIVAS:
                            print(f" [EXPIROU] Contrato {contrato} atingiu o limite de tentativas.")
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
                    print("[AVISO] Sessão expirada ou perdida. Forçando Relogin da IA...")
                    autenticado = False
                ultimo_keep_alive = time.time()

            time.sleep(1)

        except Exception as e: # pylint: disable=broad-exception-caught
            print(f"[ERRO GERAL NO LOOP] A execução foi protegida. Detalhe: {e}")
            time.sleep(2)


if __name__ == "__main__":
    loop_servico()
