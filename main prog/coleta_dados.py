import sys
import os
import time
import gspread
import shutil
import re
from datetime import datetime
import pandas as pd
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# --- ARQUIVOS DO SISTEMA ---
ARQUIVO_FILA = 'fila_vendas.csv'
ARQUIVO_EM_PROCESSAMENTO = 'temp_processando.csv'
ARQUIVO_HISTORICO_SUCESSO = 'historico_concluidos.csv'
ARQUIVO_CONFIG = 'config.txt'

# --- FUNÇÃO PARA SALVAR HISTÓRICO CSV ---
def salvar_historico_concluido(contrato, nome_planilha):
    # Verifica se o arquivo existe para criar cabeçalho
    existe = os.path.exists(ARQUIVO_HISTORICO_SUCESSO)
    try:
        with open(ARQUIVO_HISTORICO_SUCESSO, 'a', encoding='utf-8') as f:
            if not existe:
                f.write("contrato,planilha_destino,data_registro\n")
            
            data_hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            linha = f"{contrato},{nome_planilha},{data_hora}\n"
            f.write(linha)
            print(f"   [HISTÓRICO] Contrato {contrato} registrado no CSV com sucesso.")
    except Exception as e:
        print(f"   [ERRO HISTÓRICO] Não foi possível salvar no histórico: {e}")

# --- FUNÇÃO PARA LER CONFIGURAÇÕES ---
def carregar_configuracoes():
    config = {
        "MATRICULA": "",
        "SENHA": "",
        "PREFIXO_PLANILHA": "Controle de Vendas -- ",
        "NOME_ABA": "JANEIRO"
    }
    
    print(f"Lendo configurações de: {ARQUIVO_CONFIG}...")
    if os.path.exists(ARQUIVO_CONFIG):
        try:
            with open(ARQUIVO_CONFIG, 'r', encoding='utf-8') as f:
                for linha in f:
                    if '=' in linha:
                        chave, valor = linha.split('=', 1)
                        config[chave.strip()] = valor.replace('\n', '').replace('\r', '')
            return config
        except Exception as e:
            print(f"   [ERRO] Falha ao ler config.txt: {e}")
            return None
    else:
        print(f"   [ERRO] Arquivo '{ARQUIVO_CONFIG}' não encontrado!")
        return None

CONFIG = carregar_configuracoes()
USUARIO_LOGIN = CONFIG.get("MATRICULA") if CONFIG else ""
SENHA_LOGIN = CONFIG.get("SENHA") if CONFIG else ""
PREFIXO_PLANILHA = CONFIG.get("PREFIXO_PLANILHA") if CONFIG else ""
NOME_ABA = CONFIG.get("NOME_ABA") if CONFIG else ""

def conectar_google_sheets(nome_planilha):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
    client = gspread.authorize(creds)
    try:
        return client.open(nome_planilha).worksheet(NOME_ABA)
    except gspread.SpreadsheetNotFound:
        return "NAO_ENCONTRADA"
    except Exception as e:
        return None

def encontrar_proxima_linha_vazia(sheet):
    coluna_d = sheet.col_values(4)
    if len(coluna_d) < 12: return 12
    for i in range(11, len(coluna_d) + 20):
        try:
            if i >= len(coluna_d) or not coluna_d[i]: return i + 1
        except: return i + 1
    return 12

def limpar_valor_credito(texto):
    try:
        match = re.search(r'([\d\.]+,\d{2})', str(texto))
        if match: return float(match.group(1).replace('.', '').replace(',', '.'))
        return 0.00
    except: return 0.00

def formatar_lance(valor):
    try:
        return float(str(valor).replace("R$", "").replace(".", "").replace(",", ".").strip())
    except: return 0.00

def ir_para_menu_consorciado(driver):
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame")))
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
        time.sleep(1) 
    except: pass

def ir_para_conteudo_busca(driver):
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame")))
    except: pass

def fazer_login_automatico(driver):
    if not USUARIO_LOGIN or not SENHA_LOGIN: return False
    print("\n>>> TENTANDO LOGIN AUTOMÁTICO...")
    try:
        driver.get("https://intranet.consorciotradicao.com.br/autocred/")
        try:
            WebDriverWait(driver, 3).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
            driver.switch_to.default_content()
            return True
        except: pass

        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "j_username")))
        driver.find_element(By.ID, "j_username").clear()
        driver.find_element(By.ID, "j_username").send_keys(USUARIO_LOGIN)
        driver.find_element(By.ID, "j_password").clear()
        driver.find_element(By.ID, "j_password").send_keys(SENHA_LOGIN)
        
        print("\n" + "="*60 + "\n DADOS INSERIDOS! RESOLVA O CAPTCHA E ENTRE.\n" + "="*60 + "\n")
        
        WebDriverWait(driver, 600).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
        driver.switch_to.default_content()
        return True
    except Exception as e:
        print(f"Erro login: {e}")
        return False

def manter_sessao_viva(driver):
    try:
        ir_para_menu_consorciado(driver)
        return True
    except: return False

