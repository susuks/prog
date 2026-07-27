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
    if os.path.exists(ARQUIVO_ADIMPLENCIA):
        try:
            with open(ARQUIVO_ADIMPLENCIA, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def salvar_adimplencia(dados: dict) -> None:
    with open(ARQUIVO_ADIMPLENCIA, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)


def migrar_para_adimplencia(contrato: str, info_pendente: dict, grupo: str, cota: str):
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
    linha = encontrar_linha_do_contrato(sheet, contrato, col_busca)
    if linha:
        try:
            sheet.update(
                range_name=f"{col_inicio}{linha}:{col_fim}{linha}",
                values=[[str(status_pgto), int(parcelas), str(status_cliente)]],
                value_input_option="USER_ENTERED",
            )
            time.sleep(PAUSA_API_GOOGLE)
            return True
        except Exception:
            pass
    return False


def registrar_apenas_situacao_cliente(
    sheet, contrato: str, status_cliente: str, col_busca: int, col_situacao: int
):
    linha = encontrar_linha_do_contrato(sheet, contrato, col_busca)
    if linha:
        try:
            sheet.update_cell(linha, col_situacao, str(status_cliente))
            time.sleep(PAUSA_API_GOOGLE)
            return True
        except Exception:
            pass
    return False


def consultar_adimplencia_http_v2(
    sessao: requests.Session, contrato: str, info: dict
) -> dict:
    """Extração reconstruída: Segue o fluxo linear exigido pelo servidor."""
    grupo = str(info.get("grupo")).strip()
    cota_str = str(info.get("cota")).strip()
    cota_base = cota_str.split("-")[0].strip() if "-" in cota_str else cota_str
    cpf_cliente = re.sub(r"\D", "", str(info.get("cpf"))) if info.get("cpf") else None

    url_pesquisa = "https://intranet.consorciotradicao.com.br/autocred/Attendance/searchCota.asp?codigo_formulario_intranet=4&descricao_formulario_intranet=Consorciado"

    # 1. Carrega a página de pesquisa (GET) para inicializar a sessão do formulário interno
    sessao.headers.update(
        {
            "Referer": "https://intranet.consorciotradicao.com.br/autocred/LeftFrame.asp?codigo_modulo=AG"
        }
    )
    try:
        res_pre = sessao.get(url_pesquisa, timeout=10)
        if (
            "j_username" in res_pre.text.lower()
            or "acesso não permitido" in res_pre.text.lower()
        ):
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}
    except Exception:
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
            return {
                "encontrou": False,
                "motivo": "NAO_ENCONTRADO",
            }  # Corrigido: se não achou CPF na tabela, é Limbo.

    except Exception:
        return {"encontrou": False, "motivo": "FALHA_REDE"}

    # 3. Ler Painel de Parcelas Pagas
    url_painel = f"https://intranet.consorciotradicao.com.br/autocred/Attendance/dataCota.asp?Codigo_Grupo={grupo}&Codigo_Cota={cota_base}&cgc_cpf_cliente={cpf_cliente}"
    sessao.headers.update({"Referer": url_pesquisa})
    parcelas_pagas = 0
    try:
        res_painel = sessao.get(url_painel, timeout=10)
        if (
            "j_username" in res_painel.text.lower()
            or "acesso não permitido" in res_painel.text.lower()
        ):
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        sopa = BeautifulSoup(res_painel.text, "html.parser")
        td_parcelas = sopa.find(
            lambda tag: tag.name == "td" and "Parcelas Pagas:" in tag.text
        )
        if td_parcelas:
            td_valor = td_parcelas.find_next_sibling("td")
            parcelas_pagas = int(re.sub(r"\D", "", td_valor.text)) if td_valor else 0
    except Exception:
        pass

    # 4. Ler Status da 2ª Via
    desc = urllib.parse.quote("2ª Via Boleto", encoding="iso-8859-1")
    url_2via = f"https://intranet.consorciotradicao.com.br/autocred/Attendance/emissSlip.asp?tipo=0&cgc_cpf_cliente={cpf_cliente}&Codigo_Grupo={grupo}&Codigo_Cota={cota_base}&codigo_formulario_filho_intranet=67&descricao_formulario_filho_intranet={desc}"
    sessao.headers.update({"Referer": url_painel})

    try:
        res_2via = sessao.get(url_2via, timeout=10)
        if (
            "j_username" in res_2via.text.lower()
            or "acesso não permitido" in res_2via.text.lower()
        ):
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

        status_pagamento = (
            f"EM ATRASO {boletos_atr}"
            if boletos_atr > 0
            else ("PENDENTE" if boletos_pend > 0 else "PAGO")
        )

        return {
            "encontrou": True,
            "motivo": "SUCESSO",
            "cpf": cpf_cliente,
            "status_pagamento": status_pagamento,
            "parcelas_pagas": parcelas_pagas,
        }

    except Exception:
        return {"encontrou": False, "motivo": "FALHA_REDE"}


def mapear_relatorios_via_http(sessao: requests.Session) -> dict:
    mapa = {}
    alvos = {"1030": "CANCELADO", "11345": "DESISTENTE"}
    sessao.headers.update(
        {
            "Referer": "https://intranet.consorciotradicao.com.br/autocred/LeftFrame.asp?codigo_modulo=AG"
        }
    )

    for codigo_form, status in alvos.items():
        url_relatorio = f"https://intranet.consorciotradicao.com.br/autocred/plugins/Relatorio_Carteiras/Relatorios/relatorios.asp?Codigo_Formulario={codigo_form}"
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
                        match = re.match(
                            r"^(\d{4,6})\s+(\d{3,4})\s*-\s*(\d{1,2})", texto_coluna
                        )
                        if match:
                            mapa[
                                (
                                    int(match.group(1)),
                                    int(match.group(2)),
                                    int(match.group(3)),
                                )
                            ] = status
        except Exception:
            pass
    return mapa
