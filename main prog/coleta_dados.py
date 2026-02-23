import sys
import os
import time
import gspread
import shutil
import re
import json
import winsound
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
ARQUIVO_PENDENTES = 'pendentes_reanalise.json'
ARQUIVO_CONFIG = 'config.txt'

# --- CONFIGURAÇÕES ---
INTERVALO_REANALISE_SEGUNDOS = 60 # 1 Minuto
MAX_TENTATIVAS = 300
TEMPO_INATIVIDADE_MAXIMO = 300 # 5 Minutos

def carregar_configuracoes():
    config = {
        "MATRICULA": "",
        "SENHA": "",
        "PREFIXO_PLANILHA": "Controle de Vendas -- ",
        "NOME_ABA": "JANEIRO"
    }
    if os.path.exists(ARQUIVO_CONFIG):
        try:
            with open(ARQUIVO_CONFIG, 'r', encoding='utf-8') as f:
                for linha in f:
                    if '=' in linha:
                        chave, valor = linha.split('=', 1)
                        config[chave.strip()] = valor.replace('\n', '').replace('\r', '')
            return config
        except: return None
    else: return None

CONFIG = carregar_configuracoes()
USUARIO_LOGIN = CONFIG.get("MATRICULA") if CONFIG else ""
SENHA_LOGIN = CONFIG.get("SENHA") if CONFIG else ""
PREFIXO_PLANILHA = CONFIG.get("PREFIXO_PLANILHA") if CONFIG else ""
NOME_ABA = CONFIG.get("NOME_ABA") if CONFIG else ""

def carregar_pendentes():
    if os.path.exists(ARQUIVO_PENDENTES):
        try:
            with open(ARQUIVO_PENDENTES, 'r') as f: return json.load(f)
        except: return {}
    return {}

def salvar_pendentes(dados):
    with open(ARQUIVO_PENDENTES, 'w') as f: json.dump(dados, f, indent=4)

def adicionar_para_reanalise(contrato, vendedor_nome, vendedor_tel, nome_planilha, origem, dados_completos):
    pendentes = carregar_pendentes()
    pendentes[contrato] = {
        "vendedor_nome": vendedor_nome,
        "vendedor_tel": vendedor_tel,
        "nome_planilha": nome_planilha,
        "origem": origem,
        "dados_originais": dados_completos,
        "tentativas": 0,
        "proxima_verificacao": time.time() + INTERVALO_REANALISE_SEGUNDOS
    }
    salvar_pendentes(pendentes)
    print(f"   [AGENDADO] Contrato {contrato} agendado para reanálise em 1 minuto.")

def salvar_historico_concluido(contrato, nome_planilha, vendedor_nome, vendedor_tel, status_pag):
    existe = os.path.exists(ARQUIVO_HISTORICO_SUCESSO)
    try:
        with open(ARQUIVO_HISTORICO_SUCESSO, 'a', encoding='utf-8') as f:
            if not existe:
                f.write("contrato,planilha_destino,data_registro,vendedor_nome,vendedor_tel,status_pagamento\n")
            
            data_hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            safe_planilha = str(nome_planilha).replace(",", ".")
            safe_nome = str(vendedor_nome).replace(",", ".")
            
            linha = f"{contrato},{safe_planilha},{data_hora},{safe_nome},{vendedor_tel},{status_pag}\n"
            f.write(linha)
    except Exception as e:
        print(f"   [ERRO HISTÓRICO] {e}")

def conectar_google_sheets(nome_planilha):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
    client = gspread.authorize(creds)
    try:
        return client.open(nome_planilha).worksheet(NOME_ABA)
    except gspread.SpreadsheetNotFound:
        return "NAO_ENCONTRADA"
    except: return None

def encontrar_proxima_linha_vazia(sheet):
    coluna_d = sheet.col_values(4)
    if len(coluna_d) < 12: return 12
    for i in range(11, len(coluna_d) + 20):
        try:
            if i >= len(coluna_d) or not coluna_d[i]: return i + 1
        except: return i + 1
    return 12

# NOVA FUNÇÃO: Procura a linha exata do contrato na planilha
def encontrar_linha_do_contrato(sheet, contrato):
    try:
        # A Coluna M é a 13ª coluna da planilha onde o contrato é salvo
        coluna_m = sheet.col_values(13)
        # Varre a coluna procurando o número (enumerate começa no 1 para bater com a linha da planilha)
        for i, valor in enumerate(coluna_m, start=1):
            if str(contrato).strip() == str(valor).strip():
                return i # Retorna o número da linha
    except Exception as e:
        print(f"   [ERRO BUSCA PLANILHA] {e}")
    return None

def limpar_inteiro(texto):
    try:
        numeros = re.sub(r'\D', '', str(texto))
        return int(numeros) if numeros else 0
    except: return 0

def limpar_valor(texto):
    try:
        match = re.search(r'([\d\.]+,\d{2})', str(texto))
        if match: return float(match.group(1).replace('.', '').replace(',', '.'))
        return 0.00
    except: return 0.00

