"""
Módulo de Utilitários e Serviços para Automação de Consórcio.

Este módulo centraliza todas as interações de baixo nível com a API do 
Google Sheets e o driver do Selenium, permitindo que a lógica de negócio
seja mantida de forma limpa no arquivo principal.

As funções seguem o padrão de documentação do Google e respeitam as
normas de estilo PEP 8, isolando exceções e garantindo a continuidade do robô.
"""

import os
import json
import re
import time
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- CONFIGURAÇÕES E CONSTANTES GLOBAIS ---
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
    Lê o arquivo de configuração local e extrai credenciais e parâmetros.

    O arquivo deve seguir o formato 'CHAVE=VALOR'. Caso o arquivo não
    exista, valores padrão de segurança serão retornados para evitar
    falhas de importação no módulo principal.

    Returns:
        dict: Mapeamento de configurações como MATRICULA, SENHA, e nomes de abas
              (incluindo a aba da planilha GERAL).
    """
    config = {
        "MATRICULA": "",
        "SENHA": "",
        "PREFIXO_PLANILHA": "Controle de Vendas -- ",
        "NOME_ABA": "JANEIRO",
        "ANO_GERAL": "2026"
    }

    if not os.path.exists(ARQUIVO_CONFIG):
        return config

    try:
        with open(ARQUIVO_CONFIG, 'r', encoding='utf-8') as f:
            for linha in f:
                if '=' in linha:
                    chave, valor = linha.split('=', 1)
                    # CORREÇÃO: Remove apenas quebra de linha, mantendo o espaço final intacto
                    valor_limpo = valor.replace('\n', '').replace('\r', '')
                    config[chave.strip()] = valor_limpo
        return config
    except OSError:
        return config


# Inicialização das constantes de ambiente e credenciais
CONFIG = carregar_configuracoes()
USUARIO_LOGIN = CONFIG.get("MATRICULA")
SENHA_LOGIN = CONFIG.get("SENHA")
PREFIXO_PLANILHA = CONFIG.get("PREFIXO_PLANILHA")
NOME_ABA = CONFIG.get("NOME_ABA")
NOME_ABA_GERAL = CONFIG.get("ANO_GERAL")


def carregar_pendentes() -> dict:
    """
    Carrega o arquivo JSON com os contratos que aguardam pagamento.
    
    Returns:
        dict: Dicionário contendo o estado atual dos contratos pendentes.
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
    Persiste o dicionário de contratos pendentes no arquivo JSON.
    
    Args:
        dados (dict): Dicionário atualizado de contratos pendentes.
    """
    with open(ARQUIVO_PENDENTES, 'w', encoding='utf-8') as f:
        json.dump(dados, f, indent=4)


def adicionar_para_reanalise(contrato: str, vendedor_nome: str,
                             vendedor_tel: str, nome_planilha: str,
                             origem: str, dados_completos: dict) -> None:
    """
    Adiciona um contrato que ainda não foi pago à fila de monitoramento contínuo.
    
    Args:
        contrato (str): Número do contrato (ex: 22134896).
        vendedor_nome (str): Nome do vendedor associado.
        vendedor_tel (str): Telefone do vendedor para envio de feedback.
        nome_planilha (str): Nome do arquivo individual do Google Sheets.
        origem (str): Origem da venda.
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
    Grava os dados do contrato definitivamente processado no histórico local (CSV).
    
    Args:
        contrato (str): Número do contrato processado.
        nome_planilha (str): Nome da planilha(s) de destino.
        vendedor_nome (str): Nome do vendedor.
        vendedor_tel (str): Telefone do vendedor.
        status_pag (str): Status final do pagamento detectado.
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


