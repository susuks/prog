"""
Módulo Motor de Navegação e Extração (Web Scraping).

Responsável por inicializar o navegador, interagir com o Document Object
Model (DOM), resolver Captchas via IA (CapSolver) e extrair dados da Autocred.
"""

import os
import re
import time
import logging
from datetime import datetime

import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from gerador_dados import (
    USUARIO_LOGIN,
    SENHA_LOGIN,
    MODO_DESKTOP,
    limpar_inteiro,
    limpar_valor,
    CACHE_CANCELADOS,
    CACHE_DESISTENTES,
    CACHE_CANCELADOS_DIARIO,
    CACHE_DESISTENTES_DIARIO,
)

logger = logging.getLogger("EnterpriseBot")
PAUSA_HUMANA = 0.4
PAUSA_HUMANA_LONGA = 2.0  # Freio de segurança para não sobrecarregar o servidor legado


# ============================================================================
# INICIALIZAÇÃO DO NAVEGADOR E DIAGNÓSTICO DE REDE
# ============================================================================
def iniciar_navegador() -> webdriver.Chrome:
    """Configura e inicializa a instância do Chrome WebDriver."""
    chrome_options = Options()
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

    svc = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=svc, options=chrome_options)
    return driver


def validar_sessao_rede(driver: webdriver.Chrome) -> bool:
    """
    Sondagem de nível de Rede (Network Layer).
    Garante o foco na raiz e faz um ping absoluto. Tolera abortos de script.
    """
    try:
        # Garante que o Javascript seja executado na raiz do domínio, escapando de iframes
        driver.switch_to.default_content()
        script_rede = """
        var callback = arguments[arguments.length - 1];
        fetch('/autocred/LeftFrame.asp', {cache: 'no-store'})
            .then(response => {
                if (!response.ok) {
                    // Erro estrutural real do servidor (500, 502, 503, 404).
                    callback(false);
                    return;
                }
                return response.text();
            })
            .then(text => {
                if (text === undefined) return;
                var html_lower = text.toLowerCase();
                if (html_lower.includes('acesso negado') || 
                    html_lower.includes('sessão expirou') || 
                    html_lower.includes('novo login')) {
                    callback(false);
                } else {
                    callback(true);
                }
            })
            .catch(err => {
                // Se o navegador abortar o fetch (ex: tela piscando ou recarregando),
                // assumimos que a sessão está viva para evitar falsos positivos instantâneos.
                callback(true);
            });
        """
        driver.set_script_timeout(5)
        status_sessao = driver.execute_async_script(script_rede)
        return status_sessao
    except Exception:  # pylint: disable=broad-exception-caught
        # Só retorna False se o Selenium perder o controle do navegador
        return False


