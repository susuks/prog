"""
Módulo de utilitários para a automação de coleta de dados.
Contém configurações, acesso ao Google Sheets e raspagem com Selenium.
"""

import os
import json
import re
import time
from datetime import datetime
import winsound

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- CONSTANTES GERAIS ---
ARQUIVO_FILA = 'fila_vendas.csv'
ARQUIVO_EM_PROCESSAMENTO = 'temp_processando.csv'
ARQUIVO_HISTORICO_SUCESSO = 'historico_concluidos.csv'
ARQUIVO_PENDENTES = 'pendentes_reanalise.json'
ARQUIVO_CONFIG = 'config.txt'

MAX_TENTATIVAS = 10000
TEMPO_INATIVIDADE_MAXIMO = 300  # 5 Minutos
PAUSA_HUMANA = 0.4  # Segundos de espera após cada clique


def carregar_configuracoes() -> dict:
    """
    Lê o arquivo de configuração e retorna um dicionário com os valores.
    Caso o arquivo não exista ou ocorra um erro, retorna dados padrão ou None.
    
    Returns:
        dict: Dicionário com credenciais e preferências.
    """
    config = {
        "MATRICULA": "",
        "SENHA": "",
        "PREFIXO_PLANILHA": "Controle de Vendas -- ",
        "NOME_ABA": "JANEIRO"
    }

    if not os.path.exists(ARQUIVO_CONFIG):
        return None

    try:
        with open(ARQUIVO_CONFIG, 'r', encoding='utf-8') as f:
            for linha in f:
                if '=' in linha:
                    chave, valor = linha.split('=', 1)
                    valor_limpo = valor.replace('\n', '').replace('\r', '')
                    config[chave.strip()] = valor_limpo
        return config
    except OSError:
        return None


# Carrega as configurações globais ao importar o módulo
CONFIG = carregar_configuracoes()
USUARIO_LOGIN = CONFIG.get("MATRICULA") if CONFIG else ""
SENHA_LOGIN = CONFIG.get("SENHA") if CONFIG else ""
PREFIXO_PLANILHA = CONFIG.get("PREFIXO_PLANILHA") if CONFIG else ""
NOME_ABA = CONFIG.get("NOME_ABA") if CONFIG else ""


