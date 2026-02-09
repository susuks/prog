import sys
import os
import time
import pandas as pd
import gspread
import traceback
import re
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# --- CONFIGURAÇÕES ---
PREFIXO_PLANILHA = 'Controle de Vendas -- '
NOME_ABA = 'JANEIRO'


def conectar_google_sheets(nome_planilha):
    print(f"   > Abrindo planilha: '{nome_planilha}'...")
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        'credentials.json', scope)
    client = gspread.authorize(creds)
    try:
        return client.open(nome_planilha).worksheet(NOME_ABA)
    except Exception as e:
        print(f"   [ERRO] Planilha não encontrada: {e}")
        return None


def encontrar_proxima_linha_vazia(sheet):
    coluna_d = sheet.col_values(4)
    if len(coluna_d) < 12:
        return 12
    for i in range(11, len(coluna_d) + 20):
        try:
            if i >= len(coluna_d) or coluna_d[i] == "" or coluna_d[i] is None:
                return i + 1
        except:
            return i + 1
    return 12


def limpar_valor_credito(texto):
    """
    Busca cirúrgica pelo padrão monetário brasileiro (ex: 64.000,00).
    Evita duplicar números se o site trouxer lixo.
    """
    if not texto:
        return 0.00
    try:
        # Regex: Procura dígitos e pontos, seguidos obrigatoriamente de uma vírgula e 2 dígitos
        # Ex: Pega "64.000,00" de dentro de "R$ 64.000,00"
        match = re.search(r'([\d\.]+,\d{2})', str(texto))
        if match:
            valor_limpo = match.group(1)
            # Remove ponto de milhar e troca vírgula por ponto para o Python entender
            valor_float = float(valor_limpo.replace('.', '').replace(',', '.'))
            return valor_float
        return 0.00
    except:
        return 0.00


def formatar_lance(valor):
    if pd.isna(valor) or str(valor).strip() == "":
        return 0.00
    try:
        val_str = str(valor).replace("R$", "").replace(
            ".", "").replace(",", ".").strip()
        return float(val_str)
    except:
        return 0.00

# --- NAVEGAÇÃO ---


def resetar_frames(driver):
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
    except:
        pass


def ir_para_menu_consorciado(driver):
    resetar_frames(driver)
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame")))
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
    except:
        pass


def ir_para_conteudo_busca(driver):
    resetar_frames(driver)
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame")))
    except:
        pass


def extrair_dados_navegador(driver):
    dados = {'credito': 0.00, 'nome': '-', 'telefone': '-',
             'data_venda': '', 'grupo': '-', 'cota': '-'}
    print("   > Extraindo dados...")

    try:
        dados['nome'] = driver.find_element(
            By.XPATH, "//td[contains(text(), 'Consorciado:')]/following-sibling::td").text
    except:
        pass

    # AQUI ESTAVA O PROBLEMA DO VALOR DUPLICADO
    try:
        raw_credito = driver.find_element(
            By.XPATH, "//td[contains(text(), 'Crédito:')]/following-sibling::td").text
        dados['credito'] = limpar_valor_credito(raw_credito)
    except:
        pass

    try:
        dados['data_venda'] = driver.find_element(
            By.XPATH, "//td[contains(text(), 'Adesão:')]/following-sibling::td").text
    except:
        pass

    try:
        dados['grupo'] = driver.find_element(
            By.XPATH, "//td[contains(text(), 'Grupo:')]/following-sibling::td").text.strip()
        dados['cota'] = driver.find_element(
            By.XPATH, "//td[contains(text(), 'Cota:')]/following-sibling::td").text.strip()
    except:
        pass

    print("   > Buscando telefone...")
    try:
        driver.find_element(
            By.XPATH, "//*[contains(text(), 'Telefones')]").click()
        time.sleep(2)
        try:
            xpath_tel = "//td[contains(text(), 'Celular')]/parent::tr/td[2]"
            raw_tel = driver.find_element(By.XPATH, xpath_tel).text.strip()
            try:
                ddd = driver.find_element(
                    By.XPATH, "//td[contains(text(), 'Celular')]/parent::tr/td[1]").text.strip()
                dados['telefone'] = f"{ddd}{raw_tel}"
            except:
                dados['telefone'] = raw_tel
        except:
            dados['telefone'] = driver.find_element(
                By.XPATH, "//table//tr[2]/td[2]").text.strip()
    except:
        pass

    return dados


def processar_csv(arquivo_csv):
    print(f"\n>>> INICIANDO PROCESSAMENTO: {arquivo_csv} <<<")

    try:
        df = pd.read_csv(arquivo_csv, sep=',', dtype=str, encoding='utf-8')
        if len(df.columns) < 4:
            df = pd.read_csv(arquivo_csv, sep=';', dtype=str, encoding='utf-8')
        df.columns = [c.strip() for c in df.columns]
    except Exception as e:
        print(f"ERRO CRÍTICO AO LER CSV: {e}")
        return

    print("Abrindo Intranet...")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    driver.get("https://intranet.consorciotradicao.com.br/autocred/")

    print("\n" + "="*50)
    print(" FAÇA O LOGIN NA INTRANET E APERTE ENTER AQUI")
    input(" AGUARDANDO LOGIN...")
    print("="*50 + "\n")

    for index, row in df.iterrows():
        try:
            contrato = row.get('contrato')
            origem = row.get('origem')
            vendedor = row.get('vendedor')
            lance_livre = row.get('lance livre')

            if pd.isna(contrato) or pd.isna(vendedor) or str(vendedor).strip() == "":
                continue

            nome_planilha = f"{PREFIXO_PLANILHA}{str(vendedor).strip()}"
            print(f"Processando: {contrato} -> {vendedor}")

            sheet = conectar_google_sheets(nome_planilha)
            if not sheet:
                continue

            ir_para_menu_consorciado(driver)
            ir_para_conteudo_busca(driver)

            WebDriverWait(driver, 10).until(EC.presence_of_element_located(
                (By.NAME, "NumeroContrato"))).send_keys(contrato)
            driver.find_element(
                By.XPATH, "//input[contains(@value, 'Localizar')]").click()
            time.sleep(2)

            try:
                driver.find_element(
                    By.XPATH, f"//td/div[contains(text(), '{contrato}')] | //td[contains(@class, 'hand')]/div").click()
            except:
                pass

            time.sleep(2)
            ir_para_conteudo_busca(driver)
            dados = extrair_dados_navegador(driver)

            if dados:
                linha = encontrar_proxima_linha_vazia(sheet)

                part1 = [str(dados.get('data_venda', '')), str(dados.get('nome', '')), str(
                    dados.get('telefone', '')), "", str(origem)]

                # ENVIA COMO NÚMERO FLOAT PARA A PLANILHA FORMATAR
                part2 = [dados['credito'], formatar_lance(lance_livre), "", str(
                    contrato), str(dados['grupo']), str(dados['cota'])]

                # USER_ENTERED faz o Google Sheets tratar como digitação humana (aplica R$)
                sheet.update(range_name=f"D{linha}:H{linha}", values=[
                             part1], value_input_option='USER_ENTERED')
                sheet.update(range_name=f"J{linha}:O{linha}", values=[
                             part2], value_input_option='USER_ENTERED')

                print("     [Sucesso] Dados salvos.")
            else:
                print("     [Erro] Dados não extraídos.")

        except Exception as e:
            print(f"     [Falha] {e}")
            traceback.print_exc()

    driver.quit()
    print("Fim do script.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        arquivo = sys.argv[1]
        processar_csv(arquivo)
    else:
        print("Este script precisa de um arquivo CSV.")
