"""
Módulo Motor de Navegação e Extração (Web Scraping).

Responsável por inicializar o navegador, interagir com o Document Object
Model (DOM), resolver Captchas via IA (CapSolver) e extrair dados da Autocred.
"""

import os
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# Importa as credenciais e os formatadores do Módulo de Dados
from gerador_dados import (
    USUARIO_LOGIN,
    SENHA_LOGIN,
    MODO_DESKTOP,
    limpar_inteiro,
    limpar_valor
)

# Constante de controle de velocidade do robô
PAUSA_HUMANA = 0.4


# ============================================================================
# INICIALIZAÇÃO DO NAVEGADOR
# ============================================================================
def iniciar_navegador() -> webdriver.Chrome:
    """
    Configura e inicializa a instância do Chrome WebDriver.
    Aplica o modo fantasma (headless) baseando-se na configuração MODO_DESKTOP.
    """
    chrome_options = Options()

    # Só oculta a janela se o MODO_DESKTOP for False no config.txt
    if not MODO_DESKTOP:
        chrome_options.add_argument("--headless=new")

    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")

    mascara = (
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    chrome_options.add_argument(mascara)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options
    )
    return driver


# ============================================================================
# NAVEGAÇÃO E EXTRAÇÃO DE DADOS (SELENIUM)
# ============================================================================
def buscar_contrato(driver: webdriver.Chrome, contrato: str) -> bool:
    """
    Navega pelos menus laterais da intranet e executa a pesquisa pelo contrato.
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
            "//td[contains(@class, 'hand')]/div"
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


def verificar_apenas_pagamento(driver: webdriver.Chrome) -> bool:
    """
    Lê a informação de parcelas pagas no ecrã principal da cota.
    """
    try:
        xpath_pagas = "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td"
        elem = driver.find_element(By.XPATH, xpath_pagas)
        valor_pago = limpar_inteiro(elem.text)
        return valor_pago > 0
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def extrair_dados_completos(driver: webdriver.Chrome) -> dict:
    """
    Raspa todas as informações cadastrais e financeiras do cliente.
    """
    dados = {
        "credito": 0.00, "nome": "-", "telefone": "-",
        "data_venda": "", "grupo": "-", "cota": "-", "pago": False
    }

    try:
        xpath_nome = "//td[contains(text(), 'Consorciado:')]/following-sibling::td"
        dados["nome"] = driver.find_element(By.XPATH, xpath_nome).text
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_credito = "//td[contains(text(), 'Crédito:')]/following-sibling::td"
        texto_cred = driver.find_element(By.XPATH, xpath_credito).text
        dados["credito"] = limpar_valor(texto_cred)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_adesao = "//td[contains(text(), 'Adesão:')]/following-sibling::td"
        dados["data_venda"] = driver.find_element(By.XPATH, xpath_adesao).text
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_grupo = "//td[contains(text(), 'Grupo:')]/following-sibling::td"
        dados["grupo"] = driver.find_element(By.XPATH, xpath_grupo).text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_cota = "//td[contains(text(), 'Cota:')]/following-sibling::td"
        dados["cota"] = driver.find_element(By.XPATH, xpath_cota).text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_pagas = "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td"
        elem = driver.find_element(By.XPATH, xpath_pagas)
        if limpar_inteiro(elem.text) > 0:
            dados["pago"] = True
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        driver.find_element(By.XPATH, "//*[contains(text(), 'Telefones')]").click()
        time.sleep(PAUSA_HUMANA)
        xpath_celular = "//td[contains(text(), 'Celular')]/parent::tr/td[2]"
        elem_celular = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, xpath_celular))
        )
        dados["telefone"] = elem_celular.text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    return dados


def manter_sessao_viva(driver: webdriver.Chrome) -> bool:
    """
    Realiza um clique silencioso no menu para evitar timeout do servidor.
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
        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


