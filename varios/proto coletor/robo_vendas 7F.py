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

# --- CONFIGURAÇÕES FIXAS ---
PREFIXO_PLANILHA = 'Controle de Vendas -- '  # Parte fixa do nome
NOME_ABA = 'JANEIRO'


def conectar_google_sheets(nome_planilha):
    """Conecta em uma planilha específica baseada no nome"""
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
        print(
            f"   [ERRO CRÍTICO] Planilha '{nome_planilha}' não encontrada no Google Drive!")
        print(
            "   Verifique se o nome está exato e se foi compartilhada com o email do robô.")
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
    """Converte 'R$ 64.000,00' para float 64000.00 para manter formatação na planilha"""
    if not texto:
        return 0.00
    try:
        texto_limpo = texto.replace("R$", "").strip()
        # Remove pontos de milhar e troca vírgula por ponto decimal
        texto_limpo = texto_limpo.replace(".", "").replace(",", ".")
        return float(texto_limpo)
    except:
        return texto  # Se der erro, devolve o texto original


def formatar_lance(valor):
    """Formata o lance livre do CSV para número"""
    if pd.isna(valor) or valor == "":
        return ""
    try:
        # Tenta converter para float se for string numérica
        valor_str = str(valor).replace(",", ".")
        return float(valor_str)
    except:
        return valor

# --- FUNÇÕES DE NAVEGAÇÃO ---


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
    # 1. Ler CSV
    try:
        # Lê o CSV garantindo que lance_livre seja lido
        df = pd.read_csv('contratos.csv', dtype=str)
        # Limpa espaços em branco nos nomes das colunas
        df.columns = df.columns.str.strip()
    except:
        print("ERRO: Arquivo 'contratos.csv' não encontrado ou formato inválido.")
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
        vendedor = row['vendedor']  # Coluna nova
        lance_livre = row['lance_livre']  # Coluna nova

        if pd.isna(contrato) or contrato == "":
            continue

        # Constrói o nome da planilha: "Controle de Vendas -- " + "KAIO"
        nome_planilha_alvo = f"{PREFIXO_PLANILHA}{vendedor.strip()}"

        print(f"Processando: Contrato {contrato} | Vendedor: {vendedor}")

        # Conecta na planilha Específica do Vendedor
        sheet = conectar_google_sheets(nome_planilha_alvo)
        if not sheet:
            print(
                f"   [PULANDO] Não foi possível acessar a planilha de {vendedor}.")
            continue

        try:
            # Navegação no Site
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

                # --- PARTE 1: Colunas D até H (Antes da coluna I) ---
                # D=Data, E=Nome, F=Tel, G=Pagto(Vazio), H=Origem
                lista_parte1 = [
                    str(dados.get('data_venda', '')),
                    str(dados.get('nome', '')),
                    str(dados.get('telefone', '')),
                    "",
                    str(origem)
                ]

                # --- PARTE 2: Colunas J até O (Depois da coluna I) ---
                # J=ValorTotal, K=LanceBolso(do CSV), L=Obs, M=Cod, N=Grupo, O=Cota
                # Nota: dados['credito'] agora é um float, o Sheets vai formatar como moeda
                lista_parte2 = [
                    dados.get('credito', 0.00),         # J: Valor Total
                    # K: Lance do Bolso (Vem do CSV)
                    formatar_lance(lance_livre),
                    "",                                 # L: Obs
                    str(contrato),                      # M: Código
                    str(dados.get('grupo', '')),        # N: Grupo
                    str(dados.get('cota', ''))          # O: Cota
                ]

                # Atualiza em dois comandos separados para PULAR a coluna I
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