def conectar_google_sheets(nome_planilha: str, aba: str):
    """
    Estabelece uma conexão autenticada com uma planilha e aba específica.

    Utiliza as credenciais do arquivo 'credentials.json' para acessar a API
    do Google Drive e Planilhas.

    Args:
        nome_planilha (str): O título exato da planilha no Google Drive.
        aba (str): O nome da aba (worksheet) desejada.

    Returns:
        gspread.models.Worksheet: Objeto para manipulação da aba.
        None: Retornado em caso de falha na conexão ou se a planilha não existir.
    """
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            'credentials.json', scope
        )
        cliente = gspread.authorize(creds)
        return cliente.open(nome_planilha).worksheet(aba)
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def encontrar_proxima_linha_vazia(sheet, start_row: int, check_col: int) -> int:
    """
    Analisa a planilha para encontrar a primeira linha disponível para escrita.

    A função varre uma coluna chave (ex: coluna D para vendedor, C para Geral)
    para determinar onde os novos dados devem ser inseridos sem sobrescrever
    os registros já existentes.

    Args:
        sheet (gspread.models.Worksheet): A aba ativa do Google Sheets.
        start_row (int): A linha onde a tabela começa (12 para Vendedor, 7 para Geral).
        check_col (int): O índice da coluna base para verificar se a linha está vazia.

    Returns:
        int: O índice da próxima linha em branco encontrada.
    """
    coluna_alvo = sheet.col_values(check_col)

    # Se a coluna estiver menor que a linha de início, o início está livre
    if len(coluna_alvo) < start_row:
        return start_row

    for i in range(start_row - 1, len(coluna_alvo) + 20):
        try:
            if i >= len(coluna_alvo) or not coluna_alvo[i]:
                return i + 1
        except Exception:  # pylint: disable=broad-exception-caught
            return i + 1

    return start_row


def encontrar_linha_do_contrato(sheet, contrato: str, col_idx: int) -> int:
    """
    Localiza o índice da linha de um contrato específico para atualizações (Reanálise).

    Args:
        sheet (gspread.models.Worksheet): A aba ativa.
        contrato (str): O número do contrato a ser buscado.
        col_idx (int): O índice da coluna onde o contrato reside (13 para Vendedor, 12 para Geral).

    Returns:
        int: O índice numérico da linha encontrada.
        None: Se o contrato não for localizado na planilha.
    """
    try:
        valores = sheet.col_values(col_idx)
        for i, valor in enumerate(valores, start=1):
            if str(contrato).strip() == str(valor).strip():
                return i
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"   [ERRO BUSCA PLANILHA] {e}")

    return None


def limpar_inteiro(texto: str) -> int:
    """
    Extrai todos os dígitos de uma string e retorna como inteiro.
    
    Args:
        texto (str): Texto contendo números (ex: '001', '1 parcela').
        
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
    Converte uma string de valor monetário (ex: R$ 1.500,00) em formato float.
    
    Args:
        texto (str): String com formato de moeda padrão Brasil.
        
    Returns:
        float: Valor numérico limpo e computável para planilhas.
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
    Acessa a intranet da Tradição e submete os dados de login.
    Fica em estado de espera aguardando a resolução manual do Captcha.
    
    Args:
        driver (WebDriver): Instância do navegador Selenium.
        
    Returns:
        bool: True se logou com sucesso, False em caso de falha de credenciais.
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
        print(" AGUARDANDO CAPTCHA... POR FAVOR RESOLVA NO NAVEGADOR!")
        print("=" * 60 + "\n")

        WebDriverWait(driver, 600).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        driver.switch_to.default_content()

        print("\n LOGIN DETECTADO COM SUCESSO!")

        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def buscar_contrato(driver, contrato: str) -> bool:
    """
    Navega pelos menus laterais da intranet e executa a pesquisa pelo contrato.
    Implementa "Pausa Humana" para evitar bloqueios por excesso de velocidade.
    
    Args:
        driver (WebDriver): Instância ativa do navegador.
        contrato (str): O contrato de 8 dígitos a ser localizado.
        
    Returns:
        bool: True se acessou a ficha do consorciado com sucesso, False em erro.
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
    Lê a informação de parcelas pagas na tela principal da cota.
    Evita navegação extra, otimizando o ciclo de reanálise contínua.
    
    Args:
        driver (WebDriver): Navegador estacionado na tela de dados do contrato.
        
    Returns:
        bool: True se o número de parcelas pagas for maior que zero.
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
    Raspa todas as informações cadastrais e financeiras do cliente.
    Navega até a aba 'Telefones' para coletar o número de contato do cliente.
    
    Args:
        driver (WebDriver): Navegador na aba inicial da Cota.
        
    Returns:
        dict: Dicionário completo com chaves (nome, credito, telefone, etc).
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