# ============================================================================
# INTELIGÊNCIA ARTIFICIAL E AUTENTICAÇÃO
# ============================================================================
def carregar_chave_capsolver() -> str:
    """
    Extrai a chave de API do CapSolver a partir do ficheiro config.txt.
    """
    if os.path.exists("config.txt"):
        with open("config.txt", "r", encoding="utf-8") as f:
            for linha in f:
                if linha.startswith("CAPSOLVER_KEY="):
                    return linha.split("=", 1)[1].strip()
    return ""


def resolver_captcha_api_direta(api_key: str, site_url: str, site_key: str) -> str:
    """
    Conversa diretamente com o servidor da IA para obter o Token de Liberação.
    """
    print("   -> [IA] A enviar o enigma para a CapSolver...")
    payload = {
        "clientKey": api_key,
        "task": {
            "type": "ReCaptchaV2TaskProxyLess",
            "websiteURL": site_url,
            "websiteKey": site_key
        }
    }

    try:
        res = requests.post(
            "https://api.capsolver.com/createTask", json=payload, timeout=10
        ).json()

        if res.get("errorId", 0) > 0:
            print(f"   -> [ERRO IA] A CapSolver recusou: {res.get('errorDescription')}")
            return ""

        task_id = res.get("taskId")
        print(f"   -> [IA] Tarefa aceite! (ID: {task_id}). A aguardar resposta...")

        while True:
            time.sleep(3)
            res_status = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=100,
            ).json()

            status = res_status.get("status")
            if status == "ready":
                print("   -> [SUCESSO IA] Token gerado! Enigma resolvido.")
                return res_status.get("solution").get("gRecaptchaResponse")

            if status == "failed":
                print("   -> [ERRO IA] A inteligência falhou a resolver o desafio.")
                return ""

    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"   -> [ERRO API] Falha na comunicação HTTP com a CapSolver: {e}")
        return ""


def fazer_login_com_ia(driver: webdriver.Chrome) -> bool:
    """
    Fluxo de login puro: insere credenciais, pede Token à IA e injeta no DOM.
    """
    print("\n[PORTARIA] A aceder à página de login da Tradição...")
    url_site = "https://intranet.consorciotradicao.com.br/autocred/"
    driver.get(url_site)

    try:
        try:
            WebDriverWait(driver, 5).until(
                EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
            )
            driver.switch_to.default_content()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        campo_user = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "j_username"))
        )
        campo_user.clear()
        campo_user.send_keys(USUARIO_LOGIN)

        campo_senha = driver.find_element(By.ID, "j_password")
        campo_senha.clear()
        campo_senha.send_keys(SENHA_LOGIN)
        print("   -> Credenciais inseridas. A localizar a fechadura do Captcha...")

        try:
            elemento_captcha = driver.find_element(By.CLASS_NAME, "g-recaptcha")
            site_key = elemento_captcha.get_attribute("data-sitekey")
        except Exception:  # pylint: disable=broad-exception-caught
            print("   -> [AVISO] Captcha não encontrado. A tentar logar direto...")
            site_key = None

        if site_key:
            chave_api = carregar_chave_capsolver()
            token_liberacao = resolver_captcha_api_direta(chave_api, url_site, site_key)

            if token_liberacao:
                script_injecao = (
                    "document.getElementById('g-recaptcha-response').innerHTML = "
                    f"'{token_liberacao}';"
                )
                driver.execute_script(script_injecao)
                print("   -> Token injetado no HTML da página com sucesso!")
            else:
                print("   -> [FALHA] Sem token válido para prosseguir.")
                return False

        time.sleep(1)
        driver.find_element(By.ID, "j_password").send_keys(Keys.ENTER)
        time.sleep(6)

        driver.switch_to.default_content()
        if "login" not in driver.current_url.lower():
            return True
        return False

    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"   -> [ERRO PORTARIA] Sequência de login falhou criticamente: {e}")
        return False
