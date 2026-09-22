"""
Motor de Navegação Web (Selenium).

Responsável por gerenciar as instâncias do navegador, transpor barreiras
de login e reCaptcha (CapSolver) e extrair os dados cadastrais diretamente do DOM.
"""

import os
import time
import logging

import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

from gerador_dados import (
    USUARIO_LOGIN,
    SENHA_LOGIN,
    MODO_DESKTOP,
    limpar_inteiro,
    limpar_valor,
)

logger = logging.getLogger("EnterpriseBot")

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

    if not MODO_DESKTOP:
        chrome_options.add_argument("--headless=new")

    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    mascara = (
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    chrome_options.add_argument(mascara)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=chrome_options
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
    Lê a informação de parcelas pagas na tela principal da cota.
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
    Raspa todas as informações cadastrais e financeiras do cliente,
    incluindo a versão exata do contrato no momento da venda.
    """
    dados = {
        "credito": 0.00,
        "nome": "-",
        "telefone": "-",
        "data_venda": "",
        "grupo": "-",
        "cota": "-",
        "versao": None,
        "pago": False,
        "estado": "-",
        "cpf": "-",
    }

    try:
        xpath_nome = "//td[contains(text(), 'Consorciado:')]/following-sibling::td"
        dados["nome"] = driver.find_element(By.XPATH, xpath_nome).text
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    # Captura Imediata do CPF (Disponível na tela principal)
    try:
        xpath_cpf = "//td[contains(text(), 'CPF/CNPJ:')]/following-sibling::td"
        dados["cpf"] = driver.find_element(By.XPATH, xpath_cpf).text.strip()
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

    # --- CAPTURA DA VERSÃO NA FICHA PRINCIPAL ---
    try:
        xpath_versao = "/html/body/table[4]/tbody/tr[2]/td/strong[3]"
        texto_versao = driver.find_element(By.XPATH, xpath_versao).text
        dados["versao"] = limpar_inteiro(texto_versao)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xpath_pagas = "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td"
        elem = driver.find_element(By.XPATH, xpath_pagas)
        if limpar_inteiro(elem.text) > 0:
            dados["pago"] = True
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    # Navegação para coletar o Telefone
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

    # Navegação para coletar o Estado (Endereço Residencial)
    try:
        # Retorna para a aba principal "Consorciado"
        aba_consorciado = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//a[contains(text(), 'Consorciado')]")
            )
        )
        aba_consorciado.click()
        time.sleep(PAUSA_HUMANA)

        # Clica na sub-aba "Endereço Residencial"
        aba_endereco = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//a[contains(text(), 'Endereço Residencial')] | //td[contains(text(), 'Endereço Residencial')]",
                )
            )
        )
        aba_endereco.click()
        time.sleep(PAUSA_HUMANA)

        # Extrai o Estado usando o ID exato validado no DOM
        elem_estado = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "ESTADO"))
        )

        # Correção: Uso do get_attribute("value") para tags de formulário <input>
        valor_estado = elem_estado.get_attribute("value")

        if not valor_estado:
            valor_estado = elem_estado.text

        dados["estado"] = valor_estado.strip().upper() if valor_estado else ""

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
    Extrai a chave de API do CapSolver a partir do arquivo config.txt.
    """
    if os.path.exists("files/config.txt"):
        with open("files/config.txt", "r", encoding="utf-8") as f:
            for linha in f:
                if "CAPSOLVER" in linha:
                    return linha.split("=")[1].strip()
    return ""


def resolver_captcha_api_direta(api_key: str, site_url: str, site_key: str) -> str:
    """
    Conversa diretamente com o servidor da IA para obter o Token de Liberação.
    """
    logger.info("   -> [IA] Enviando o enigma para a CapSolver...")
    payload = {
        "clientKey": api_key,
        "task": {
            "type": "ReCaptchaV2TaskProxyless",
            "websiteURL": site_url,
            "websiteKey": site_key,
        },
    }

    try:
        res = requests.post(
            "https://api.capsolver.com/createTask", json=payload, timeout=10
        ).json()

        if res.get("errorId", 0) > 0:
            logger.error(
                "   -> [ERRO IA] A CapSolver recusou: %s", res.get("errorDescription")
            )
            return ""

        task_id = res.get("taskId")
        logger.info(
            "   -> [IA] Tarefa aceita! (ID: %s). Aguardando resposta...", task_id
        )

        while True:
            time.sleep(3)
            res_status = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=10,
            ).json()

            status = res_status.get("status")
            if status == "ready":
                logger.info("   -> [SUCESSO IA] Token gerado! Enigma resolvido.")
                return res_status.get("solution", {}).get("gRecaptchaResponse", "")
            if status == "failed":
                logger.error(
                    "   -> [ERRO IA] A inteligência falhou em resolver o desafio."
                )
                return ""

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(
            "   -> [ERRO API] Falha na comunicação HTTP com a CapSolver: %s", e
        )
        return ""


def fazer_login_com_ia(driver) -> bool:
    """
    Fluxo de login puro: insere credenciais, injeta o token da IA, clica no botão
    e estabiliza a sessão na página primária (MasterFrameset).
    """
    logger.info("[PORTARIA] Acessando a página de login da Tradição...")
    url_site = "https://intranet.consorciotradicao.com.br/autocred/"
    driver.get(url_site)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "j_username"))
        )

        driver.find_element(By.ID, "j_username").clear()
        driver.find_element(By.ID, "j_username").send_keys(USUARIO_LOGIN)

        driver.find_element(By.ID, "j_password").clear()
        driver.find_element(By.ID, "j_password").send_keys(SENHA_LOGIN)

        logger.info(
            "   -> Credenciais inseridas. Localizando a fechadura do Captcha..."
        )

        site_key = None
        try:
            recaptcha_div = driver.find_element(By.CLASS_NAME, "g-recaptcha")
            site_key = recaptcha_div.get_attribute("data-sitekey")
        except Exception:  # pylint: disable=broad-exception-caught
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
                logger.info("   -> Token injetado no HTML da página com sucesso!")
            else:
                logger.error("   -> [FALHA] Sem token válido para prosseguir.")
                return False

        time.sleep(2)

        # CLIQUE EXPLÍCITO: Mais estável que a tecla ENTER no modo Headless
        try:
            btn_entrar = driver.find_element(
                By.XPATH,
                "//button[contains(text(), 'Enviar')] | //input[@value='Enviar'] | //button[@type='submit']",
            )
            btn_entrar.click()
        except Exception:  # pylint: disable=broad-exception-caught
            driver.find_element(By.ID, "j_password").send_keys(Keys.ENTER)

        time.sleep(5)

        driver.switch_to.default_content()
        url_atual = driver.current_url.lower()

        # VALIDAÇÃO RIGOROSA: Impede o Falso Positivo
        if "login" in url_atual or "index.asp" in url_atual:
            logger.error(
                "   -> [BARRADO] O portal recusou o acesso (retornou para a página inicial)."
            )
            return False

        # ESTABILIZAÇÃO OBRIGATÓRIA NOS FRAMES
        logger.info("   -> [SUCESSO] Redirecionando e ancorando no MasterFrameset...")
        driver.get(
            "https://intranet.consorciotradicao.com.br/autocred/MasterFrameset.asp"
        )
        time.sleep(2)

        return True

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Erro interno do motor de execução: %s", e)
        return False
