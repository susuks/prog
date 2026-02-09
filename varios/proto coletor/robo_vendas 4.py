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
        # Pega o que está depois de "R$"
        if "R$" in texto:
            valor = texto.split("R$")[1].strip()
            return valor
        return texto
    except:
        return texto


def navegar_para_busca(driver):
    """Função robusta para lidar com os frames aninhados"""
    driver.switch_to.default_content()

    # 1. Entra no Frame "Pai" (mainFrame minúsculo)
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame")))
    except:
        print("   [!] Não achou frame pai 'mainFrame'. Tentando seguir...")

    # 2. Tenta clicar no Menu (LeftFrame) para garantir que a tela carregue
    try:
        # Guarda o contexto atual
        driver.switch_to.frame("LeftFrame")
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
        driver.switch_to.parent_frame()  # Volta para o pai (mainFrame)
        time.sleep(1)
    except:
        pass  # Se não der pra clicar no menu, tenta ir direto pro conteúdo

    # 3. Entra no Frame de Conteúdo (MainFrame Maiúsculo)
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame")))
    except:
        print("   [!] Erro crítico: Não conseguiu entrar no MainFrame de conteúdo.")


def extrair_dados_navegador(driver):
    dados = {}
    try:
        # O driver JÁ DEVE estar no MainFrame aqui.

        # --- TELA 1: DETALHES GERAIS ---
        print("   > Lendo dados...")

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

        # Grupo/Cota (Topo da página)
        try:
            # Tenta pegar pelo ID ou posição, já que no print aparecem no topo
            # Ajuste baseado no print Screenshot_1.png
            texto_topo = driver.find_element(
                By.XPATH, "//td[contains(text(), 'Grupo:') and contains(text(), 'Cota:')]").text
            # Se for tudo numa linha só, tenta separar. Se forem TDs separados:
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
            driver.find_element(By.PARTIAL_LINK_TEXT, "Telefones").click()
            time.sleep(2)

            # Pega celular na tabela (image_986f6e.png)
            try:
                # Procura a linha que tem 'Celular' e pega a coluna 2 (Telefone)
                # Pega o TD anterior ao TD que tem 'Celular'
                xpath_tel = "//td[contains(text(), 'Celular')]/preceding-sibling::td[1]"
                # OU se a estrutura for: TD(Tel) | TD | TD(Local=Celular)
                # Vamos tentar pegar o primeiro número que parece celular
                dados['telefone'] = driver.find_element(
                    By.XPATH, "//td[contains(text(), 'Celular')]/../td[2]").text.strip()
            except:
                # Tentativa genérica: pega o primeiro valor da tabela de telefones
                try:
                    dados['telefone'] = driver.find_element(
                        By.XPATH, "//tr[@class='label' or contains(@style, 'background')]/following-sibling::tr/td[2]").text
                except:
                    dados['telefone'] = "Não achou"

        except Exception as e:
            print(f"   (X) Erro ao buscar telefone: {e}")
            dados['telefone'] = ""

        return dados
    except Exception as e:
        print(f"Erro na extração: {e}")
        return None


def main():
    try:
        df = pd.read_csv('contratos.csv', dtype=str)
    except:
        print("ERRO: Crie 'contratos.csv' com colunas: contrato,origem")
        return

    try:
        sheet = conectar_google_sheets()
    except:
        print("ERRO DE PLANILHA. Verifique credentials.json e o nome da planilha.")
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
            # 1. Navegar até a busca (Resetando frames)
            navegar_para_busca(driver)

            # 2. Preencher Contrato
            # Espera o campo aparecer (agora estamos no frame certo)
            campo = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "NumeroContrato")))
            campo.clear()
            campo.send_keys(contrato)

            # 3. Clicar em Localizar
            # XPath corrigido para achar o botão mesmo com espaços no value
            btn = driver.find_element(
                By.XPATH, "//input[contains(@value, 'Localizar')]")
            btn.click()
            time.sleep(2)

            # 4. CLICAR NO RESULTADO (Se aparecer lista)
            # Verifica se apareceu a lista de resultados e clica no primeiro link
            try:
                # Se achou um link dentro de uma tabela, clica
                link_resultado = driver.find_element(
                    By.XPATH, "//a[contains(@href, 'Codigo_Grupo')]")
                print("   > Selecionando contrato na lista...")
                link_resultado.click()
                time.sleep(2)
            except:
                # Se não achou lista, assume que já entrou (às vezes acontece se só tem 1)
                pass

            # 5. Extrair
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

                range_nome = f"D{linha}:O{linha}"
                sheet.update(range_nome, [nova_linha])
                print("   [OK] Sucesso.")
            else:
                print(
                    "   [!] Dados não encontrados (verifique se o contrato existe).")

        except Exception as e:
            print(f"   [ERRO] Falha ao processar {contrato}: {e}")
            # Tira um print do erro pra ajudar a debugar se precisar
            # driver.save_screenshot(f"erro_{contrato}.png")

    print("\nFim do processamento.")


if __name__ == "__main__":
    main()
