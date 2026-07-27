"""
Módulo Exclusivo do Controle de Adimplência (CDA) - HTTP Puro (Reconstrução Zero).
Extração direta, sem atalhos que quebram o estado do ASP.
"""

import os
import json
import re
import time
import logging
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup

from gerador_dados import encontrar_linha_do_contrato

logger = logging.getLogger("EnterpriseCRM")
ARQUIVO_ADIMPLENCIA = "files/adimplencia.json"
PAUSA_API_GOOGLE = 0.5


def carregar_adimplencia() -> dict:
    """
    Lê o ficheiro JSON contendo o estado de todos os contratos em monitorização.
    """
    if os.path.exists(ARQUIVO_ADIMPLENCIA):
        try:
            with open(ARQUIVO_ADIMPLENCIA, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # pylint: disable=broad-exception-caught
            return {}
    return {}


def salvar_adimplencia(dados: dict) -> None:
    """
    Guarda o estado de monitorização no ficheiro JSON persistente.
    """
    with open(ARQUIVO_ADIMPLENCIA, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)


def migrar_para_adimplencia(contrato: str, info_pendente: dict, grupo: str, cota: str):
    """
    Transita um contrato novo para a fila de adimplência.
    """
    bd = carregar_adimplencia()
    if str(contrato) not in bd:
        cpf_cru = info_pendente.get("dados_originais", {}).get("cpf")
        bd[str(contrato)] = {
            "vendedor_nome": info_pendente.get("vendedor_nome"),
            "nome_planilha": info_pendente.get("nome_planilha"),
            "aba_original": info_pendente.get("aba_original"),
            "grupo": grupo,
            "cota": cota,
            "cpf": re.sub(r"\D", "", str(cpf_cru)) if cpf_cru else None,
            "cota_versao": None,
            "monitorar": True,
            "ultima_verificacao": 0,
            "data_limbo": None,
            "ultimo_status": None,
            "ultimas_parcelas": 0,
        }
        salvar_adimplencia(bd)


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
    """
    Insere os três blocos de dados do cliente na respetiva aba do Google Sheets.
    """
    linha = encontrar_linha_do_contrato(sheet, contrato, col_busca)
    if linha:
        try:
            intervalo = f"{col_inicio}{linha}:{col_fim}{linha}"
            valores = [[str(status_pgto), int(parcelas), str(status_cliente)]]
            sheet.update(
                range_name=intervalo, values=valores, value_input_option="USER_ENTERED"
            )
            time.sleep(PAUSA_API_GOOGLE)
            return True
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return False


def registrar_apenas_situacao_cliente(
    sheet, contrato: str, status_cliente: str, col_busca: int, col_situacao: int
):
    """
    Atualiza estritamente a célula de situação (Ex: Cancelado, Inacessível).
    """
    linha = encontrar_linha_do_contrato(sheet, contrato, col_busca)
    if linha:
        try:
            sheet.update_cell(linha, col_situacao, str(status_cliente))
            time.sleep(PAUSA_API_GOOGLE)
            return True
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return False


def validar_morte_sessao(texto_html: str) -> bool:
    """
    Verifica se a String HTML devolvida pelo ASP contém assinaturas de bloqueio.
    """
    html_min = texto_html.lower()
    gatilhos = [
        "acesso não permitido",
        "j_username",
        "senha",
        "sessão expirou",
        "novo login",
        "default.asp",
        "alert('",
        "window.top.location",
    ]
    return any(gatilho in html_min for gatilho in gatilhos)


def consultar_adimplencia_http_v2(
    sessao: requests.Session, contrato: str, info: dict
) -> dict:
    """Extração reconstruída: Segue o fluxo linear exigido pelo servidor."""
    grupo = str(info.get("grupo")).strip()
    cota_str = str(info.get("cota")).strip()
    cota_base = (
        cota_str.split("-", maxsplit=1)[0].strip() if "-" in cota_str else cota_str
    )
    cpf_cliente = re.sub(r"\D", "", str(info.get("cpf"))) if info.get("cpf") else None

    url_pesquisa = (
        "https://intranet.consorciotradicao.com.br/autocred/"
        "Attendance/searchCota.asp?codigo_formulario_intranet=4&"
        "descricao_formulario_intranet=Consorciado"
    )
    url_left = "https://intranet.consorciotradicao.com.br/autocred/LeftFrame.asp?codigo_modulo=AG"

    # 1. Carrega a página de pesquisa (GET) para inicializar a sessão do formulário interno
    sessao.headers.update({"Referer": url_left})
    try:
        res_pre = sessao.get(url_pesquisa, timeout=10)
        if (
            "j_username" in res_pre.text.lower()
            or "acesso não permitido" in res_pre.text.lower()
        ):
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}
    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}

    # 2. Submete os dados (POST)
    payload = f"Grupo=&Cota=&cgc_cpf_cliente=&nome=&NumeroContrato={contrato}&event=3"
    sessao.headers.update({"Content-Type": "application/x-www-form-urlencoded"})

    try:
        res_post = sessao.post(url_pesquisa, data=payload, timeout=10)
        html_post = res_post.text.lower()

        if "j_username" in html_post or "acesso não permitido" in html_post:
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        if "não reconheço os dados" in html_post:
            return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

        if not cpf_cliente:
            sopa = BeautifulSoup(res_post.text, "html.parser")
            for td in sopa.find_all("td"):
                txt = td.text.strip()
                if ("." in txt and "-" in txt) and len(txt) in [14, 18]:
                    cpf_cliente = re.sub(r"\D", "", txt)
                    break

        if not cpf_cliente:
            return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}

    # 3. Ler Painel de Parcelas Pagas
    url_painel = (
        f"https://intranet.consorciotradicao.com.br/autocred/Attendance/dataCota.asp?"
        f"Codigo_Grupo={grupo}&Codigo_Cota={cota_base}&cgc_cpf_cliente={cpf_cliente}"
    )
    sessao.headers.update({"Referer": url_pesquisa})
    parcelas_pagas = 0
    try:
        res_painel = sessao.get(url_painel, timeout=10)
        html_painel = res_painel.text.lower()
        if "j_username" in html_painel or "acesso não permitido" in html_painel:
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        sopa = BeautifulSoup(res_painel.text, "html.parser")
        td_parcelas = sopa.find(
            lambda tag: tag.name == "td" and "Parcelas Pagas:" in tag.text
        )
        if td_parcelas:
            td_valor = td_parcelas.find_next_sibling("td")
            parcelas_pagas = int(re.sub(r"\D", "", td_valor.text)) if td_valor else 0
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    # 4. Ler Status da 2ª Via
    desc = urllib.parse.quote("2ª Via Boleto", encoding="iso-8859-1")
    url_2via = (
        f"https://intranet.consorciotradicao.com.br/autocred/Attendance/emissSlip.asp?"
        f"tipo=0&cgc_cpf_cliente={cpf_cliente}&Codigo_Grupo={grupo}&Codigo_Cota={cota_base}&"
        f"codigo_formulario_filho_intranet=67&descricao_formulario_filho_intranet={desc}"
    )
    sessao.headers.update({"Referer": url_painel})

    try:
        res_2via = sessao.get(url_2via, timeout=10)
        html_2via = res_2via.text.lower()
        if "j_username" in html_2via or "acesso não permitido" in html_2via:
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        sopa2 = BeautifulSoup(res_2via.text, "html.parser")
        tabela_alvo = next(
            (tb for tb in sopa2.find_all("table") if "VENCIMENTO" in tb.text), None
        )

        if not tabela_alvo:
            return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

        boletos_atr, boletos_pend = 0, 0
        agora = datetime.now()

        for linha in tabela_alvo.find_all("tr")[1:]:
            cols = linha.find_all("td")
            if len(cols) >= 3:
                descricao = cols[1].text.strip().upper()
                venc = cols[2].text.strip()
                if "PGTO PARC" in descricao and venc:
                    try:
                        dv = datetime.strptime(venc, "%d/%m/%Y").replace(
                            hour=23, minute=59, second=59
                        )
                        if agora > dv:
                            boletos_atr += 1
                        else:
                            boletos_pend += 1
                    except ValueError:
                        continue

        if boletos_atr > 0:
            status_pagamento = f"EM ATRASO {boletos_atr}"
        else:
            status_pagamento = "PENDENTE" if boletos_pend > 0 else "PAGO"

        return {
            "encontrou": True,
            "motivo": "SUCESSO",
            "cpf": cpf_cliente,
            "status_pagamento": status_pagamento,
            "parcelas_pagas": parcelas_pagas,
        }

    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}