# ============================================================================
# NAVEGAÇÃO E EXTRAÇÃO DE DADOS (SELENIUM)
# ============================================================================
def buscar_contrato(driver: webdriver.Chrome, contrato: str) -> bool:
    """Busca da primeira parcela do contrato. Ignora bloqueios temporários."""
    driver.switch_to.default_content()
    try:
        frame_main = (By.NAME, "mainFrame")
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it(frame_main)
        )
        frame_left = (By.NAME, "LeftFrame")
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it(frame_left)
        )
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
        time.sleep(PAUSA_HUMANA_LONGA)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    driver.switch_to.default_content()
    try:
        frame_main = (By.NAME, "mainFrame")
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it(frame_main)
        )
        frame_m_up = (By.NAME, "MainFrame")
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it(frame_m_up)
        )

        campo = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.NAME, "NumeroContrato"))
        )
        campo.clear()
        campo.send_keys(contrato)

        btn_xpath = "//input[contains(@value, 'Localizar')]"
        driver.find_element(By.XPATH, btn_xpath).click()
        time.sleep(PAUSA_HUMANA)

        try:
            WebDriverWait(driver, 1).until(EC.alert_is_present())
            driver.switch_to.alert.accept()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        xpath_resultado = (
            f"//td/div[contains(text(), '{contrato}')] | "
            "//td[contains(@class, 'hand')]/div"
        )
        resultado = WebDriverWait(driver, 8).until(
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

        xpath_cons = "//td[contains(text(), 'Consorciado:')]"
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.XPATH, xpath_cons))
        )
        return True
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def buscar_contrato_avancado(driver: webdriver.Chrome, contrato: str) -> tuple:
    """Busca cega e direta. Captura a versão exata da cota na ficha do cliente."""
    if not validar_sessao_rede(driver):
        logger.warning("   -> [Motor] Sessão de rede caiu: %s.", contrato)
        return False, "SESSAO_CAIU", ""

    driver.switch_to.default_content()
    try:
        frame_main = (By.NAME, "mainFrame")
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it(frame_main)
        )
        frame_left = (By.NAME, "LeftFrame")
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it(frame_left)
        )
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
        time.sleep(PAUSA_HUMANA_LONGA)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning(
            "   -> [Motor] Falha de estrutura ao ancorar frames para o contrato %s.",
            contrato,
        )
        return False, "SESSAO_CAIU", ""

    driver.switch_to.default_content()
    try:
        frame_main = (By.NAME, "mainFrame")
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it(frame_main)
        )
        frame_m_up = (By.NAME, "MainFrame")
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it(frame_m_up)
        )

        logger.info("   -> [Motor] Preenchendo campo de busca para %s...", contrato)
        campo = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.NAME, "NumeroContrato"))
        )
        campo.clear()
        campo.send_keys(contrato)

        btn_xpath = "//input[contains(@value, 'Localizar')]"
        driver.find_element(By.XPATH, btn_xpath).click()

        try:
            WebDriverWait(driver, 1.5).until(EC.alert_is_present())
            alerta = driver.switch_to.alert
            texto_alerta = alerta.text
            alerta.accept()
            logger.info(
                "   -> [Motor] Alerta interceptado na busca: '%s'.", texto_alerta
            )
            return False, "NAO_ENCONTRADO", ""
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        xpath_resultado = (
            f"//td/div[contains(text(), '{contrato}')] | "
            "//td[contains(@class, 'hand')]/div"
        )
        resultado = WebDriverWait(driver, 5).until(
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

        xpath_cons = "//td[contains(text(), 'Consorciado:')]"
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.XPATH, xpath_cons))
        )

        logger.info("   -> [Motor] Ficha carregada. Lendo versão de %s...", contrato)

        cota_completa = ""
        try:
            xp_cota_topo = "/html/body/table[4]/tbody/tr[2]/td/strong[3]"
            texto_elemento = driver.find_element(By.XPATH, xp_cota_topo).text

            match_generico = re.search(r"(\d+)\s*-\s*(\d+)", texto_elemento)
            if match_generico:
                cota_completa = f"{match_generico.group(1)}-{match_generico.group(2)}"
            else:
                cota_completa = texto_elemento.strip()

            logger.info(
                "   -> [Motor] Versão lida com sucesso pelo topo: %s", cota_completa
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "   -> [Motor] Não foi possível ler a versão no topo da tela."
            )

        return True, None, cota_completa

    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning(
            "   -> [Motor] Tempo esgotado (Timeout) ou tela vazia ao buscar %s.",
            contrato,
        )
        if not validar_sessao_rede(driver):
            return False, "SESSAO_CAIU", ""
        return False, "NAO_ENCONTRADO", ""