def fazer_login_automatico(driver):
    if not USUARIO_LOGIN or not SENHA_LOGIN: return False
    try:
        driver.get("https://intranet.consorciotradicao.com.br/autocred/")
        try:
            WebDriverWait(driver, 3).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
            driver.switch_to.default_content()
            return True
        except: pass
        
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "j_username")))
        driver.find_element(By.ID, "j_username").send_keys(USUARIO_LOGIN)
        driver.find_element(By.ID, "j_password").send_keys(SENHA_LOGIN)
        
        print("\n" + "="*60)
        print(" AGUARDANDO CAPTCHA... POR FAVOR RESOLVA!")
        print("="*60 + "\n")
        
        WebDriverWait(driver, 600).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
        driver.switch_to.default_content()
        
        print("\n LOGIN DETECTADO!")
        try: winsound.Beep(1000, 500) 
        except: pass
        return True
    except: return False

def buscar_contrato(driver, contrato):
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame")))
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
    except: pass
    
    time.sleep(1)
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame")))
        
        campo = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.NAME, "NumeroContrato")))
        campo.clear()
        campo.send_keys(contrato)
        driver.find_element(By.XPATH, "//input[contains(@value, 'Localizar')]").click()
        time.sleep(1.5)
        
        try: driver.find_element(By.XPATH, f"//td/div[contains(text(), '{contrato}')] | //td[contains(@class, 'hand')]/div").click()
        except: pass
        time.sleep(1)
        
        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame")))
        
        driver.find_element(By.XPATH, "//td[contains(text(), 'Consorciado:')]")
        return True
    except:
        return False

def verificar_apenas_pagamento(driver):
    try:
        elem = driver.find_element(By.XPATH, "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td")
        valor_pago = limpar_inteiro(elem.text)
        return (valor_pago > 0)
    except: 
        return False

def extrair_dados_completos(driver):
    dados = {'credito': 0.00, 'nome': '-', 'telefone': '-', 'data_venda': '', 'grupo': '-', 'cota': '-', 'pago': False}
    
    try: dados['nome'] = driver.find_element(By.XPATH, "//td[contains(text(), 'Consorciado:')]/following-sibling::td").text
    except: pass
    try: dados['credito'] = limpar_valor(driver.find_element(By.XPATH, "//td[contains(text(), 'Crédito:')]/following-sibling::td").text)
    except: pass
    try: dados['data_venda'] = driver.find_element(By.XPATH, "//td[contains(text(), 'Adesão:')]/following-sibling::td").text
    except: pass
    try: dados['grupo'] = driver.find_element(By.XPATH, "//td[contains(text(), 'Grupo:')]/following-sibling::td").text.strip()
    except: pass
    try: dados['cota'] = driver.find_element(By.XPATH, "//td[contains(text(), 'Cota:')]/following-sibling::td").text.strip()
    except: pass
    
    try:
        elem = driver.find_element(By.XPATH, "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td")
        valor_pago = limpar_inteiro(elem.text)
        if valor_pago > 0: dados['pago'] = True
    except: dados['pago'] = False

    try:
        driver.find_element(By.XPATH, "//*[contains(text(), 'Telefones')]").click()
        time.sleep(1.5)
        xpath_celular = "//td[contains(text(), 'Celular')]/parent::tr/td[2]"
        dados['telefone'] = driver.find_element(By.XPATH, xpath_celular).text.strip()
    except: pass

    return dados

def atualizar_planilha(sheet, row_csv, dados_site, contrato):
    linha = encontrar_proxima_linha_vazia(sheet)
    status_pag = "1º Parcela Paga" if dados_site['pago'] else "" 
    
    p1 = [str(dados_site['data_venda']), str(dados_site['nome']), str(dados_site['telefone']), "", str(row_csv.get('origem'))]
    
    lance_val = 0.00
    try: lance_val = float(str(row_csv.get('lance livre', 0)).replace("R$", "").replace(".", "").replace(",", ".").strip())
    except: pass
    
    p2 = [dados_site['credito'], lance_val, "", str(contrato), str(dados_site['grupo']), str(dados_site['cota'])]

    sheet.update_cell(linha, 2, status_pag) 
    sheet.update(range_name=f"D{linha}:H{linha}", values=[p1], value_input_option='USER_ENTERED')
    sheet.update(range_name=f"J{linha}:O{linha}", values=[p2], value_input_option='USER_ENTERED')
    
    return status_pag

def manter_sessao_viva(driver):
    try:
        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
        WebDriverWait(driver, 5).until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame")))
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Keep-Alive: Sessão renovada.")
        return True
    except: return False

