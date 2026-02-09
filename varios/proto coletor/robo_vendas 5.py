import time
import pandas as pd
import gspread
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


def navegar_para_busca(driver):
    """Reseta os frames e vai para o menu de busca"""
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
    except:
        pass

    # Tenta garantir que estamos na tela de busca
    try:
        driver.switch_to.frame("MainFrame")
    except:
        pass


def extrair_dados_navegador(driver):
    dados = {}
    try:
        # --- TELA 1: DETALHES GERAIS ---
        print("   > Lendo dados da cota...")

        # Nome
        try:
            dados['nome'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Consorciado:')]/following-sibling::td").text
        except:
            dados['nome'] = "-"

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
        print("   > Buscando telefone...")
        try:
            # Tenta clicar na aba 'Telefones'
            # Estratégia: Procura qualquer elemento que contenha o texto 'Telefones' e clica
            try:
                # Tenta achar o texto exato na barra de menu (pode ser um TD, SPAN ou A)
                aba_tel = driver.find_element(
                    By.XPATH, "//*[contains(text(), 'Telefones') and not(contains(text(), 'Automático'))]")
                aba_tel.click()
            except:
                # Se falhar, tenta JavaScript para forçar
                driver.execute_script("arguments[0].click();", driver.find_element(
                    By.PARTIAL_LINK_TEXT, "Telefones"))

            time.sleep(2)

            # Pega celular na tabela
            # A tabela tem colunas: DDD | Telefone | ... | Local (Celular)
            # Vamos procurar a linha que tem "Celular" e pegar o 2º TD (Telefone)
            try:
                # XPath: Ache o TD que tem o texto 'Celular', suba para o TR, e pegue o segundo TD
                xpath_tel = "//td[contains(text(), 'Celular')]/parent::tr/td[2]"
                raw_tel = driver.find_element(By.XPATH, xpath_tel).text.strip()

                # Se o numero vier sem DDD, tenta pegar o DDD da coluna anterior (td[1])
                try:
                    ddd = driver.find_element(
                        By.XPATH, "//td[contains(text(), 'Celular')]/parent::tr/td[1]").text.strip()
                    dados['telefone'] = f"{ddd}{raw_tel}"
                except:
                    dados['telefone'] = raw_tel
            except:
                # Plano B: Pega o primeiro numero da tabela
                dados['telefone'] = driver.find_element(
                    By.XPATH, "//table//tr[2]/td[2]").text.strip()

        except Exception as e:
            print(f"   (X) Erro ao buscar telefone: {e}")
            dados['telefone'] = "-"

        return dados
    except Exception as e:
        print(f"Erro na extração: {e}")
        return None


def main():
    try:
        df = pd.read_csv('contratos.csv', dtype=str)
    except:
        print("ERRO: Crie 'contratos.csv'")
        return

    try:
        sheet = conectar_google_sheets()
    except:
        print("ERRO DE PLANILHA.")
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

        try:
            # 1. Navegar e Pesquisar
            navegar_para_busca(driver)

            # Preenche Contrato
            campo = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "NumeroContrato")))
            campo.clear()
            campo.send_keys(contrato)

            # Clica em Localizar
            driver.find_element(
                By.XPATH, "//input[contains(@value, 'Localizar')]").click()
            time.sleep(2)

            # 2. CLICAR NO RESULTADO (Correção Principal)
            # O resultado é uma linha de tabela com 'onclick', não um link normal.
            # Vamos clicar na célula que contém o número do grupo ou qualquer célula com classe 'hand'
            try:
                print("   > Clicando no resultado...")
                # Procura a primeira célula clicável da tabela de resultados
                resultado = driver.find_element(
                    By.XPATH, "//td[@class='hand']/div")
                resultado.click()
                time.sleep(3)  # Tempo extra para carregar a ficha
            except:
                print(
                    "   [!] Não achou lista de resultados (talvez já entrou direto?)")

            # 3. Extrair
            # Mapeamento para Colunas D a O (Indices 4 a 15)
            # D=Data, E=Nome, F=Tel, G=Pagto, H=Origem, I=Lance, J=Total, K=Bolso, L=Obs, M=Cod, N=Grupo, O=Cota
            dados = extrair_dados_navegador(driver)

            if dados:
                linha = encontrar_proxima_linha_vazia(sheet)
                print(f"   > Salvando na linha {linha}...")

                nova_linha = [
                    str(dados.get('data_venda', '')),  # D=Data
                    str(dados.get('nome', '')),        # E=Nome
                    str(dados.get('telefone', '')),    # F=Tel
                    "",                                # G=Pagto
                    str(origem),                       # H=Origem
                    "",                                # I=Lance
                    str(dados.get('credito', '')),     # J=Total
                    "",                                # K=Bolso
                    "",                                # L=Obs
                    str(contrato),                     # M=Cod
                    str(dados.get('grupo', '')),       # N=Grupo
                    str(dados.get('cota', ''))         # O=Cota
                ]

                # CORREÇÃO DO GSPREAD (Aviso Deprecation)
                # Passamos os argumentos nomeados para evitar o erro amarelo
                range_nome = f"D{linha}:O{linha}"
                sheet.update(range_name=range_nome, values=[nova_linha])
                print("   [OK] Sucesso.")
            else:
                print("   [!] Dados não encontrados.")

        except Exception as e:
            print(f"   [ERRO] {e}")

    print("\nFim do processamento.")


if __name__ == "__main__":
    main()