def mapear_relatorios_via_http(sessao: requests.Session) -> dict:
    """
    Obtém as tabelas de contratos inativos mapeando-as em memória.
    """
    mapa = {}
    alvos = {"1030": "CANCELADO", "11345": "DESISTENTE"}
    url_left = "https://intranet.consorciotradicao.com.br/autocred/LeftFrame.asp?codigo_modulo=AG"
    sessao.headers.update({"Referer": url_left})

    for codigo_form, status in alvos.items():
        url_relatorio = (
            f"https://intranet.consorciotradicao.com.br/autocred/plugins/"
            f"Relatorio_Carteiras/Relatorios/relatorios.asp?Codigo_Formulario={codigo_form}"
        )
        try:
            resposta = sessao.get(url_relatorio, timeout=45)
            if (
                resposta.status_code == 200
                and "j_username" not in resposta.text.lower()
            ):
                sopa = BeautifulSoup(resposta.text, "html.parser")
                for linha in sopa.find_all("tr"):
                    colunas = linha.find_all("td")
                    if colunas:
                        texto_coluna = colunas[0].text.strip()
                        regex_str = r"^(\d{4,6})\s+(\d{3,4})\s*-\s*(\d{1,2})"
                        match = re.match(regex_str, texto_coluna)
                        if match:
                            g_id = int(match.group(1))
                            c_id = int(match.group(2))
                            v_id = int(match.group(3))
                            mapa[(g_id, c_id, v_id)] = status
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return mapa
