"""
Motor de Navegação Web (Selenium).

Responsável por gerenciar as instâncias do navegador, transpor barreiras
de login e reCaptcha (CapSolver) e extrair os dados cadastrais diretamente do DOM.
"""

import os
import time
import logging
import re
import json
from bs4 import BeautifulSoup

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

ARQUIVO_ADIMPLENCIA = "files/adimplencia.json"
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


def carregar_adimplencia() -> dict:
    """Lê e processa o arquivo JSON de auditorias do CDA."""
    if os.path.exists(ARQUIVO_ADIMPLENCIA):
        try:
            with open(ARQUIVO_ADIMPLENCIA, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def salvar_adimplencia(dados: dict) -> None:
    """Persiste os dados de auditoria com Escrita Anti-Corrupção."""
    temp_file = ARQUIVO_ADIMPLENCIA + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)
    os.replace(temp_file, ARQUIVO_ADIMPLENCIA)


def consultar_venda_http_completa(sessao: requests.Session, contrato: str) -> dict:
    """
    Motor HTTP Puro para o site_main.py (Substitui o Selenium).
    Extração Expressa (1-2s): Retorna apenas dados cadastrais e financeiros básicos.
    """
    url_base = "https://intranet.consorciotradicao.com.br/autocred"

    url_pesq = (
        f"{url_base}/Attendance/searchCota.asp?"
        f"codigo_formulario_intranet=4&descricao_formulario_intranet=Consorciado"
    )

    payload_otimizado = {
        "Grupo": "",
        "Cota": "",
        "cgc_cpf_cliente": "",
        "nome": "",
        "NumeroContrato": str(contrato),
        "event": "3",
    }

    try:
        # 1. AQUECIMENTO E POST DE BUSCA
        sessao.headers.update({"Referer": f"{url_base}/LeftFrame.asp?codigo_modulo=AG"})
        sessao.get(url_pesq, timeout=10)

        sessao.headers.update({"Referer": url_pesq})
        res_post = sessao.post(
            url_pesq, data=payload_otimizado, timeout=15, allow_redirects=True
        )

        if res_post.status_code != 200 or len(res_post.text) < 1000:
            return {"encontrou": False, "motivo": "FALHA_REDE"}

        html_post = res_post.text.lower()
        if "j_username" in html_post or "acesso não permitido" in html_post:
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        if (
            "total de cotas encontradas" not in html_post
            and "datacota.asp" not in res_post.url.lower()
        ):
            return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}
        if "mostrando 0 até" in html_post:
            return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

        url_painel_atual = res_post.url
        html_painel = res_post.text
        cpf_cliente = grupo_cliente = cota_cliente = None

        # 2. RESOLVE O REDIRECIONAMENTO E CAPTURA AS CHAVES
        if "datacota.asp" in url_painel_atual.lower():
            match_cpf = re.search(
                r"cgc_cpf_cliente=(\d+)", url_painel_atual, re.IGNORECASE
            )
            match_grupo = re.search(
                r"Codigo_Grupo=(\d+)", url_painel_atual, re.IGNORECASE
            )
            match_cota = re.search(
                r"Codigo_Cota=(\d+)", url_painel_atual, re.IGNORECASE
            )

            cpf_cliente = match_cpf.group(1) if match_cpf else None
            grupo_cliente = match_grupo.group(1) if match_grupo else None
            cota_cliente = match_cota.group(1) if match_cota else None
        else:
            sopa_busca = BeautifulSoup(res_post.text, "html.parser")
            linha_clicavel = sopa_busca.find("tr", attrs={"onclick": True})

            if not linha_clicavel:
                return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

            match_url = re.search(
                r"resultSearchCota\.asp\?[^']+", linha_clicavel["onclick"]
            )
            if not match_url:
                return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

            url_validacao = f"{url_base}/Attendance/{match_url.group(0).strip()}"
            sessao.headers.update({"Referer": res_post.url})
            res_valida = sessao.get(url_validacao, timeout=15, allow_redirects=True)

            url_painel_atual = res_valida.url
            if "datacota.asp" in url_painel_atual.lower():
                html_painel = res_valida.text

            match_cpf = re.search(
                r"cgc_cpf_cliente=(\d+)", url_painel_atual, re.IGNORECASE
            )
            match_grupo = re.search(
                r"Codigo_Grupo=(\d+)", url_painel_atual, re.IGNORECASE
            )
            match_cota = re.search(
                r"Codigo_Cota=(\d+)", url_painel_atual, re.IGNORECASE
            )

            cpf_cliente = match_cpf.group(1) if match_cpf else None
            grupo_cliente = match_grupo.group(1) if match_grupo else None
            cota_cliente = match_cota.group(1) if match_cota else None

        if (
            "j_username" in html_painel.lower()
            or "acesso não permitido" in html_painel.lower()
        ):
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        # 3. EXTRAI OS DADOS DA TELA PRINCIPAL
        dados = {
            "contrato": contrato,
            "grupo": str(grupo_cliente or "-"),
            "cota": str(cota_cliente or "-"),
            "versao": None,
            "cpf": str(cpf_cliente or "-"),
            "nome": "-",
            "data_venda": "",
            "credito": 0.00,
            "pago": False,
            "telefone": "",
            "estado": "",
            "valor_parcela": 0.00,
        }

        sopa = BeautifulSoup(html_painel, "html.parser")

        def buscar_td(rotulo):
            td = sopa.find("td", string=re.compile(rotulo, re.IGNORECASE))
            return (
                td.find_next_sibling("td").text.strip()
                if td and td.find_next_sibling("td")
                else ""
            )

        dados["nome"] = buscar_td("Consorciado:") or "-"
        dados["data_venda"] = buscar_td("Adesão:") or ""

        cred_str = buscar_td("Crédito:")
        if cred_str:
            dados["credito"] = limpar_valor(cred_str)

        parc_str = buscar_td("Contribuição Mensal Atual:")
        if parc_str:
            dados["valor_parcela"] = limpar_valor(parc_str)

        td_header = sopa.find(
            lambda tag: tag.name == "td"
            and "Grupo:" in tag.text
            and "Versão:" in tag.text
        )
        if td_header:
            strongs = td_header.find_all("strong")
            if len(strongs) >= 3:
                dados["versao"] = limpar_inteiro(strongs[2].text)

        pagas_str = buscar_td("Parcelas Pagas:")
        if pagas_str and limpar_inteiro(pagas_str) > 0:
            dados["pago"] = True

        if cpf_cliente and grupo_cliente and cota_cliente:
            # 4. CAPTURA DO TELEFONE E ESTADO VIA ENDPOINTS
            try:
                url_tel = (
                    f"{url_base}/Attendance/telefone_consorciado.asp?tipo=0&"
                    f"cgc_cpf_cliente={cpf_cliente}&Codigo_Grupo={grupo_cliente}&"
                    f"Codigo_Cota={cota_cliente}&codigo_formulario_filho_intranet=133&"
                    f"descricao_formulario_filho_intranet=Telefones"
                )
                sessao.headers.update({"Referer": url_painel_atual})
                res_tel = sessao.get(url_tel, timeout=5)

                if "j_username" not in res_tel.text.lower():
                    sopa_tel = BeautifulSoup(res_tel.text, "html.parser")
                    td_celular = sopa_tel.find(
                        "td", string=re.compile("Celular", re.IGNORECASE)
                    )
                    if td_celular:
                        tr = td_celular.find_parent("tr")
                        if len(tr.find_all("td")) >= 2:
                            dados["telefone"] = tr.find_all("td")[1].text.strip()
            except Exception:  # pylint: disable=broad-exception-caught
                pass

            try:
                url_end = (
                    f"{url_base}/Prospects/editAddrHome.asp?focar=document.form1.CEP.focus()&"
                    f"CGC_CPF_CLIENTE={cpf_cliente}&TIPO=0&codigo_grupo={grupo_cliente}&"
                    f"codigo_cota={cota_cliente}&processo="
                )
                sessao.headers.update({"Referer": url_painel_atual})
                res_end = sessao.get(url_end, timeout=5)

                if "j_username" not in res_end.text.lower():
                    sopa_end = BeautifulSoup(res_end.text, "html.parser")
                    estado_input = sopa_end.find(id="ESTADO")
                    if estado_input:
                        val = estado_input.get("value", "").strip()
                        dados["estado"] = (
                            val.upper() if val else estado_input.text.strip().upper()
                        )
            except Exception:  # pylint: disable=broad-exception-caught
                pass

        return {"encontrou": True, "motivo": "SUCESSO", "dados": dados}

    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}


