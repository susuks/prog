"""
Módulo de Extração HTTP do Portal Consórcio Tradição.

Contém exclusivamente os extratores ativos da arquitetura Server-to-Server:
1. Consulta expressa de vendas (site_main.py)
2. Raspagem do relatório consolidado de desistentes (api_cda_main.py)
"""

import re
import logging
from typing import List, Dict
import requests
from bs4 import BeautifulSoup

from core.config import URL_BASE_PORTAL
from core.utils import limpar_inteiro, limpar_valor
from core.schemas import DesistenteSchema

logger = logging.getLogger("CDAWorker")


def consultar_venda_http_completa(sessao: requests.Session, contrato: str) -> dict:
    """
    Extração Expressa (1-2s) para o site_main.py:
    Retorna dados cadastrais, telefone, UF e status da 1ª parcela.
    """
    url_pesq = (
        f"{URL_BASE_PORTAL}/Attendance/searchCota.asp?"
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
        sessao.headers.update(
            {"Referer": f"{URL_BASE_PORTAL}/LeftFrame.asp?codigo_modulo=AG"}
        )
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

            url_validacao = f"{URL_BASE_PORTAL}/Attendance/{match_url.group(0).strip()}"
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
            try:
                url_tel = (
                    f"{URL_BASE_PORTAL}/Attendance/telefone_consorciado.asp?tipo=0&"
                    f"cgc_cpf_cliente={cpf_cliente}&Codigo_Grupo={grupo_cliente}&"
                    f"Codigo_Cota={cota_cliente}&codigo_formulario_filho_intranet=133&"
                    f"descricao_formulario_filho_intranet=Telefones"
                )
                sessao.headers.update({"Referer": url_painel_atual})
                res_tel = sessao.get(url_tel, timeout=15)

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
                    f"{URL_BASE_PORTAL}/Prospects/editAddrHome.asp?"
                    f"focar=document.form1.CEP.focus()&CGC_CPF_CLIENTE={cpf_cliente}&"
                    f"TIPO=0&codigo_grupo={grupo_cliente}&codigo_cota={cota_cliente}&processo="
                )
                sessao.headers.update({"Referer": url_painel_atual})
                res_end = sessao.get(url_end, timeout=15)

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


def extrair_relatorio_desistentes(sessao: requests.Session) -> List[Dict]:
    """
    Faz o scraping do formulário de desistentes (11345), interpretando o DOM
    e validando cada registro com DesistenteSchema.
    """
    logger.info("[RELATÓRIO] Extraindo relatório consolidado de desistentes...")
    resultados = []
    url_relatorio = (
        f"{URL_BASE_PORTAL}/plugins/Relatorio_Carteiras/Relatorios/"
        f"relatorios.asp?Codigo_Formulario=11345"
    )

    try:
        resposta = sessao.get(url_relatorio, timeout=45)
        if resposta.status_code != 200 or "j_username" in resposta.text.lower():
            logger.error(
                "[RELATÓRIO] Falha ao acessar o relatório. Sessão inválida ou expirada."
            )
            return resultados

        sopa = BeautifulSoup(resposta.text, "html.parser")
        linhas = sopa.find_all("tr")

        for linha in linhas:
            colunas = linha.find_all("td")
            if not colunas or len(colunas) < 6:
                continue

            try:
                onclick_attr = linha.get("onclick", "")
                bloqueado = "alert(" in onclick_attr.lower()
                cpf_cliente = None

                if not bloqueado and "imprimeextrato" in onclick_attr.lower():
                    match_cpf = re.search(
                        r"imprimeExtrato\s*\([^,]+,\s*(\d+)",
                        onclick_attr,
                        re.IGNORECASE,
                    )
                    if match_cpf:
                        cpf_cliente = match_cpf.group(1).strip()

                txt_grupo = colunas[0].text.strip()
                txt_cota_versao = colunas[1].text.strip()
                nome_cliente = colunas[2].text.strip()
                txt_data_adesao = colunas[3].text.strip()
                txt_contemplacao = colunas[4].text.strip()
                txt_percentual = colunas[5].text.strip()

                grupo = limpar_inteiro(txt_grupo) or None
                cota = versao = None
                match_cota = re.search(r"(\d+)\s*-\s*(\d+)", txt_cota_versao)
                if match_cota:
                    cota = int(match_cota.group(1))
                    versao = int(match_cota.group(2))

                if grupo and cota and versao is not None:
                    item_validado = DesistenteSchema.criar_de_bruto(
                        grupo=grupo,
                        cota=cota,
                        versao=versao,
                        nome=nome_cliente,
                        txt_data_adesao=txt_data_adesao,
                        txt_contemplacao=txt_contemplacao,
                        txt_percentual=txt_percentual,
                        cpf=cpf_cliente,
                        bloqueado=bloqueado,
                    )
                    resultados.append(item_validado.para_dict())

            except Exception:  # pylint: disable=broad-exception-caught
                continue

        logger.info(
            "[RELATÓRIO] Extração concluída: %d desistentes mapeados.",
            len(resultados),
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("[RELATÓRIO] Falha de rede na extração do relatório: %s", e)

    return resultados