def extrair_dados_completos(driver: webdriver.Chrome) -> dict:
    """Raspagem de dados cadastrais na página principal do consorciado."""
    dados = {
        "credito": 0.00,
        "nome": "-",
        "telefone": "-",
        "data_venda": "",
        "grupo": "-",
        "cota": "-",
        "pago": False,
        "cpf": "-",
        "estado": "-",
    }

    try:
        xp_nome = "//td[contains(text(), 'Consorciado:')]/following-sibling::td"
        dados["nome"] = driver.find_element(By.XPATH, xp_nome).text
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xp_cpf = "//td[contains(text(), 'CPF/CNPJ:')]/following-sibling::td"
        dados["cpf"] = driver.find_element(By.XPATH, xp_cpf).text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xp_cred = "//td[contains(text(), 'Crédito:')]/following-sibling::td"
        texto_cred = driver.find_element(By.XPATH, xp_cred).text
        dados["credito"] = limpar_valor(texto_cred)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xp_data = "//td[contains(text(), 'Adesão:')]/following-sibling::td"
        dados["data_venda"] = driver.find_element(By.XPATH, xp_data).text
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xp_grupo = "//td[contains(text(), 'Grupo:')]/following-sibling::td"
        dados["grupo"] = driver.find_element(By.XPATH, xp_grupo).text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xp_cota = "//td[contains(text(), 'Cota:')]/following-sibling::td"
        dados["cota"] = driver.find_element(By.XPATH, xp_cota).text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xp_pg = "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td"
        elem = driver.find_element(By.XPATH, xp_pg)
        if limpar_inteiro(elem.text) > 0:
            dados["pago"] = True
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        driver.find_element(By.XPATH, "//*[contains(text(), 'Telefones')]").click()
        time.sleep(PAUSA_HUMANA)
        xp_cel = "//td[contains(text(), 'Celular')]/parent::tr/td[2]"
        elem_celular = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, xp_cel))
        )
        dados["telefone"] = elem_celular.text.strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        xp_btn_cons = "//a[contains(text(), 'Consorciado')]"
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, xp_btn_cons))
        ).click()
        time.sleep(PAUSA_HUMANA)

        xp_end = (
            "//a[contains(text(), 'Endereço Residencial')] | "
            "//td[contains(text(), 'Endereço')]"
        )
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, xp_end))
        ).click()
        time.sleep(PAUSA_HUMANA)

        elem_estado = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "ESTADO"))
        )
        dados["estado"] = elem_estado.get_attribute("value").strip()
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    return dados


def raspar_dados_adimplencia(driver: webdriver.Chrome) -> dict:
    """Captura parcelas e gerencia alertas inesperados na 2ª Via."""
    dados = {"parcelas_pagas": 0, "status_pagamento": "PAGO"}

    logger.info("   -> [Motor] Realizando leitura da tela inicial da ficha...")
    try:
        xp_pg = "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td"
        elem = driver.find_element(By.XPATH, xp_pg)
        dados["parcelas_pagas"] = limpar_inteiro(elem.text)
        logger.info(
            "   -> [Motor] Parcelas pagas identificadas: %s", dados["parcelas_pagas"]
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.info("   -> [Motor] Campo de parcelas pagas não encontrado na ficha.")

    try:
        logger.info("   -> [Motor] Tentando acessar a aba de 2ª Via do Boleto...")
        time.sleep(PAUSA_HUMANA_LONGA)

        xp_2via = (
            "//a[contains(text(), '2ª Via Boleto')] | "
            "//a[contains(text(), '2Âª Via Boleto')]"
        )
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, xp_2via))
        ).click()

        try:
            WebDriverWait(driver, 1.5).until(EC.alert_is_present())
            alerta = driver.switch_to.alert
            alerta_texto = alerta.text
            alerta.accept()
            logger.info(
                "   -> [Motor] Alerta interceptado: '%s'. Status: SEM ACESSO.",
                alerta_texto,
            )
            dados["status_pagamento"] = "SEM ACESSO"
            return dados
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        logger.info("   -> [Motor] Aguardando o carregamento da tabela de boletos...")
        xpath_tabela = "/html/body/form/table[2]/tbody"
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, xpath_tabela))
        )

        boletos_atrasados = 0
        boletos_pendentes = 0
        agora = datetime.now()

        linhas = driver.find_elements(By.XPATH, f"{xpath_tabela}/tr")
        logger.info("   -> [Motor] Lendo %s linhas de boletos...", len(linhas) - 1)

        for linha in linhas[1:]:
            try:
                coluna_descricao = linha.find_element(By.XPATH, "./td[2]").text.upper()
                if "PGTO PARC" in coluna_descricao:
                    coluna_vencimento = linha.find_element(
                        By.XPATH, "./td[3]"
                    ).text.strip()
                    if coluna_vencimento:
                        data_venc = datetime.strptime(coluna_vencimento, "%d/%m/%Y")
                        data_venc = data_venc.replace(hour=23, minute=59, second=59)

                        if agora > data_venc:
                            boletos_atrasados += 1
                        else:
                            boletos_pendentes += 1
            except Exception:  # pylint: disable=broad-exception-caught
                continue

        logger.info(
            "   -> [Motor] Concluído: %s Atrasados, %s Pendentes.",
            boletos_atrasados,
            boletos_pendentes,
        )

        if boletos_atrasados > 0:
            dados["status_pagamento"] = f"EM ATRASO {boletos_atrasados}"
        elif boletos_pendentes > 0:
            dados["status_pagamento"] = "PENDENTE"
        else:
            dados["status_pagamento"] = "PAGO"

    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning(
            "   -> [Motor] Tempo esgotado (Timeout) ao ler a tabela de 2ª via."
        )

    return dados


