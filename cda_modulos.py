"""
Módulo Exclusivo do Controle de Adimplência (CDA).

Contém a lógica de scraping profundo (relatórios offline, 2ª via) e
a manipulação da base de dados e planilhas referentes à adimplência a longo prazo.
"""

import os
import json
import re
import time
import logging
from datetime import datetime
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Importações dos módulos base do sistema 2.3
from gerador_dados import limpar_inteiro, encontrar_linha_do_contrato
from motor_navegacao import PAUSA_HUMANA

logger = logging.getLogger("EnterpriseCRM")

ARQUIVO_ADIMPLENCIA = "files/adimplencia.json"
CACHE_CANCELADOS = "files/cache_cancelados.html"
CACHE_DESISTENTES = "files/cache_desistentes.html"
CACHE_CANCELADOS_DIARIO = "files/cache_cancelados_diario.html"
CACHE_DESISTENTES_DIARIO = "files/cache_desistentes_diario.html"

PAUSA_API_GOOGLE = 0.5
PAUSA_HUMANA_LONGA = 0.5


# ============================================================================
# FUNÇÕES DE INFRAESTRUTURA E REDE
# ============================================================================
def validar_sessao_rede(driver) -> bool:
    """
    Sondagem de nível de Rede (Network Layer).
    Garante o foco na raiz e valida a integridade da sessão do Autocred.
    """
    try:
        driver.switch_to.default_content()
        script_rede = """
        var callback = arguments[arguments.length - 1];
        fetch('/autocred/LeftFrame.asp', {cache: 'no-store'})
            .then(response => {
                if (!response.ok) { callback(false); return; }
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
            .catch(err => { callback(true); });
        """
        driver.set_script_timeout(5)
        status_sessao = driver.execute_async_script(script_rede)
        return status_sessao
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.debug("Falha na validação de rede: %s", e)
        return False