def atualizar_planilha_vendedor(sheet, row_csv: dict,
                                dados_site: dict, contrato: str) -> str:
    """
    Registra ou atualiza os dados na planilha padrão do vendedor.
    Padrão de Início: Coluna B, Linha 12.
    
    Args:
        sheet (Worksheet): Instância da aba da planilha do vendedor.
        row_csv (dict): Dados originados do WhatsApp/Fila.
        dados_site (dict): Dicionário com dados raspados da intranet.
        contrato (str): Número do contrato para registro.
        
    Returns:
        str: Feedback de status final ("1º Parcela Paga" ou string vazia).
    """
    linha = encontrar_proxima_linha_vazia(sheet, start_row=12, check_col=4)
    status_pag = "1º Parcela Paga" if dados_site['pago'] else ""

    dados_cadastrais = [
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

    dados_financeiros = [
        dados_site['credito'],
        lance_val,
        "",
        str(contrato),
        str(dados_site['grupo']),
        str(dados_site['cota'])
    ]

    # Atualização por ranges para otimizar tempo de API
    sheet.update_cell(linha, 2, status_pag)
    sheet.update(range_name=f"D{linha}:H{linha}", values=[dados_cadastrais],
                 value_input_option='USER_ENTERED')
    sheet.update(range_name=f"J{linha}:O{linha}", values=[dados_financeiros],
                 value_input_option='USER_ENTERED')

    return status_pag


def atualizar_planilha_geral(sheet, row_csv: dict,
                             dados_site: dict, contrato: str) -> str:
    """
    Registra ou atualiza os dados na planilha GERAL (Centralizada).
    Padrão de Início: Coluna A, Linha 7.
    Todas as colunas são deslocadas 1 casa para a esquerda em relação à individual.
    
    Args:
        sheet (Worksheet): Instância da aba da planilha Geral (ex: Ano 2026).
        row_csv (dict): Dados originados do WhatsApp/Fila.
        dados_site (dict): Dicionário com dados raspados da intranet.
        contrato (str): Número do contrato para registro.
        
    Returns:
        str: Feedback de status final ("1º Parcela Paga" ou string vazia).
    """
    linha = encontrar_proxima_linha_vazia(sheet, start_row=7, check_col=3)
    status_pag = "1º Parcela Paga" if dados_site['pago'] else ""

    dados_cadastrais = [
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

    dados_financeiros = [
        dados_site['credito'],
        lance_val,
        "",
        str(contrato),
        str(dados_site['grupo']),
        str(dados_site['cota'])
    ]

    # Atualização com ranges deslocados (-1 Coluna base)
    sheet.update_cell(linha, 1, status_pag)
    sheet.update(range_name=f"C{linha}:G{linha}", values=[dados_cadastrais],
                 value_input_option='USER_ENTERED')
    sheet.update(range_name=f"I{linha}:N{linha}", values=[dados_financeiros],
                 value_input_option='USER_ENTERED')

    return status_pag


def manter_sessao_viva(driver) -> bool:
    """
    Realiza um clique silencioso no menu para evitar timeout do servidor.
    Dessa forma, o robô pode ficar inativo na madrugada sem precisar
    realizar todo o fluxo de login + captcha de manhã.
    
    Args:
        driver (WebDriver): Navegador em execução.
        
    Returns:
        bool: True se conseguiu renovar o tempo, False se perdeu a conexão/login.
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
        print(f"[{hora_atual}] Keep-Alive: Sessão renovada com sucesso.")
        return True

    except Exception:  # pylint: disable=broad-exception-caught
        return False