def carregar_pendentes() -> dict:
    """
    Carrega o arquivo JSON com os contratos pendentes de reanálise.
    
    Returns:
        dict: Dicionário contendo o estado dos contratos pendentes.
    """
    if os.path.exists(ARQUIVO_PENDENTES):
        try:
            with open(ARQUIVO_PENDENTES, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def salvar_pendentes(dados: dict) -> None:
    """
    Salva o dicionário de contratos pendentes no arquivo JSON.
    
    Args:
        dados (dict): Dicionário atualizado de contratos pendentes.
    """
    with open(ARQUIVO_PENDENTES, 'w', encoding='utf-8') as f:
        json.dump(dados, f, indent=4)


def adicionar_para_reanalise(contrato: str, vendedor_nome: str,
                             vendedor_tel: str, nome_planilha: str,
                             origem: str, dados_completos: dict) -> None:
    """
    Adiciona um contrato que ainda não foi pago ao arquivo de reanálise.
    
    Args:
        contrato (str): Número do contrato.
        vendedor_nome (str): Nome do vendedor associado.
        vendedor_tel (str): Telefone do vendedor para feedback.
        nome_planilha (str): Nome do arquivo do Google Sheets.
        origem (str): Origem da venda (ex: Tráfego).
        dados_completos (dict): Dados brutos provenientes do CSV da fila.
    """
    pendentes = carregar_pendentes()
    pendentes[contrato] = {
        "vendedor_nome": vendedor_nome,
        "vendedor_tel": vendedor_tel,
        "nome_planilha": nome_planilha,
        "origem": origem,
        "dados_originais": dados_completos,
        "tentativas": 0
    }
    salvar_pendentes(pendentes)
    print(f"   [AGENDADO] Contrato {contrato} adicionado à reanálise.")


def salvar_historico_concluido(contrato: str, nome_planilha: str,
                               vendedor_nome: str, vendedor_tel: str,
                               status_pag: str) -> None:
    """
    Grava os dados do contrato processado no histórico de concluídos.
    
    Args:
        contrato (str): Número do contrato processado.
        nome_planilha (str): Nome da planilha de destino.
        vendedor_nome (str): Nome do vendedor.
        vendedor_tel (str): Telefone do vendedor.
        status_pag (str): Status final do pagamento.
    """
    existe = os.path.exists(ARQUIVO_HISTORICO_SUCESSO)
    try:
        with open(ARQUIVO_HISTORICO_SUCESSO, 'a', encoding='utf-8') as f:
            if not existe:
                cabecalho = ("contrato,planilha_destino,data_registro,"
                             "vendedor_nome,vendedor_tel,status_pagamento\n")
                f.write(cabecalho)

            data_hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            safe_planilha = str(nome_planilha).replace(",", ".")
            safe_nome = str(vendedor_nome).replace(",", ".")

            linha = (f"{contrato},{safe_planilha},{data_hora},"
                     f"{safe_nome},{vendedor_tel},{status_pag}\n")
            f.write(linha)
    except OSError as e:
        print(f"   [ERRO HISTÓRICO] {e}")


def conectar_google_sheets(nome_planilha: str):
    """
    Autentica e estabelece conexão com uma planilha específica do Google Sheets.
    
    Args:
        nome_planilha (str): Título exato do documento no Google Drive.
        
    Returns:
        Worksheet|str|None: Objeto da aba se sucesso, 'NAO_ENCONTRADA',
                            ou None para outros erros.
    """
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        'credentials.json', scope
    )
    cliente = gspread.authorize(creds)

    try:
        planilha = cliente.open(nome_planilha)
        return planilha.worksheet(NOME_ABA)
    except gspread.SpreadsheetNotFound:
        return "NAO_ENCONTRADA"
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def encontrar_proxima_linha_vazia(sheet) -> int:
    """
    Itera pela planilha do Google Sheets para achar a próxima linha livre.
    Começa na linha 12.
    
    Args:
        sheet (Worksheet): Objeto da aba do Google Sheets conectada.
        
    Returns:
        int: Número da próxima linha em branco.
    """
    coluna_d = sheet.col_values(4)
    if len(coluna_d) < 12:
        return 12

    for i in range(11, len(coluna_d) + 20):
        try:
            if i >= len(coluna_d) or not coluna_d[i]:
                return i + 1
        except Exception:  # pylint: disable=broad-exception-caught
            return i + 1

    return 12


def encontrar_linha_do_contrato(sheet, contrato: str) -> int:
    """
    Varre a coluna de contratos (M) para localizar a linha de um contrato.
    
    Args:
        sheet (Worksheet): Objeto da aba do Google Sheets.
        contrato (str): Número do contrato para busca.
        
    Returns:
        int|None: Número da linha encontrada ou None se falhar/inexistente.
    """
    try:
        coluna_m = sheet.col_values(13)
        for i, valor in enumerate(coluna_m, start=1):
            if str(contrato).strip() == str(valor).strip():
                return i
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"   [ERRO BUSCA PLANILHA] {e}")

    return None


def limpar_inteiro(texto: str) -> int:
    """
    Extrai todos os dígitos de uma string e retorna como inteiro.
    
    Args:
        texto (str): Texto contendo números (ex: '001').
        
    Returns:
        int: O número formatado como inteiro (ex: 1).
    """
    try:
        numeros = re.sub(r'\D', '', str(texto))
        return int(numeros) if numeros else 0
    except (ValueError, TypeError):
        return 0


