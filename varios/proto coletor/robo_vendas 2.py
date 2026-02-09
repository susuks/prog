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
# COLOQUE AQUI O NOME EXATO DA PLANILHA COMO APARECE NO SEU GOOGLE DRIVE
NOME_PLANILHA_GOOGLE = 'Cópia de Controle de Vendas -- '
NOME_ABA = 'JANEIRO'  # Verifique se o nome da aba inferior é este mesmo


def conectar_google_sheets():
    print("Conectando ao Google Sheets...")
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        'credentials.json', scope)
    client = gspread.authorize(creds)
    # Abre a planilha e a aba específica
    sheet = client.open(NOME_PLANILHA_GOOGLE).worksheet(NOME_ABA)
    return sheet


def encontrar_proxima_linha_vazia(sheet):
    coluna_d = sheet.col_values(4)  # Coluna D = Data Venda
    # Começa a checar a partir da linha 12 (índice 11)
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
    # Transforma "CREDITO AUTO 64 - R$64.000,00" em "64.000,00"
    if not texto:
        return "0,00"
    try:
        if "R$" in texto:
            valor = texto.split("R$")[1].strip()  # Pega tudo depois do R$
            return valor
        return texto
    except:
        return texto


def extrair_dados_navegador(driver):
    dados = {}

    try:
        # Focar no Frame Principal
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame("MainFrame")
        except:
            pass

        # --- TELA 1: DETALHES GERAIS (Screenshot_1.png) ---
        print("   > Lendo dados da tela principal...")

        # Nome Cliente
        try:
            dados['nome'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Consorciado:')]/following-sibling::td").text
        except:
            dados['nome'] = "Não encontrado"

        # Crédito (Valor Total)
        try:
            raw_credito = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Crédito:')]/following-sibling::td").text
            dados['credito'] = limpar_valor_credito(raw_credito)
        except:
            dados['credito'] = "0,00"

        # Data Venda / Adesão
        try:
            dados['data_venda'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Adesão:')]/following-sibling::td").text
        except:
            dados['data_venda'] = ""

        # Grupo e Cota (Estão no topo, conforme print Screenshot_1.png)
        try:
            dados['grupo'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Grupo:')]/following-sibling::td").text.strip()
            dados['cota'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Cota:')]/following-sibling::td").text.strip()
        except:
            dados['grupo'] = "-"
            dados['cota'] = "-"

        # --- TELA 2: PEGAR TELEFONE (image_986f6e.png) ---
        print("   > Indo para aba Telefones...")

        # Guarda a janela principal para poder voltar depois se precisar
        janela_principal = driver.current_window_handle

        try:
            # Clica no link "Telefones" na barra superior
            driver.find_element(By.PARTIAL_LINK_TEXT, "Telefones").click()
            time.sleep(2)  # Espera carregar

            # Pega o telefone da tabela (image_986f6e.png)
            # Procura a linha que tem um numero de celular (começa com 6, 7, 8 ou 9 e tem tamanho de celular)
            # Ou pega a primeira linha de dados da tabela
            try:
                # Tenta pegar célula abaixo de 'Telefone' na primeira linha de dados
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
    # 1. Ler arquivo de contratos (CSV)
    try:
        df = pd.read_csv('contratos.csv', dtype=str)
    except:
        print("ERRO: Crie o arquivo 'contratos.csv' com as colunas: contrato,origem")
        return

    # 2. Conectar Planilha
    try:
        sheet = conectar_google_sheets()
    except Exception as e:
        print(f"ERRO DE CONEXÃO COM GOOGLE SHEETS: {e}")
        print("Verifique se o arquivo credentials.json está na pasta e se você compartilhou a planilha com o email do robô.")
        return

    # 3. Abrir Navegador
    print("Abrindo navegador...")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    driver.get("https://intranet.consorciotradicao.com.br/autocred/")

    # 4. LOGIN MANUAL
    print("\n" + "="*60)
    print(" ATENÇÃO: FAÇA O LOGIN E RESOLVA O CAPTCHA AGORA.")
    print(" Navegue até a tela inicial do sistema após o login.")
    input(" Pressione [ENTER] aqui no terminal APÓS estar logado...")
    print("="*60 + "\n")

    # 5. Processar lista
    for index, row in df.iterrows():
        contrato = row['contrato']
        origem = row['origem']

        if pd.isna(contrato) or contrato == "":
            continue

        print(f"Processando contrato {contrato}...")

        # Vai para a busca
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame("MainFrame")
        except:
            pass

        # Força ir para a URL de busca para resetar a tela
        driver.get(
            "https://intranet.consorciotradicao.com.br/autocred/Attendance/searchCota.asp")

        try:
            # Preenche Contrato
            campo = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "NumeroContrato")))
            campo.clear()
            campo.send_keys(contrato)

            # Clica em Localizar
            driver.find_element(
                By.XPATH, "//input[@value='Localizar']").click()
            time.sleep(3)  # Espera carregar resultado

            # Extrai
            dados = extrair_dados_navegador(driver)

            if dados:
                linha = encontrar_proxima_linha_vazia(sheet)
                print(f"   > Salvando na linha {linha}...")

                # Mapeamento para Colunas D a O (Indices 4 a 15)
                # D=Data, E=Nome, F=Tel, G=Pagto, H=Origem, I=Lance, J=Total, K=Bolso, L=Obs, M=Cod, N=Grupo, O=Cota

                # Monta a linha. Importante: Converter para string para evitar erro
                nova_linha = [
                    str(dados.get('data_venda', '')),  # D (Data)
                    str(dados.get('nome', '')),        # E (Nome)
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

                # Atualiza o range específico
                range_nome = f"D{linha}:O{linha}"
                sheet.update(range_nome, [nova_linha])
                print("   [OK] Sucesso.")
            else:
                print("   [!] Dados não encontrados.")

        except Exception as e:
            print(f"   [ERRO] Falha ao processar {contrato}: {e}")

    print("\nFim do processamento.")
    # driver.quit() # Comentei para o navegador não fechar sozinho no fim


if __name__ == "__main__":
    main()
