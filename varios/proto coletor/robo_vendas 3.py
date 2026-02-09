import time
import pandas as pd
import gspread
import re
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# --- CONFIGURAÇÕES ---
NOME_PLANILHA_GOOGLE = 'Cópia de Controle de Vendas -- '
NOME_ABA = 'JANEIRO'


def conectar_google_sheets():
    print("Conectando ao Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        'credentials.json', scope)
    client = gspread.authorize(creds)
    sheet = client.open(NOME_PLANILHA_GOOGLE).worksheet(NOME_ABA)
    return sheet


def encontrar_proxima_linha_vazia(sheet):
    coluna_d = sheet.col_values(4)  # Coluna D = Data Venda
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
        return "0,00"
    try:
        if "R$" in texto:
            valor = texto.split("R$")[1].strip()
            return valor
        return texto
    except:
        return texto


def extrair_dados_navegador(driver):
    dados = {}
    try:
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame("MainFrame")
        except:
            pass

        # --- TELA 1: DETALHES GERAIS ---
        print("   > Lendo dados da tela principal...")

        # Nome
        try:
            dados['nome'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Consorciado:')]/following-sibling::td").text
        except:
            dados['nome'] = "Não encontrado"

        # Crédito
        try:
            raw_credito = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Crédito:')]/following-sibling::td").text
            dados['credito'] = limpar_valor_credito(raw_credito)
        except:
            dados['credito'] = "0,00"

        # Data Adesão
        try:
            dados['data_venda'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Adesão:')]/following-sibling::td").text
        except:
            dados['data_venda'] = ""

        # Grupo/Cota
        try:
            dados['grupo'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Grupo:')]/following-sibling::td").text.strip()
            dados['cota'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Cota:')]/following-sibling::td").text.strip()
        except:
            dados['grupo'] = "-"
            dados['cota'] = "-"

        # --- TELA 2: TELEFONE ---
        print("   > Indo para aba Telefones...")
        try:
            driver.find_element(By.PARTIAL_LINK_TEXT, "Telefones").click()
            time.sleep(2)

            # Pega celular na tabela
            try:
                # Procura célula na coluna 2 da linha de dados
                xpath_tel = "//td[contains(text(), 'Telefone')]/ancestor::table//tr[2]/td[2]"
                dados['telefone'] = driver.find_element(
                    By.XPATH, xpath_tel).text.strip()
            except:
                dados['telefone'] = "Não achou tel"
        except Exception as e:
            print(f"   (X) Erro ao buscar telefone: {e}")
            dados['telefone'] = ""

        return dados
    except Exception as e:
        print(f"Erro crítico na extração: {e}")
        return None


def main():
    try:
        df = pd.read_csv('contratos.csv', dtype=str)
    except:
        print("ERRO: Crie 'contratos.csv' com colunas: contrato,origem")
        return

    try:
        sheet = conectar_google_sheets()
    except Exception as e:
        print(f"ERRO PLANILHA: {e}")
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

        if pd.isna(contrato) or contrato == "":
            continue

        print(f"Processando contrato {contrato}...")

        # Vai para busca
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame("MainFrame")
        except:
            pass
        driver.get(
            "https://intranet.consorciotradicao.com.br/autocred/Attendance/searchCota.asp")

        try:
            # Preenche Contrato
            campo = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "NumeroContrato")))
            campo.clear()
            campo.send_keys(contrato)

            # CORREÇÃO AQUI: Usa 'contains' para ignorar espaços no value
            driver.find_element(
                By.XPATH, "//input[contains(@value, 'Localizar')]").click()
            time.sleep(2)

            # --- NOVO: CLICAR NO RESULTADO DA LISTA ---
            # Se aparecer a lista "Registros encontrados: 1", clica no link para abrir
            try:
                # Procura qualquer link dentro da tabela de resultados (geralmente no Grupo ou Cota)
                # O XPath procura um link (a) que tenha 'Codigo_Grupo' no endereço, comum nesses sistemas
                link_resultado = driver.find_element(
                    By.XPATH, "//a[contains(@href, 'Codigo_Grupo')]")
                print("   > Clicando no resultado da busca...")
                link_resultado.click()
                time.sleep(2)
            except:
                # Se não achar link, talvez já tenha entrado direto (raro, mas possível)
                pass

            # Extrai
            # Mapeamento para Colunas D a O (Indices 4 a 15)
            # D=Data, E=Nome, F=Tel, G=Pagto, H=Origem, I=Lance, J=Total, K=Bolso, L=Obs, M=Cod, N=Grupo, O=Cota
            # Monta a linha. Importante: Converter para string para evitar erro
            dados = extrair_dados_navegador(driver)

            if dados:
                linha = encontrar_proxima_linha_vazia(sheet)
                print(f"   > Salvando na linha {linha}...")

                nova_linha = [
                    str(dados.get('data_venda', '')),  # D (Data)
                    str(dados.get('nome', '')),        # E (Name)
                    str(dados.get('telefone', '')),    # F (Tel)
                    "",                                # G (Pagamento)
                    str(origem),                       # H (Origem)
                    "",                                # I (Lance)
                    str(dados.get('credito', '')),     # J (Valor Total)
                    "",                                # K (Lance Bolso)
                    "",                                # L (Obs)
                    str(contrato),                     # M (Cod)
                    str(dados.get('grupo', '')),       # N (Grupo)
                    str(dados.get('cota', ''))         # O (Cota)
                ]

                range_nome = f"D{linha}:O{linha}"
                sheet.update(range_nome, [nova_linha])
                print("   [OK] Sucesso.")
            else:
                print("   [!] Dados não encontrados.")

        except Exception as e:
            print(f"   [ERRO] Falha ao processar {contrato}: {e}")

    print("\nFim do processamento.")
    # driver.quit()


if __name__ == "__main__":
    main()
