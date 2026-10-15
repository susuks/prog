"""
Módulo de Autenticação Efêmera (Selenium + CapSolver -> Sessão HTTP).

Gerencia a abertura pontual do navegador exclusivamente para transpor o
reCaptcha no login e transferir os cookies de sessão para o motor HTTP.
"""

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

from core.config import (
    USUARIO_LOGIN,
    SENHA_LOGIN,
    CHAVE_CAPSOLVER,
    URL_BASE_PORTAL,
    carregar_configuracoes,
)

logger = logging.getLogger("EnterpriseAuth")

USER_AGENT_PADRAO = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def iniciar_navegador() -> webdriver.Chrome:
    """
    Configura e inicializa a instância efêmera do Chrome WebDriver.
    """
    chrome_options = Options()

    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(f"user-agent={USER_AGENT_PADRAO}")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=chrome_options,
    )
    return driver


def carregar_chave_capsolver() -> str:
    """Retorna a chave da API CapSolver carregada na configuração."""
    if CHAVE_CAPSOLVER:
        return CHAVE_CAPSOLVER
    return carregar_configuracoes().get("CAPSOLVER", "")


def resolver_captcha_api_direta(api_key: str, site_url: str, site_key: str) -> str:
    """
    Comunica diretamente com a API da CapSolver para resolver o ReCaptchaV2.
    """
    logger.info("   -> [IA] Enviando desafio para a CapSolver...")
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
                "   -> [ERRO IA] CapSolver recusou: %s", res.get("errorDescription")
            )
            return ""

        task_id = res.get("taskId")
        logger.info("   -> [IA] Tarefa aceita (ID: %s). Aguardando...", task_id)

        for _ in range(40):
            time.sleep(3)
            res_status = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=10,
            ).json()

            status = res_status.get("status")
            if status == "ready":
                logger.info("   -> [SUCESSO IA] Token de liberação gerado.")
                return res_status.get("solution", {}).get("gRecaptchaResponse", "")
            if status == "failed":
                logger.error("   -> [ERRO IA] Falha na resolução do desafio.")
                return ""

        logger.error("   -> [ERRO IA] Tempo limite excedido aguardando CapSolver.")
        return ""

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("   -> [ERRO API] Falha de conexão com a CapSolver: %s", e)
        return ""


def fazer_login_com_ia(driver: webdriver.Chrome) -> bool:
    """
    Executa a autenticação no portal, injeta o token do reCaptcha e ancora no MasterFrameset.
    """
    logger.info("[PORTARIA] Acessando página de login da Tradição...")
    url_site = f"{URL_BASE_PORTAL}/"
    driver.get(url_site)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "j_username"))
        )

        driver.find_element(By.ID, "j_username").clear()
        driver.find_element(By.ID, "j_username").send_keys(USUARIO_LOGIN)

        driver.find_element(By.ID, "j_password").clear()
        driver.find_element(By.ID, "j_password").send_keys(SENHA_LOGIN)

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
            else:
                logger.error("   -> [FALHA] Sem token válido para prosseguir.")
                return False

        time.sleep(2)

        try:
            btn_entrar = driver.find_element(
                By.XPATH,
                "//button[contains(text(), 'Enviar')] | "
                "//input[@value='Enviar'] | //button[@type='submit']",
            )
            btn_entrar.click()
        except Exception:  # pylint: disable=broad-exception-caught
            driver.find_element(By.ID, "j_password").send_keys(Keys.ENTER)

        time.sleep(5)

        driver.switch_to.default_content()
        url_atual = driver.current_url.lower()

        if "login" in url_atual or "index.asp" in url_atual:
            logger.error("   -> [BARRADO] O portal recusou o acesso.")
            return False

        driver.get(f"{URL_BASE_PORTAL}/MasterFrameset.asp")
        time.sleep(2)
        return True

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Erro interno durante autenticação Selenium: %s", e)
        return False


def aquecer_sessao_asp(sessao: requests.Session) -> None:
    """Ancora os cookies de sessão requisitando as molduras primárias do ASP."""
    try:
        sessao.get(f"{URL_BASE_PORTAL}/MasterFrameset.asp", timeout=10)
        sessao.get(f"{URL_BASE_PORTAL}/LeftFrame.asp?codigo_modulo=AG", timeout=10)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Aviso durante aquecimento da sessão ASP: %s", e)


def criar_sessao_http_autenticada() -> requests.Session:
    """
    Ciclo completo: abre o navegador efêmero, autentica com IA, extrai os cookies
    para um objeto requests.Session e destrói o navegador imediatamente.
    """
    driver_temp = iniciar_navegador()
    try:
        if not fazer_login_com_ia(driver_temp):
            raise ConnectionError("Falha de autenticação IA no portal Autocred.")

        nova_sessao = requests.Session()
        nova_sessao.headers.update(
            {
                "User-Agent": USER_AGENT_PADRAO,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
            }
        )

        for cookie in driver_temp.get_cookies():
            nova_sessao.cookies.set(
                cookie["name"], cookie["value"], domain=cookie.get("domain")
            )

        aquecer_sessao_asp(nova_sessao)
        return nova_sessao
    finally:
        try:
            driver_temp.quit()
        except Exception:  # pylint: disable=broad-exception-caught
            pass
