import time
import pandas as pd
import gspread
import traceback
import sys
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# --- CONFIGURAÇÕES FIXAS ---
PREFIXO_PLANILHA = 'Controle de Vendas -- '
NOME_ABA = 'JANEIRO'


def conectar_google_sheets(nome_planilha):
    print(f"   > Tentando abrir planilha: '{nome_planilha}'...")
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        'credentials.json', scope)
    client = gspread.authorize(creds)
    try:
        sheet = client.open(nome_planilha).worksheet(NOME_ABA)
        return sheet
    except gspread.SpreadsheetNotFound:
        print(f"   [ERRO] A planilha '{nome_planilha}' não foi encontrada.")
        print("   Verifique se o nome do vendedor está certo no CSV e se o arquivo existe no Drive.")
        return None
    except gspread.WorksheetNotFound:
        print(
            f"   [ERRO] A aba '{NOME_ABA}' não existe na planilha '{nome_planilha}'.")
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
        texto_limpo = texto.replace("R$", "").strip()
        texto_limpo = texto_limpo.replace(".", "").replace(",", ".")
        return float(texto_limpo)
    except:
        return texto


def formatar_lance(valor):
    if pd.isna(valor) or str(valor).strip() == "":
        return ""
    try:
        valor_str = str(valor).replace(",", ".")
        return float(valor_str)
    except:
        return valor

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
    dados = {}
    try:
        print("   > Extraindo dados da cota...")
        try:
            dados['nome'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Consorciado:')]/following-sibling::td").text
        except:
            dados['nome'] = "-"

        try:
            raw_credito = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Crédito:')]/following-sibling::td").text
            dados['credito'] = limpar_valor_credito(raw_credito)
        except:
            dados['credito'] = 0.00

        try:
            dados['data_venda'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Adesão:')]/following-sibling::td").text
        except:
            dados['data_venda'] = ""

        try:
            dados['grupo'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Grupo:')]/following-sibling::td").text.strip()
            dados['cota'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Cota:')]/following-sibling::td").text.strip()
        except:
            dados['grupo'] = "-"
            dados['cota'] = "-"

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
            dados['telefone'] = "-"

        return dados
    except Exception as e:
        print(f"Erro na extração: {e}")
        return None


def main():
    # 1. Leitura Inteligente do CSV
    print("Lendo 'contratos.csv'...")
    try:
        # Tenta ler com vírgula (padrão do texto que você mandou)
        df = pd.read_csv('contratos.csv', sep=',',
                         dtype=str, encoding='utf-8-sig')

        # Se só achou 1 coluna, é porque provavelmente foi salvo no Excel (que usa ;)
        if len(df.columns) <= 1:
            print("   > Detectado formato Excel (;), ajustando...")
            df = pd.read_csv('contratos.csv', sep=';',
                             dtype=str, encoding='utf-8-sig')

        # Limpa espaços vazios nos nomes das colunas (ex: "vendedor " vira "vendedor")
        df.columns = df.columns.str.strip()

        # Validação das Colunas
        colunas_necessarias = ['contrato', 'origem', 'vendedor', 'lance livre']
        for col in colunas_necessarias:
            if col not in df.columns:
                print(f"ERRO CRÍTICO: Coluna '{col}' não encontrada no CSV.")
                print(f"Colunas encontradas: {list(df.columns)}")
                return

    except Exception as e:
        print(f"ERRO ao ler CSV: {e}")
        return

    print("Abrindo navegador...")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    driver.get("https://intranet.consorciotradicao.com.br/autocred/")

    print("\n" + "="*60)
    print(" ATENÇÃO: FAÇA O LOGIN E RESOLVA O CAPTCHA AGORA.")
    input(" Pressione [ENTER] aqui no terminal APÓS estar logado...")
    print("="*60 + "\n")

    for index, row in df.iterrows():
        contrato = row['contrato']
        origem = row['origem']
        vendedor = row['vendedor']
        # ATENÇÃO: Agora com espaço, conforme seu CSV
        lance_livre = row['lance livre']

        if pd.isna(contrato) or contrato == "":
            continue

        # PULA se não tiver vendedor (ex: linha vazia ou só com origem)
        if pd.isna(vendedor) or str(vendedor).strip() == "":
            print(
                f"   [PULANDO] Contrato {contrato} não tem Vendedor preenchido.")
            continue

        nome_planilha_alvo = f"{PREFIXO_PLANILHA}{str(vendedor).strip()}"

        print(f"Processando: Contrato {contrato} | Vendedor: {vendedor}")

        sheet = conectar_google_sheets(nome_planilha_alvo)
        if not sheet:
            continue

        try:
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
                time.sleep(3)
            except:
                pass

            ir_para_conteudo_busca(driver)
            dados = extrair_dados_navegador(driver)

            if dados:
                linha = encontrar_proxima_linha_vazia(sheet)
                print(
                    f"   > Salvando na linha {linha} da planilha de {vendedor}...")

                lista_parte1 = [
                    str(dados.get('data_venda', '')),
                    str(dados.get('nome', '')),
                    str(dados.get('telefone', '')),
                    "",
                    str(origem)
                ]

                lista_parte2 = [
                    dados.get('credito', 0.00),
                    formatar_lance(lance_livre),  # Usa a coluna 'lance livre'
                    "",
                    str(contrato),
                    str(dados.get('grupo', '')),
                    str(dados.get('cota', ''))
                ]

                sheet.update(
                    range_name=f"D{linha}:H{linha}", values=[lista_parte1])
                sheet.update(
                    range_name=f"J{linha}:O{linha}", values=[lista_parte2])

                print("   [OK] Sucesso.")
            else:
                print("   [!] Dados não encontrados no site.")

        except Exception as e:
            print(f"   [ERRO] Falha: {e}")
            traceback.print_exc()

    print("\nFim do processamento.")


if __name__ == "__main__":
    main()