def limpar_valor(texto: str) -> float:
    """
    Converte uma string de valor monetário (R$ 1.500,00) em formato float.
    
    Args:
        texto (str): String com formato de moeda.
        
    Returns:
        float: Valor numérico limpo e computável.
    """
    try:
        match = re.search(r'([\d\.]+,\d{2})', str(texto))
        if match:
            valor_texto = match.group(1).replace('.', '').replace(',', '.')
            return float(valor_texto)
        return 0.00
    except (ValueError, TypeError):
        return 0.00


def fazer_login_automatico(driver) -> bool:
    """
    Acessa a intranet e submete os dados de login.
    Fica em espera aguardando resolução manual do Captcha.
    
    Args:
        driver (WebDriver): Instância do navegador Selenium.
        
    Returns:
        bool: True se logou com sucesso, False em caso de falha.
    """
    if not USUARIO_LOGIN or not SENHA_LOGIN:
        return False

    try:
        driver.get("https://intranet.consorciotradicao.com.br/autocred/")
        try:
            WebDriverWait(driver, 3).until(
                EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
            )
            driver.switch_to.default_content()
            return True
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "j_username"))
        )
        driver.find_element(By.ID, "j_username").send_keys(USUARIO_LOGIN)
        driver.find_element(By.ID, "j_password").send_keys(SENHA_LOGIN)

        print("\n" + "=" * 60)
        print(" AGUARDANDO CAPTCHA... POR FAVOR RESOLVA!")
        print("=" * 60 + "\n")

        WebDriverWait(driver, 600).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        driver.switch_to.default_content()

        print("\n LOGIN DETECTADO!")
        try:
            winsound.Beep(1000, 500)
        except RuntimeError:
            pass

        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def buscar_contrato(driver, contrato: str) -> bool:
    """
    Navega pelos menus laterais da intranet e executa a pesquisa pelo contrato.
    
    Args:
        driver (WebDriver): Instância ativa do navegador.
        contrato (str): O contrato de 8 dígitos a ser localizado.
        
    Returns:
        bool: True se acessou a tela do consorciado com sucesso, False em erro.
    """
    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame"))
        )
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
        time.sleep(PAUSA_HUMANA)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame"))
        )

        campo = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.NAME, "NumeroContrato"))
        )
        campo.clear()
        campo.send_keys(contrato)

        btn_localizar = driver.find_element(
            By.XPATH, "//input[contains(@value, 'Localizar')]"
        )
        btn_localizar.click()
        time.sleep(PAUSA_HUMANA)

        xpath_resultado = (
            f"//td/div[contains(text(), '{contrato}')] | "
            f"//td[contains(@class, 'hand')]/div"
        )
        resultado = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, xpath_resultado))
        )
        resultado.click()
        time.sleep(PAUSA_HUMANA)

        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame"))
        )

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.XPATH, "//td[contains(text(), 'Consorciado:')]")
            )
        )
        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def verificar_apenas_pagamento(driver) -> bool:
    """
    Lê a informação de parcelas na tela principal sem navegação adicional.
    Útil para verificações rápidas no modo turbo.
    
    Args:
        driver (WebDriver): Navegador na tela de dados do contrato.
        
    Returns:
        bool: True se parcela paga for maior que zero.
    """
    try:
        xpath_pagas = "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td"
        elem = driver.find_element(By.XPATH, xpath_pagas)
        valor_pago = limpar_inteiro(elem.text)
        return valor_pago > 0
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def extrair_dados_completos(driver) -> dict:
    """
    Raspa as informações cadastrais completas do cliente nas telas.
    
    Args:
        driver (WebDriver): Navegador na aba Cota inicial.
        
    Returns:
        dict: Dicionário contendo nome, crédito, venda, grupo, cota e telefone.
    """
    dados = {
        'credito': 0.00,
        'nome': '-',
        'telefone': '-',
        'data_venda': '',
        'grupo': '-',
        'cota': '-',
        'pago': False
    }

    try:
        xpath_nome = "//td[contains(text(), 'Consorciado:')]/following-sibling::td"
        dados['nome'] = driver.find_element(By.XPATH, xpath_nome).text
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_credito = "//td[contains(text(), 'Crédito:')]/following-sibling::td"
        texto_cred = driver.find_element(By.XPATH, xpath_credito).text
        dados['credito'] = limpar_valor(texto_cred)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_adesao = "//td[contains(text(), 'Adesão:')]/following-sibling::td"
        dados['data_venda'] = driver.find_element(By.XPATH, xpath_adesao).text
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_grupo = "//td[contains(text(), 'Grupo:')]/following-sibling::td"
        dados['grupo'] = driver.find_element(By.XPATH, xpath_grupo).text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_cota = "//td[contains(text(), 'Cota:')]/following-sibling::td"
        dados['cota'] = driver.find_element(By.XPATH, xpath_cota).text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_pagas = "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td"
        elem = driver.find_element(By.XPATH, xpath_pagas)
        valor_pago = limpar_inteiro(elem.text)
        if valor_pago > 0:
            dados['pago'] = True
    except Exception:  # pylint: disable=broad-exception-caught
        dados['pago'] = False

    try:
        driver.find_element(By.XPATH, "//*[contains(text(), 'Telefones')]").click()
        time.sleep(PAUSA_HUMANA)

        xpath_celular = "//td[contains(text(), 'Celular')]/parent::tr/td[2]"
        elem_celular = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, xpath_celular))
        )
        dados['telefone'] = elem_celular.text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    return dados