def baixar_relatorios_cache(driver: webdriver.Chrome, tipo: str = "frequente"):
    """
    Download estático das pastas com espera cega para o diário.
    """
    if tipo == "diario":
        logger.info("[SINCRONIZADOR DIÁRIO] Iniciando download profundo (30s)...")
    else:
        logger.info("[SINCRONIZADOR FREQUENTE] Iniciando download rápido...")

    def aguardar_estabilidade(driver_ref):
        tam_anterior = 0
        estabilidade = 0
        for _ in range(30):
            time.sleep(2)
            tam_atual = len(driver_ref.page_source)
            if tam_atual == tam_anterior and tam_atual > 5000:
                estabilidade += 1
                if estabilidade >= 2:
                    return True
            else:
                estabilidade = 0
            tam_anterior = tam_atual
        return False

    try:
        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "topFrame"))
        )

        xp_relatorios = (
            "/html/body/table/tbody/tr[2]/td/table/tbody/tr/td[5]/div/font/a"
        )
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, xp_relatorios))
        ).click()
        time.sleep(1)

        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame"))
        )

        xp_analise = "/html/body/table/tbody/tr[1]/td/table/tbody/tr[4]/td[2]/a"
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, xp_analise))
        ).click()
        time.sleep(1)

        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame"))
        )

        # --- CANCELADOS ---
        try:
            btn_canc = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "sd7"))
            )
            btn_canc.click()

            if tipo == "diario":
                logger.info(
                    "   -> Modo Diário: Forçando espera bruta de 30 segundos..."
                )
                time.sleep(30)
                arquivo_alvo = CACHE_CANCELADOS_DIARIO
            else:
                logger.info("   -> Modo Frequente: Avaliando estabilidade da página...")
                aguardar_estabilidade(driver)
                arquivo_alvo = CACHE_CANCELADOS

            with open(arquivo_alvo, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            logger.info("   -> Cancelados sincronizados com sucesso!")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("   -> Falha na pasta de Cancelados: %s", e)

        # Retorno aos Frames
        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, xp_analise))
        ).click()
        time.sleep(1)

        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame"))
        )

        # --- DESISTENTES ---
        try:
            btn_des = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "sd10"))
            )
            btn_des.click()

            if tipo == "diario":
                logger.info(
                    "   -> Modo Diário: Forçando espera bruta de 30 segundos..."
                )
                time.sleep(30)
                arquivo_alvo = CACHE_DESISTENTES_DIARIO
            else:
                logger.info("   -> Modo Frequente: Avaliando estabilidade da página...")
                aguardar_estabilidade(driver)
                arquivo_alvo = CACHE_DESISTENTES

            with open(arquivo_alvo, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            logger.info("   -> Desistentes sincronizados com sucesso!")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("   -> Falha na pasta de Desistentes: %s", e)

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(
            "[ERRO SINCRONIZADOR] Falha severa na navegação de relatórios: %s", e
        )

    finally:
        logger.info("   -> Restaurando o navegador para a página principal (Vendas)...")
        try:
            driver.get(
                "https://intranet.consorciotradicao.com.br/autocred/MasterFrameset.asp"
            )
            time.sleep(2)
        except Exception as e_reset:  # pylint: disable=broad-exception-caught
            logger.error("   -> Falha ao resetar o MasterFrameset: %s", e_reset)


def manter_sessao_viva(driver: webdriver.Chrome) -> bool:
    """
    Atua como Keep-Alive gerando tráfego na página inicial.
    Utiliza validação de rede assíncrona para garantir precisão absoluta.
    """
    try:
        if not validar_sessao_rede(driver):
            logger.warning(
                "   -> [SISTEMA] Falha de Rede detectada antes do Keep-Alive."
            )
            return False

        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "topFrame"))
        )

        xpath_vendas = (
            "/html/body/table/tbody/tr[2]/td/table/tbody/tr/td[2]/div/font | "
            "//font[contains(text(), 'VENDAS')] | //a[contains(., 'VENDAS')]"
        )
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, xpath_vendas))
        ).click()
        time.sleep(1.5)

        if not validar_sessao_rede(driver):
            logger.warning("   -> [SISTEMA] Acesso Negado detectado na camada de rede.")
            return False

        return True

    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("   -> [SISTEMA] Keep-Alive não encontrou a estrutura física.")
        return False


