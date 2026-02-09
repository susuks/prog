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
    # Pega todos os valores da coluna D (Data)
    # A lógica: Se a coluna D tiver texto, a linha tá ocupada.
    coluna_d = sheet.col_values(4)  # Coluna 4 = D

    # Precisamos começar a checar a partir da linha 12
    # O array em python começa do 0, então linha 12 é índice 11.
    linha_atual = 12

    # Se a planilha tiver menos de 12 linhas preenchidas, começamos da 12
    if len(coluna_d) < 12:
        return 12

    # Verifica linha por linha a partir da 12
    # O loop vai até o fim dos dados existentes + 1 buffer
    for i in range(11, len(coluna_d) + 10):
        try:
            valor = coluna_d[i]
            if valor == "" or valor is None:
                return i + 1  # Retorna o número da linha real (índice + 1)
        except IndexError:
            return i + 1  # Se deu erro de índice, é porque acabou a planilha, então é aqui mesmo

    return linha_atual


def limpar_valor_moeda(texto):
    # Transforma "R$ 64.000,00" em "64000,00"
    if not texto:
        return ""
    texto_limpo = texto.replace("R$", "").replace(" ", "").strip()
    return texto_limpo


def extrair_dados_navegador(driver):
    dados = {}

    try:
        # Focar no Frame Principal
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame("MainFrame")
        except:
            pass

        # --- TELA 1: DETALHES GERAIS ---
        print("   Lendo dados principais...")

        # 1. Nome Cliente
        try:
            dados['nome'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Consorciado')]/following-sibling::td//span").text
        except:
            dados['nome'] = "Não encontrado"

        # 2. Crédito (Valor Total)
        try:
            raw_credito = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Crédito')]/following-sibling::td//span").text
            dados['credito'] = limpar_valor_moeda(raw_credito)
        except:
            dados['credito'] = "0,00"

        # 3. Data Venda / Adesão
        # Tenta procurar por 'Adesão' ou 'Data Venda'
        try:
            dados['data_venda'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Adesão') or contains(text(), 'Data Venda')]/following-sibling::td//span").text
        except:
            # Se falhar, tenta pegar Data Cadastro
            try:
                dados['data_venda'] = driver.find_element(
                    By.XPATH, "//td[contains(text(), 'Cadastro')]/following-sibling::td//span").text
            except:
                dados['data_venda'] = ""

        # 4. Grupo e Cota
        # O site geralmente mostra "Grupo: XXXX" e "Cota: YYY" em campos separados ou juntos
        try:
            # Tentativa de pegar campos individuais
            grp = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Grupo')]/following-sibling::td//span").text
            cot = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Cota')]/following-sibling::td//span").text
            dados['grupo'] = grp
            dados['cota'] = cot
        except:
            dados['grupo'] = "Verif."
            dados['cota'] = "Verif."

        # --- TELA 2: PEGAR TELEFONE (NA OUTRA ABA) ---
        print("   Buscando telefone...")
        try:
            # Clica no link que diz "Telefones" ou "Dados Cadastrais"
            # O link_text deve ser exato. Se falhar, verifique no site o nome exato da aba.
            try:
                driver.find_element(By.PARTIAL_LINK_TEXT, "Telefones").click()
            except:
                driver.find_element(By.PARTIAL_LINK_TEXT, "Cadastrais").click()

            time.sleep(2)  # Espera carregar a aba

            # Tenta pegar o celular
            # XPath genérico procurando onde tem a palavra Celular e pegando o próximo valor
            dados['telefone'] = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Celular') or contains(text(), 'Fone')]/following-sibling::td//span").text

            # IMPORTANTE: Voltar para a tela anterior se necessário ou garantir que a próxima busca reinicie do zero
            # Como a função main recarrega a URL de busca, não precisamos clicar em "Voltar" aqui.

        except Exception as e:
            print(f"   (X) Não consegui pegar telefone: {e}")
            dados['telefone'] = ""

        return dados

    except Exception as e:
        print(f"Erro crítico na extração: {e}")
        return None


def main():
    # 1. Ler arquivo de contratos (CSV)
    # Formato do CSV: contrato, origem
    try:
        df = pd.read_csv('contratos.csv', dtype=str)
    except:
        print("ERRO: Crie o arquivo 'contratos.csv' com as colunas: contrato,origem")
        return

    # 2. Conectar Planilha
    print("Conectando ao Google Sheets...")
    sheet = conectar_google_sheets()

    # 3. Abrir Navegador
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    driver.get("https://intranet.consorciotradicao.com.br/autocred/")

    # 4. LOGIN MANUAL
    print("\n" + "="*60)
    print(" ATENÇÃO: FAÇA O LOGIN E RESOLVA O CAPTCHA AGORA.")
    input(" Pressione [ENTER] aqui no terminal quando estiver logado...")
    print("="*60 + "\n")

    # 5. Processar lista
    for index, row in df.iterrows():
        contrato = row['contrato']
        origem = row['origem']

        print(f"Processando contrato {contrato}...")

        # Vai para a busca
        driver.switch_to.default_content()
        try:
            driver.switch_to.frame("MainFrame")
        except:
            pass

        driver.get(
            "https://intranet.consorciotradicao.com.br/autocred/Attendance/searchCota.asp")

        # Preenche e Pesquisa
        try:
            WebDriverWait(driver, 10).until(EC.presence_of_element_located(
                (By.NAME, "NumeroContrato"))).send_keys(contrato)
            driver.find_element(
                By.XPATH, "//input[@value='Localizar' or @type='submit']").click()
            time.sleep(2)

            # Extrai
            dados = extrair_dados_navegador(driver)

            if dados:
                # Encontrar onde escrever
                linha = encontrar_proxima_linha_vazia(sheet)
                print(f"   Escrevendo na linha {linha}...")

                # Mapeamento exato das colunas (D até O)
                # D=Data, E=Nome, F=Tel, G=Pagto, H=Origem, I=Lance, J=Total, K=Bolso, L=Obs, M=Cod, N=Grupo, O=Cota

                # Prepara a lista de valores (célula por célula para garantir posição)
                # update_acell é mais lento, mas mais seguro para iniciante. Vamos usar batch update para a linha.

                valores_para_salvar = [
                    [
                        dados.get('data_venda', ''),  # D: Data
                        dados.get('nome', ''),       # E: Nome
                        dados.get('telefone', ''),   # F: Telefone
                        "",                          # G: Pagamento (Vazio)
                        origem,                      # H: Origem (do CSV)
                        "",                          # I: Lance (Vazio)
                        dados.get('credito', ''),    # J: Valor Total (Crédito)
                        "",                          # K: Lance Bolso (Vazio)
                        "",                          # L: Obs (Vazio)
                        contrato,                    # M: Código
                        dados.get('grupo', ''),      # N: Grupo
                        dados.get('cota', '')        # O: Cota
                    ]
                ]

                # Escreve de D{linha} até O{linha}
                range_escrita = f"D{linha}:O{linha}"
                sheet.update(range_escrita, valores_para_salvar)
                print("   [OK] Salvo com sucesso.")

            else:
                print("   [ERRO] Dados não retornados.")

        except Exception as e:
            print(f"   [ERRO] Falha ao buscar contrato: {e}")

    print("\nFim do processamento.")
    driver.quit()


if __name__ == "__main__":
    main()