# ============================================================================
# GERENCIAMENTO DE DADOS (JSON E PLANILHAS)
# ============================================================================
def carregar_adimplencia() -> dict:
    """Carrega o banco de dados local de adimplência em formato JSON."""
    if os.path.exists(ARQUIVO_ADIMPLENCIA):
        try:
            with open(ARQUIVO_ADIMPLENCIA, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug("Arquivo JSON vazio ou corrompido: %s", e)
            return {}
    return {}


def salvar_adimplencia(dados: dict) -> None:
    """Persiste o banco de dados do CRM no disco."""
    with open(ARQUIVO_ADIMPLENCIA, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)


def migrar_para_adimplencia(contrato: str, info_pendente: dict, grupo: str, cota: str):
    """Transfere o cliente da fila de 1ª parcela para monitorização do CDA."""
    bd = carregar_adimplencia()
    if contrato not in bd:
        bd[contrato] = {
            "vendedor_nome": info_pendente.get("vendedor_nome"),
            "nome_planilha": info_pendente.get("nome_planilha"),
            "aba_original": info_pendente.get("aba_original"),
            "grupo": grupo,
            "cota": cota,
            "cota_versao": None,
            "monitorar": True,
            "ultima_verificacao": 0,
            "data_limbo": None,
            "ultimo_status": None,
            "ultimas_parcelas": 0,
        }
        salvar_adimplencia(bd)


def mapear_todos_offline() -> dict:
    """
    Otimização de RAM: Lê todos os arquivos HTML de cache apenas uma vez,
    realiza o parsing completo do DOM e gera um mapa de identificação.
    Retorno O(1) de complexidade na busca: {(grupo, cota_base, versao): "Status"}
    """
    mapa_offline = {}
    lista_arquivos = [
        ("Cancelado", CACHE_CANCELADOS),
        ("Desistente", CACHE_DESISTENTES),
        ("Cancelado", CACHE_CANCELADOS_DIARIO),
        ("Desistente", CACHE_DESISTENTES_DIARIO),
    ]

    for status, arquivo in lista_arquivos:
        if os.path.exists(arquivo):
            try:
                with open(arquivo, "r", encoding="utf-8", errors="ignore") as f:
                    sopa = BeautifulSoup(f.read(), "html.parser")
                    linhas = sopa.find_all("tr")

                    for linha in linhas:
                        colunas = linha.find_all("td")
                        if len(colunas) >= 3:
                            texto_grupo = colunas[0].get_text(strip=True)
                            texto_cota_bruto = colunas[1].get_text(strip=True)

                            grupo_html = limpar_inteiro(texto_grupo)
                            match_cota_html = re.search(
                                r"(\d+)\s*-\s*(\d+)", texto_cota_bruto
                            )

                            if match_cota_html and grupo_html > 0:
                                cota_base = limpar_inteiro(match_cota_html.group(1))
                                versao = limpar_inteiro(match_cota_html.group(2))

                                # Adiciona a tupla de identificação ao mapa de memória
                                mapa_offline[(grupo_html, cota_base, versao)] = status

            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error(
                    "Falha ao consolidar o arquivo %s na memória: %s", arquivo, e
                )

    return mapa_offline


def registrar_adimplencia_planilhas(
    sheet,
    contrato: str,
    status_pgto: str,
    parcelas: int,
    status_cliente: str,
    col_busca: int,
    col_inicio: str,
    col_fim: str,
):
    """Submete a atualização Delta nas colunas dinâmicas de adimplência da planilha."""
    linha = encontrar_linha_do_contrato(sheet, contrato, col_busca)
    if linha:
        try:
            dados_adimplencia = [str(status_pgto), int(parcelas), str(status_cliente)]
            sheet.update(
                range_name=f"{col_inicio}{linha}:{col_fim}{linha}",
                values=[dados_adimplencia],
                value_input_option="USER_ENTERED",
            )
            time.sleep(PAUSA_API_GOOGLE)
            return True
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Falha ao atualizar colunas no contrato %s: %s", contrato, e)
    return False


def registrar_apenas_situacao_cliente(
    sheet, contrato: str, status_cliente: str, col_busca: int, col_situacao: int
):
    """Atualiza a coluna de Situação dinamicamente sem alterar dados financeiros."""
    linha = encontrar_linha_do_contrato(sheet, contrato, col_busca)
    if linha:
        try:
            sheet.update_cell(linha, col_situacao, str(status_cliente))
            time.sleep(PAUSA_API_GOOGLE)
            return True
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Falha ao atualizar Situação no contrato %s: %s", contrato, e)
    return False


# ============================================================================
# NAVEGAÇÃO E EXTRAÇÃO (WEB SCRAPING)
# ============================================================================
def buscar_contrato_avancado(driver, contrato: str) -> tuple:
    """Busca cega e direta de contrato, tratando avisos de forma cirúrgica."""
    if not validar_sessao_rede(driver):
        logger.warning(
            "   -> [Motor] Sessão de rede caiu durante busca do contrato %s.", contrato
        )
        return False, "SESSAO_CAIU", ""

    driver.switch_to.default_content()
    try:
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "LeftFrame"))
        )
        driver.find_element(By.LINK_TEXT, "Consorciado").click()
        time.sleep(PAUSA_HUMANA_LONGA)
    except Exception:  # pylint: disable=broad-exception-caught
        return False, "SESSAO_CAIU", ""

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

        btn_xpath = "//input[contains(@value, 'Localizar')]"
        driver.find_element(By.XPATH, btn_xpath).click()

        tempo_limite = time.time() + 6
        xpath_resultado = (
            f"//td/div[contains(text(), '{contrato}')] | "
            "//td[contains(@class, 'hand')]/div"
        )
        xpath_erro_vermelho = (
            "/html/body/form/table[1]/tbody/tr[2]/td/table/tbody/tr[12]/td/div"
        )
        resultado_elemento = None

        while time.time() < tempo_limite:
            try:
                alerta = driver.switch_to.alert
                alerta.accept()
                return False, "NAO_ENCONTRADO", ""
            except Exception:  # pylint: disable=broad-exception-caught
                pass

            try:
                elem_erro = driver.find_element(By.XPATH, xpath_erro_vermelho)
                texto_erro = elem_erro.text.strip().lower()
                if texto_erro and (
                    "inexistente" in texto_erro or "cancelado" in texto_erro
                ):
                    return False, "NAO_ENCONTRADO", ""
            except Exception:  # pylint: disable=broad-exception-caught
                pass

            try:
                elem = driver.find_element(By.XPATH, xpath_resultado)
                if elem.is_displayed():
                    resultado_elemento = elem
                    break
            except Exception:  # pylint: disable=broad-exception-caught
                pass

            time.sleep(0.3)

        if not resultado_elemento:
            if not validar_sessao_rede(driver):
                return False, "SESSAO_CAIU", ""
            return False, "NAO_ENCONTRADO", ""

        try:
            resultado_elemento.click()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        time.sleep(PAUSA_HUMANA)

        driver.switch_to.default_content()
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
        )
        WebDriverWait(driver, 5).until(
            EC.frame_to_be_available_and_switch_to_it((By.NAME, "MainFrame"))
        )
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located(
                (By.XPATH, "//td[contains(text(), 'Consorciado:')]")
            )
        )

        cota_completa = ""
        try:
            xp_cota_topo = "/html/body/table[4]/tbody/tr[2]/td/strong[3]"
            texto_elemento = driver.find_element(By.XPATH, xp_cota_topo).text
            match_generico = re.search(r"(\d+)\s*-\s*(\d+)", texto_elemento)
            if match_generico:
                cota_completa = f"{match_generico.group(1)}-{match_generico.group(2)}"
            else:
                cota_completa = texto_elemento.strip()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        return True, None, cota_completa

    except Exception:  # pylint: disable=broad-exception-caught
        if not validar_sessao_rede(driver):
            return False, "SESSAO_CAIU", ""
        return False, "NAO_ENCONTRADO", ""


