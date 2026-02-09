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
    except gspread.SpreadsheetNotFound:
        return "NAO_ENCONTRADA"
    except Exception as e:
        print(f"   [ERRO SHEETS] {e}")
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
    if not texto:
        return 0.00
    try:
        match = re.search(r'([\d\.]+,\d{2})', str(texto))
        if match:
            valor_limpo = match.group(1)
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


def ir_para_menu_consorciado(driver):
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
    except:
        pass
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame")))
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
    except:
        pass


def ir_para_conteudo_busca(driver):
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
    except:
        pass
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame")))
    except:
        pass


def extrair_dados_navegador(driver):
    dados = {'credito': 0.00, 'nome': '-', 'telefone': '-',
             'data_venda': '', 'grupo': '-', 'cota': '-'}

    try:
        dados['nome'] = driver.find_element(
            By.XPATH, "//td[contains(text(), 'Consorciado:')]/following-sibling::td").text
    except:
        pass

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

        # Cria colunas de status se não existirem
        if 'status' not in df.columns:
            df['status'] = ''
        if 'detalhe' not in df.columns:
            df['detalhe'] = ''

    except Exception as e:
        print(f"ERRO CRÍTICO AO LER CSV: {e}")
        return

    print("Abrindo Intranet...")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    driver.get("https://intranet.consorciotradicao.com.br/autocred/")

    print("\n=======================================================")
    print(" FAÇA O LOGIN MANUALMENTE.")
    print(" O robô detectará automaticamente quando o menu carregar.")
    print("=======================================================\n")

    # --- LOGICA DE LOGIN ROBUSTA ---
    try:
        # Espera até 300 segundos (5 min) para o frame principal aparecer
        WebDriverWait(driver, 300).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
        # Se achou o frame, volta pra raiz pra começar a navegação certa
        driver.switch_to.default_content()
        print(">>> LOGIN DETECTADO! Iniciando operações...")
        time.sleep(2)
    except:
        print("Tempo de login esgotado ou erro de detecção.")
        return

    for index, row in df.iterrows():
        try:
            contrato = row.get('contrato')
            origem = row.get('origem')
            vendedor = row.get('vendedor')
            lance_livre = row.get('lance livre')

            if pd.isna(contrato):
                continue

            # Validação Vendedor
            if pd.isna(vendedor) or str(vendedor).strip() == "":
                df.at[index, 'status'] = 'FALHA'
                df.at[index, 'detalhe'] = 'Vendedor em branco'
                print(f"   [FALHA] {contrato}: Vendedor vazio.")
                continue

            nome_planilha = f"{PREFIXO_PLANILHA}{str(vendedor).strip()}"
            print(f"Processando: {contrato} -> {vendedor}")

            sheet = conectar_google_sheets(nome_planilha)

            if sheet == "NAO_ENCONTRADA":
                df.at[index, 'status'] = 'FALHA'
                df.at[index,
                      'detalhe'] = f"Planilha '{nome_planilha}' não existe"
                print(f"   [FALHA] Planilha não encontrada.")
                continue
            elif sheet is None:
                df.at[index, 'status'] = 'ERRO_API'
                df.at[index, 'detalhe'] = "Erro conexão Google"
                continue

            ir_para_menu_consorciado(driver)
            ir_para_conteudo_busca(driver)

            # Busca
            WebDriverWait(driver, 10).until(EC.presence_of_element_located(
                (By.NAME, "NumeroContrato"))).send_keys(contrato)
            driver.find_element(
                By.XPATH, "//input[contains(@value, 'Localizar')]").click()
            time.sleep(2)

            # Tenta clicar no resultado
            try:
                driver.find_element(
                    By.XPATH, f"//td/div[contains(text(), '{contrato}')] | //td[contains(@class, 'hand')]/div").click()
                time.sleep(2)
            except:
                pass

            ir_para_conteudo_busca(driver)

            # Verifica se achou contrato
            try:
                driver.find_element(
                    By.XPATH, "//td[contains(text(), 'Consorciado:')]")
            except:
                df.at[index, 'status'] = 'NAO_ENCONTRADO'
                df.at[index, 'detalhe'] = 'Contrato inexistente na Intranet'
                print("   [FALHA] Contrato não encontrado na busca.")
                continue

            dados = extrair_dados_navegador(driver)

            if dados:
                linha = encontrar_proxima_linha_vazia(sheet)

                part1 = [str(dados.get('data_venda', '')), str(dados.get('nome', '')), str(
                    dados.get('telefone', '')), "", str(origem)]
                part2 = [dados['credito'], formatar_lance(lance_livre), "", str(
                    contrato), str(dados['grupo']), str(dados['cota'])]

                sheet.update(range_name=f"D{linha}:H{linha}", values=[
                             part1], value_input_option='USER_ENTERED')
                sheet.update(range_name=f"J{linha}:O{linha}", values=[
                             part2], value_input_option='USER_ENTERED')

                df.at[index, 'status'] = 'SUCESSO'
                df.at[index, 'detalhe'] = f"Salvo linha {linha}"
                print("     [SUCESSO] Dados salvos.")
            else:
                df.at[index, 'status'] = 'ERRO_EXTRACAO'
                df.at[index, 'detalhe'] = "Falha ao ler dados da tela"
                print("     [ERRO] Dados não extraídos.")

        except Exception as e:
            df.at[index, 'status'] = 'ERRO_GERAL'
            df.at[index, 'detalhe'] = str(e)
            print(f"     [ERRO] {e}")

    driver.quit()

    # CORREÇÃO DO NOME DO ARQUIVO PARA EVITAR ERRO DE CAMINHO
    nome_base = os.path.basename(arquivo_csv)
    nome_relatorio = f"RELATORIO_{nome_base}"

    # Salva na mesma pasta onde o script está
    caminho_completo_relatorio = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), nome_relatorio)

    df.to_csv(caminho_completo_relatorio, index=False,
              sep=',', encoding='utf-8-sig')
    print(f"\nRelatório gerado: {caminho_completo_relatorio}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # sys.argv[1] é o caminho do arquivo que você arrastou
        processar_csv(sys.argv[1])
    else:
        print("Arraste um arquivo CSV sobre este script para processar.")
        input("Pressione Enter para sair...")
