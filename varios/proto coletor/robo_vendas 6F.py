import time
import pandas as pd
import gspread
import traceback
from oauth2client.service_account import ServiceAccountCredentials
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# --- CONFIGURAÇÕES ---
NOME_PLANILHA_GOOGLE = 'Controle de Vendas -- '
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
        return "0,00"
    try:
        if "R$" in texto:
            return texto.split("R$")[1].strip()
        return texto
    except:
        return texto

# --- FUNÇÕES DE NAVEGAÇÃO SEGURA ---


def ir_para_menu_consorciado(driver):
    """Reseta e clica no menu lateral"""
    print("   > Acessando Menu Consorciado...")
    driver.switch_to.default_content()
    # 1. Entra no frame PAI (mainFrame minúsculo)
    WebDriverWait(driver, 10).until(
        EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))

    # 2. Entra no frame MENU (LeftFrame)
    WebDriverWait(driver, 10).until(
        EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame")))

    # 3. Clica
    driver.find_element(By.LINK_TEXT, "Consorciado").click()
    time.sleep(1)


def ir_para_conteudo_busca(driver):
    """Reseta e foca na tela de preenchimento (lado direito)"""
    driver.switch_to.default_content()
    # 1. Entra no frame PAI
    WebDriverWait(driver, 10).until(
        EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))

    # 2. Entra no frame CONTEÚDO (MainFrame Maiúsculo)
    WebDriverWait(driver, 10).until(
        EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame")))


def extrair_dados_navegador(driver):
    dados = {}
    try:
        # --- TELA 1: DETALHES GERAIS ---
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
            dados['credito'] = "0,00"

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

        # --- TELA 2: TELEFONE ---
        print("   > Buscando telefone...")
        try:
            # Tenta clicar na aba Telefones (procura por texto parcial em qualquer elemento clicável)
            # XPath: Procura qualquer elemento (*) que tenha o texto 'Telefones'
            driver.find_element(
                By.XPATH, "//*[contains(text(), 'Telefones')]").click()
            time.sleep(2)

            # Pega celular na tabela
            # Estratégia: Procura a palavra "Celular" e pega o número na mesma linha
            try:
                # Acha o TD com texto 'Celular', sobe pro TR (linha), pega o 2º TD (coluna do número)
                xpath_tel = "//td[contains(text(), 'Celular')]/parent::tr/td[2]"
                raw_tel = driver.find_element(By.XPATH, xpath_tel).text.strip()

                # Tenta pegar DDD da coluna 1
                try:
                    ddd = driver.find_element(
                        By.XPATH, "//td[contains(text(), 'Celular')]/parent::tr/td[1]").text.strip()
                    dados['telefone'] = f"{ddd}{raw_tel}"
                except:
                    dados['telefone'] = raw_tel
            except:
                # Se falhar, tenta pegar qualquer coisa na segunda linha da tabela principal
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
            # 1. Clicar no Menu Consorciado (Garante que a tela de busca carregue limpa)
            ir_para_menu_consorciado(driver)

            # 2. Ir para o frame da direita para preencher
            ir_para_conteudo_busca(driver)

            # Preenche Contrato
            campo = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "NumeroContrato")))
            campo.clear()
            campo.send_keys(contrato)

            # Clica em Localizar (busca parcial por valor)
            driver.find_element(
                By.XPATH, "//input[contains(@value, 'Localizar')]").click()
            time.sleep(2)

            # 3. CLICAR NO RESULTADO
            # Baseado no seu HTML, a linha (TR) tem o clique. Vamos clicar na DIV dentro dela.
            try:
                print("   > Clicando no resultado...")
                # Procura uma DIV centralizada dentro de uma tabela, típico desse sistema
                # Ou procura pelo texto do contrato na tabela de resultado
                resultado = driver.find_element(
                    By.XPATH, f"//td/div[contains(text(), '{contrato}')] | //td[contains(@class, 'hand')]/div")
                resultado.click()
                time.sleep(3)
            except Exception as e:
                print(
                    "   [!] Não clicou na lista (Se já abriu a cota direto, ignore este aviso).")

            # 4. Extrair
            # Como clicamos, a pagina recarregou. Precisamos refazer o foco no frame.
            ir_para_conteudo_busca(driver)
            dados = extrair_dados_navegador(driver)

            if dados:
                linha = encontrar_proxima_linha_vazia(sheet)
                print(f"   > Salvando na linha {linha}...")

                nova_linha = [
                    str(dados.get('data_venda', '')),
                    str(dados.get('nome', '')),
                    str(dados.get('telefone', '')),
                    "",
                    str(origem),
                    "",
                    str(dados.get('credito', '')),
                    "",
                    "",
                    str(contrato),
                    str(dados.get('grupo', '')),
                    str(dados.get('cota', ''))
                ]

                range_nome = f"D{linha}:O{linha}"
                sheet.update(range_name=range_nome, values=[nova_linha])
                print("   [OK] Sucesso.")
            else:
                print("   [!] Dados não encontrados.")

        except Exception as e:
            print(f"   [ERRO CRÍTICO] Falha ao processar {contrato}.")
            # Isso vai imprimir o erro detalhado no terminal para a gente saber o que foi
            traceback.print_exc()

    print("\nFim do processamento.")


if __name__ == "__main__":
    main()