def raspar_dados_adimplencia(driver) -> dict:
    """Captura parcelas pagas e gerencia alertas na 2ª Via do Boleto."""
    dados = {"parcelas_pagas": 0, "status_pagamento": "PAGO"}

    try:
        xp_pg = "//td[contains(text(), 'Parcelas Pagas:')]/following-sibling::td"
        elem = driver.find_element(By.XPATH, xp_pg)
        dados["parcelas_pagas"] = limpar_inteiro(elem.text)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    try:
        time.sleep(PAUSA_HUMANA_LONGA)
        xp_2via = "//a[contains(text(), '2ª Via Boleto')] | //a[contains(text(), '2Âª Via Boleto')]"
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, xp_2via))
        ).click()

        try:
            WebDriverWait(driver, 1.5).until(EC.alert_is_present())
            alerta = driver.switch_to.alert
            alerta.accept()
            dados["status_pagamento"] = "SEM ACESSO"
            return dados
        except Exception:  # pylint: disable=broad-exception-caught
            pass

        xpath_tabela = "/html/body/form/table[2]/tbody"
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, xpath_tabela))
        )

        boletos_atrasados = 0
        boletos_pendentes = 0
        agora = datetime.now()

        linhas = driver.find_elements(By.XPATH, f"{xpath_tabela}/tr")

        for linha in linhas[1:]:
            try:
                coluna_descricao = linha.find_element(By.XPATH, "./td[2]").text.upper()
                if "PGTO PARC" in coluna_descricao:
                    col_vencimento = linha.find_element(
                        By.XPATH, "./td[3]"
                    ).text.strip()
                    if col_vencimento:
                        data_venc = datetime.strptime(col_vencimento, "%d/%m/%Y")
                        data_venc = data_venc.replace(hour=23, minute=59, second=59)

                        if agora > data_venc:
                            boletos_atrasados += 1
                        else:
                            boletos_pendentes += 1
            except Exception:  # pylint: disable=broad-exception-caught
                continue

        if boletos_atrasados > 0:
            dados["status_pagamento"] = f"EM ATRASO {boletos_atrasados}"
        elif boletos_pendentes > 0:
            dados["status_pagamento"] = "PENDENTE"
        else:
            dados["status_pagamento"] = "PAGO"

    except Exception:  # pylint: disable=broad-exception-caught
        pass

    return dados


def baixar_relatorios_cache(driver, tipo: str = "frequente"):
    """Descarrega silenciosamente as pastas de Cancelados e Desistentes para cache local."""

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

        try:
            btn_canc = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "sd7"))
            )
            btn_canc.click()

            if tipo == "diario":
                time.sleep(30)
                arquivo_alvo = CACHE_CANCELADOS_DIARIO
            else:
                aguardar_estabilidade(driver)
                arquivo_alvo = CACHE_CANCELADOS

            with open(arquivo_alvo, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

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

        try:
            btn_des = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "sd10"))
            )
            btn_des.click()

            if tipo == "diario":
                time.sleep(30)
                arquivo_alvo = CACHE_DESISTENTES_DIARIO
            else:
                aguardar_estabilidade(driver)
                arquivo_alvo = CACHE_DESISTENTES

            with open(arquivo_alvo, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    except Exception:  # pylint: disable=broad-exception-caught
        pass
    finally:
        try:
            driver.get(
                "https://intranet.consorciotradicao.com.br/autocred/MasterFrameset.asp"
            )
            time.sleep(2)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