# ============================================================================
# INTELIGÊNCIA ARTIFICIAL E AUTENTICAÇÃO
# ============================================================================
def carregar_chave_capsolver() -> str:
    """Recupera a chave secreta da API do CapSolver a partir do ficheiro local."""
    if os.path.exists("files/config.txt"):
        with open("files/config.txt", "r", encoding="utf-8") as f:
            for linha in f:
                if linha.startswith("CAPSOLVER_KEY="):
                    return linha.split("=", 1)[1].strip()
    return ""


def resolver_captcha_api_direta(api_key: str, site_url: str, site_key: str) -> str:
    """Comunica-se com a CapSolver para delegar a resolução do ReCaptcha."""
    logger.info("   -> [IA] Enviando o enigma para a CapSolver...")

    payload = {
        "clientKey": api_key,
        "task": {
            "type": "ReCaptchaV2TaskProxyLess",
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
                timeout=100,
            ).json()

            status = res_status.get("status")
            if status == "ready":
                logger.info("   -> [SUCESSO IA] Token gerado! Enigma resolvido.")
                return res_status.get("solution").get("gRecaptchaResponse")

            if status == "failed":
                logger.error(
                    "   -> [ERRO IA] A inteligência falhou em resolver o desafio."
                )
                return ""

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("   -> [ERRO API] Falha na comunicação HTTP: %s", e)
        return ""


def fazer_login_com_ia(driver) -> bool:
    """Preenche dados e injeta o token do captcha no DOM."""
    logger.info("[PORTARIA] Acessando a página de login da Tradição...")
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
        logger.info(
            "   -> Credenciais inseridas. Localizando a fechadura do Captcha..."
        )

        try:
            elemento_captcha = driver.find_element(By.CLASS_NAME, "g-recaptcha")
            site_key = elemento_captcha.get_attribute("data-sitekey")
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "   -> [AVISO] Captcha não encontrado. Tentando logar direto..."
            )
            site_key = None

        if site_key:
            token_liberacao = resolver_captcha_api_direta(
                carregar_chave_capsolver(), url_site, site_key
            )
            if token_liberacao:
                js_script = (
                    f"document.getElementById('g-recaptcha-response').innerHTML = "
                    f"'{token_liberacao}';"
                )
                driver.execute_script(js_script)
                logger.info("   -> Token injetado no HTML da página com sucesso!")
            else:
                logger.error("   -> [FALHA] Sem token válido para prosseguir.")
                return False

        time.sleep(2)
        try:
            xpath_btn = (
                "//button[contains(text(), 'Enviar')] | //input[@value='Enviar']"
            )
            driver.find_element(By.XPATH, xpath_btn).click()
        except Exception:  # pylint: disable=broad-exception-caught
            driver.find_element(By.ID, "j_password").send_keys(Keys.ENTER)

        time.sleep(6)

        driver.switch_to.default_content()
        url_atual = driver.current_url.lower()
        if "login" in url_atual or "index.asp" in url_atual:
            logger.error(
                "   -> [BARRADO] O portal recusou o acesso (retornou à página inicial)."
            )
            return False

        logger.info("   -> [SUCESSO] Redirecionando e ancorando no MasterFrameset...")
        url_master = (
            "https://intranet.consorciotradicao.com.br/autocred/MasterFrameset.asp"
        )
        driver.get(url_master)
        time.sleep(2)
        return True

    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(
            "   -> [ERRO PORTARIA] Sequência de login falhou criticamente: %s", e
        )
        return False