def atualizar_planilha(sheet, row_csv: dict,
                       dados_site: dict, contrato: str) -> str:
    """
    Estrutura a string de atualização e envia para o Google Sheets.
    
    Args:
        sheet (Worksheet): Instância da aba da planilha.
        row_csv (dict|Series): Linha original capturada da Fila/Monitor.
        dados_site (dict): Dicionário com dados raspados da intranet.
        contrato (str): Número do contrato para registro.
        
    Returns:
        str: Feedback de status final ("1º Parcela Paga" ou string vazia).
    """
    linha = encontrar_proxima_linha_vazia(sheet)
    status_pag = "1º Parcela Paga" if dados_site['pago'] else ""

    p1 = [
        str(dados_site['data_venda']),
        str(dados_site['nome']),
        str(dados_site['telefone']),
        "",
        str(row_csv.get('origem', ''))
    ]

    lance_val = 0.00
    try:
        str_lance = str(row_csv.get('lance livre', 0))
        val_limpo = str_lance.replace("R$", "").replace(".", "")\
                             .replace(",", ".").strip()
        lance_val = float(val_limpo)
    except (ValueError, TypeError):
        pass

    p2 = [
        dados_site['credito'],
        lance_val,
        "",
        str(contrato),
        str(dados_site['grupo']),
        str(dados_site['cota'])
    ]

    sheet.update_cell(linha, 2, status_pag)
    sheet.update(range_name=f"D{linha}:H{linha}", values=[p1],
                 value_input_option='USER_ENTERED')
    sheet.update(range_name=f"J{linha}:O{linha}", values=[p2],
                 value_input_option='USER_ENTERED')

    return status_pag


def manter_sessao_viva(driver) -> bool:
    """
    Realiza um clique silencioso no menu para evitar timeout do servidor.
    
    Args:
        driver (WebDriver): Navegador em execução.
        
    Returns:
        bool: True se conseguiu renovar o tempo, False se perdeu o login.
    """
    try:
        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame"))
        )
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
        time.sleep(PAUSA_HUMANA)

        hora_atual = datetime.now().strftime('%H:%M:%S')
        print(f"[{hora_atual}] Keep-Alive: Sessão renovada.")
        return True

    # CORREÇÃO PYLINT: Informando que esta exceção é intencional
    except Exception:  # pylint: disable=broad-exception-caught
        return False