def extrair_dados_navegador(driver):
    dados = {'credito': 0.00, 'nome': '-', 'telefone': '-', 'data_venda': '', 'grupo': '-', 'cota': '-'}
    try: dados['nome'] = driver.find_element(By.XPATH, "//td[contains(text(), 'Consorciado:')]/following-sibling::td").text
    except: pass
    try:
        raw = driver.find_element(By.XPATH, "//td[contains(text(), 'Crédito:')]/following-sibling::td").text
        dados['credito'] = limpar_valor_credito(raw)
    except: pass
    try: dados['data_venda'] = driver.find_element(By.XPATH, "//td[contains(text(), 'Adesão:')]/following-sibling::td").text
    except: pass
    try:
        dados['grupo'] = driver.find_element(By.XPATH, "//td[contains(text(), 'Grupo:')]/following-sibling::td").text.strip()
        dados['cota'] = driver.find_element(By.XPATH, "//td[contains(text(), 'Cota:')]/following-sibling::td").text.strip()
    except: pass
    try:
        driver.find_element(By.XPATH, "//*[contains(text(), 'Telefones')]").click()
        time.sleep(1.5)
        try:
            xpath_tel = "//td[contains(text(), 'Celular')]/parent::tr/td[2]"
            raw_tel = driver.find_element(By.XPATH, xpath_tel).text.strip()
            try: dados['telefone'] = f"{driver.find_element(By.XPATH, '//td[contains(text(), \"Celular\")]/parent::tr/td[1]').text.strip()}{raw_tel}"
            except: dados['telefone'] = raw_tel
        except: dados['telefone'] = driver.find_element(By.XPATH, "//table//tr[2]/td[2]").text.strip()
    except: pass
    return dados

def loop_servico():
    print(">>> INICIANDO SERVIÇO... <<<")
    if not CONFIG: return
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    if not fazer_login_automatico(driver): return
    ultimo_keep_alive = time.time()

    while True:
        try:
            if os.path.exists(ARQUIVO_FILA):
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Novo lote detectado.")
                try: shutil.move(ARQUIVO_FILA, ARQUIVO_EM_PROCESSAMENTO)
                except: 
                    time.sleep(1)
                    continue

                try:
                    df = pd.read_csv(ARQUIVO_EM_PROCESSAMENTO, sep=',', dtype=str)
                    if len(df.columns) < 4: df = pd.read_csv(ARQUIVO_EM_PROCESSAMENTO, sep=';', dtype=str)
                    df.columns = [c.strip() for c in df.columns]
                except:
                    print("CSV Inválido.")
                    if os.path.exists(ARQUIVO_EM_PROCESSAMENTO): os.remove(ARQUIVO_EM_PROCESSAMENTO)
                    continue

                for index, row in df.iterrows():
                    contrato = row.get('contrato')
                    vendedor = row.get('vendedor')
                    if pd.isna(contrato): continue

                    print(f"Processando: {contrato} ({vendedor})...")
                    nome_planilha = f"{PREFIXO_PLANILHA}{str(vendedor).strip()}"
                    sheet = conectar_google_sheets(nome_planilha)

                    if not sheet or sheet == "NAO_ENCONTRADA":
                        print(f"   [FALHA] Planilha '{nome_planilha}' não encontrada.")
                        continue

                    try:
                        ir_para_menu_consorciado(driver)
                        ir_para_conteudo_busca(driver)
                        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.NAME, "NumeroContrato"))).clear()
                        driver.find_element(By.NAME, "NumeroContrato").send_keys(contrato)
                        driver.find_element(By.XPATH, "//input[contains(@value, 'Localizar')]").click()
                        time.sleep(1.5)

                        try: driver.find_element(By.XPATH, f"//td/div[contains(text(), '{contrato}')] | //td[contains(@class, 'hand')]/div").click()
                        except: pass
                        
                        time.sleep(1)
                        ir_para_conteudo_busca(driver)

                        try: driver.find_element(By.XPATH, "//td[contains(text(), 'Consorciado:')]")
                        except:
                            print("   [ERRO] Contrato não localizado.")
                            continue

                        dados = extrair_dados_navegador(driver)
                        linha = encontrar_proxima_linha_vazia(sheet)
                        
                        p1 = [str(dados['data_venda']), str(dados['nome']), str(dados['telefone']), "", str(row['origem'])]
                        p2 = [dados['credito'], formatar_lance(row['lance livre']), "", str(contrato), str(dados['grupo']), str(dados['cota'])]

                        sheet.update(range_name=f"D{linha}:H{linha}", values=[p1], value_input_option='USER_ENTERED')
                        sheet.update(range_name=f"J{linha}:O{linha}", values=[p2], value_input_option='USER_ENTERED')
                        
                        print("   [SUCESSO] Atualizado.")
                        
                        # --- AQUI ESTÁ A MUDANÇA: SÓ SALVA SE DEU SUCESSO ---
                        salvar_historico_concluido(contrato, nome_planilha)
                        # ---------------------------------------------------

                    except Exception as e:
                        print(f"   [ERRO SITE] {e}")
                        driver.save_screenshot("erro_site.png")
                        if "login" in driver.current_url.lower(): fazer_login_automatico(driver)

                if os.path.exists(ARQUIVO_EM_PROCESSAMENTO): os.remove(ARQUIVO_EM_PROCESSAMENTO)
                ultimo_keep_alive = time.time()

            if (time.time() - ultimo_keep_alive) > 300:
                if not manter_sessao_viva(driver): fazer_login_automatico(driver)
                ultimo_keep_alive = time.time()

            time.sleep(5)

        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Erro loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    loop_servico()