def loop_servico():
    print(">>> SERVIÇO DE COLETA E REANÁLISE (MODO TURBO) <<<")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    if not fazer_login_automatico(driver): return

    ultimo_keep_alive = time.time()

    while True:
        try:
            # 1. PROCESSAMENTO DE NOVOS
            if os.path.exists(ARQUIVO_FILA):
                try: shutil.move(ARQUIVO_FILA, ARQUIVO_EM_PROCESSAMENTO)
                except: time.sleep(1); continue

                ultimo_keep_alive = time.time()

                try:
                    df = pd.read_csv(ARQUIVO_EM_PROCESSAMENTO, sep=',', dtype=str)
                    df.columns = [c.strip() for c in df.columns] 
                except:
                    if os.path.exists(ARQUIVO_EM_PROCESSAMENTO): os.remove(ARQUIVO_EM_PROCESSAMENTO)
                    continue

                for index, row in df.iterrows():
                    contrato = row.get('contrato')
                    vendedor = row.get('vendedor')
                    vendedor_tel = row.get('telefone')
                    
                    if pd.isna(contrato): continue

                    print(f"\nProcessando Novo: {contrato} ({vendedor})...")
                    nome_planilha = f"{PREFIXO_PLANILHA}{str(vendedor).strip()}"
                    sheet = conectar_google_sheets(nome_planilha)

                    if not sheet: 
                        print(f"   [ERRO] Planilha {nome_planilha} não acessível.")
                        continue

                    if buscar_contrato(driver, contrato):
                        dados = extrair_dados_completos(driver)
                        
                        status_final = atualizar_planilha(sheet, row, dados, contrato)
                        
                        texto_status = "1º Parcela Paga" if dados['pago'] else "1º Parcela Não Paga"
                        salvar_historico_concluido(contrato, nome_planilha, vendedor, vendedor_tel, texto_status)
                        
                        if not dados['pago']:
                            dados_completos_para_json = row.to_dict()
                            adicionar_para_reanalise(contrato, vendedor, vendedor_tel, nome_planilha, row.get('origem'), dados_completos_para_json)
                        
                        print("   [SUCESSO] Processado.")
                    else:
                        print("   [ERRO] Contrato não achado no site.")

                if os.path.exists(ARQUIVO_EM_PROCESSAMENTO): os.remove(ARQUIVO_EM_PROCESSAMENTO)

            # 2. REANÁLISE RÁPIDA (COM BUSCA DE LINHA EXISTENTE)
            pendentes = carregar_pendentes()
            agora = time.time()
            mudou_pendentes = False
            lista_pendentes = list(pendentes.items()) 

            for contrato, info in lista_pendentes:
                if agora >= info['proxima_verificacao']:
                    ultimo_keep_alive = time.time()
                    print(f"\n[REANÁLISE] Verificando {contrato} (Tentativa {info['tentativas']+1}/{MAX_TENTATIVAS})...")
                    
                    if buscar_contrato(driver, contrato):
                        pagou = verificar_apenas_pagamento(driver)
                        
                        if pagou:
                            print("   [PAGAMENTO DETECTADO] Procurando linha original na planilha...")
                            sheet = conectar_google_sheets(info['nome_planilha'])
                            if sheet:
                                linha_existente = encontrar_linha_do_contrato(sheet, contrato)
                                
                                if linha_existente:
                                    # Atualiza APENAS a Coluna B (Status Adimplência) da linha que já existe
                                    sheet.update_cell(linha_existente, 2, "1º Parcela Paga")
                                    print(f"   [SUCESSO] Status atualizado direto na linha {linha_existente}!")
                                else:
                                    # Fallback: Se alguém apagou a linha original sem querer, cria uma nova
                                    print("   [AVISO] Linha original sumiu! Criando uma nova por segurança...")
                                    dados_completos = extrair_dados_completos(driver)
                                    atualizar_planilha(sheet, info['dados_originais'], dados_completos, contrato)

                                salvar_historico_concluido(contrato, info['nome_planilha'], info['vendedor_nome'], info['vendedor_tel'], "1º Parcela Paga (Reanálise)")
                                del pendentes[contrato]
                                mudou_pendentes = True
                        else:
                            print("   [AINDA NÃO PAGO] Reagendando...")
                            info['tentativas'] += 1
                            if info['tentativas'] >= MAX_TENTATIVAS:
                                print("   [EXPIROU] Desistindo.")
                                del pendentes[contrato]
                            else:
                                info['proxima_verificacao'] = agora + INTERVALO_REANALISE_SEGUNDOS
                            mudou_pendentes = True
            
            if mudou_pendentes:
                salvar_pendentes(pendentes)

            # 3. KEEP ALIVE
            if (time.time() - ultimo_keep_alive) > TEMPO_INATIVIDADE_MAXIMO:
                if not manter_sessao_viva(driver):
                    print("[ERRO] Sessão perdida.") 
                    fazer_login_automatico(driver)
                ultimo_keep_alive = time.time()

            time.sleep(5) 

        except Exception as e:
            print(f"Erro Loop Geral: {e}")
            time.sleep(5)

if __name__ == "__main__":
    loop_servico()