def migrar_para_adimplencia(contrato: str, info_pendente: dict, grupo: str, cota: str):
    """
    Efetua o transbordo estrutural de um cliente pago para a fila longa do CDA,
    trazendo o nome e a versão da cota desde a sua origem no sistema principal.
    """
    bd = carregar_adimplencia()
    if str(contrato) not in bd:
        cpf_cru = info_pendente.get("dados_originais", {}).get("cpf")
        versao = info_pendente.get("versao")
        cota_base = limpar_inteiro(cota)

        cota_versao_str = (
            f"{cota_base:04d}-{versao:02d}" if versao is not None else None
        )

        bd[str(contrato)] = {
            "vendedor_nome": info_pendente.get("vendedor_nome"),
            "nome_planilha": info_pendente.get("nome_planilha"),
            "aba_original": info_pendente.get("aba_original"),
            "grupo": grupo,
            "cota": str(cota_base),
            "nome": info_pendente.get("nome"),
            "cpf": re.sub(r"\D", "", str(cpf_cru)) if cpf_cru else None,
            "cota_versao": cota_versao_str,
            "monitorar": True,
            "ultima_verificacao": 0,
            "data_limbo": None,
            "ultimo_status": None,
            "ultimas_parcelas": 0,
        }
        salvar_adimplencia(bd)
