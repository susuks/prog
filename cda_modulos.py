"""
Módulo Exclusivo do Controle de Adimplência (CDA) - HTTP Puro.

Extração direta de dados na intranet corporativa e orquestração de cache
inteligente em memória RAM para otimização de consultas O(1) no Google Sheets.
"""

import os
import io
import json
import re
import time
import logging
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import pymupdf
from PIL import Image
from pyzbar.pyzbar import decode, ZBarSymbol

from gerador_dados import limpar_inteiro, limpar_valor

logger = logging.getLogger("EnterpriseCRM")
ARQUIVO_ADIMPLENCIA = "files/adimplencia.json"
PAUSA_API_GOOGLE = 0.5


class GerenciadorCachePlanilhasCDA:
    """
    Gestor de estado em memória (RAM) exclusivo para o módulo CDA.

    Transforma pesquisas de complexidade O(N) nas colunas da API do Google Sheets
    em acessos diretos no dicionário de complexidade O(1).
    """

    def __init__(self):
        """Inicializa a estrutura interna do dicionário de mapeamento."""
        self._mapas = {}

    def _gerar_chave_cache(self, sheet, col_idx: int) -> str:
        """Gera um identificador único para indexar a aba e coluna."""
        return f"{sheet.spreadsheet.id}|{sheet.title}|{col_idx}"

    def _construir_mapa(self, sheet, col_idx: int) -> dict:
        """Faz o download em lote da coluna via API e processa a correspondência."""
        chave_cache = self._gerar_chave_cache(sheet, col_idx)
        mapa = {}
        try:
            valores = sheet.col_values(col_idx)
            for i, valor in enumerate(valores, start=1):
                contrato_str = str(valor).strip()
                if contrato_str:
                    mapa[contrato_str] = i

            self._mapas[chave_cache] = mapa
            logger.info(
                "    [CACHE CDA] Construído: Aba '%s' (Col %d) -> %d contratos mapeados.",
                sheet.title,
                col_idx,
                len(mapa),
            )
            return mapa
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("    [ERRO CACHE CDA] Falha ao descarregar coluna: %s", e)
            return {}

    def obter_linha(self, sheet, contrato: str, col_idx: int) -> int:
        """
        Recupera o índice da linha de um contrato em tempo constante.
        Aplica contingência (Cache Miss) e recarga automática caso necessário.
        """
        contrato_str = str(contrato).strip()
        chave_cache = self._gerar_chave_cache(sheet, col_idx)

        if chave_cache not in self._mapas:
            self._construir_mapa(sheet, col_idx)

        mapa_atual = self._mapas.get(chave_cache, {})

        if contrato_str in mapa_atual:
            return mapa_atual[contrato_str]

        logger.warning(
            "    [CACHE MISS CDA] Contrato %s ausente na aba '%s'. Invalidando...",
            contrato,
            sheet.title,
        )
        mapa_atualizado = self._construir_mapa(sheet, col_idx)

        if contrato_str in mapa_atualizado:
            logger.info(
                "    [CACHE RECOVERY] Contrato %s localizado após recarga.", contrato
            )
            return mapa_atualizado[contrato_str]

        logger.error(
            "    [FALHA CRÍTICA CDA] Contrato %s inexistente na aba '%s'.",
            contrato,
            sheet.title,
        )
        return None


# Instância exclusiva partilhada pelos métodos do módulo
cache_cda = GerenciadorCachePlanilhasCDA()


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
    """Formata e insere dados financeiros e cadastrais na matriz do Google Sheets."""
    linha = cache_cda.obter_linha(sheet, contrato, col_busca)
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
    """Realiza atualização unitária focada apenas na coluna de situação cadastral."""
    linha = cache_cda.obter_linha(sheet, contrato, col_busca)
    if linha:
        try:
            sheet.update_cell(linha, col_situacao, str(status_cliente))
            time.sleep(PAUSA_API_GOOGLE)
            return True
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return False


def validar_morte_sessao(texto_html: str) -> bool:
    """Audita blocos HTML retornados em busca de assinaturas de exclusão ASP."""
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
    """Algoritmo Otimizado de Extração por POST Fisiológico e resolução via BeautifulSoup."""
    grupo_str = str(info.get("grupo")).strip()
    cota_str = str(info.get("cota")).strip()
    cota_base_str = (
        cota_str.split("-", maxsplit=1)[0].strip() if "-" in cota_str else cota_str
    )

    grupo = int(grupo_str) if grupo_str.isdigit() else 0
    cota_base = int(cota_base_str) if cota_base_str.isdigit() else 0

    url_base = "https://intranet.consorciotradicao.com.br/autocred"
    url_pesq = (
        f"{url_base}/Attendance/searchCota.asp?"
        f"codigo_formulario_intranet=4&descricao_formulario_intranet=Consorciado"
    )

    payload_otimizado = {
        "Grupo": str(grupo) if grupo > 0 else "0",
        "Cota": str(cota_base) if cota_base > 0 else "0",
        "cgc_cpf_cliente": "0",
        "nome": "",
        "NumeroContrato": str(contrato),
        "event": "3",
    }

    try:
        res_post = sessao.post(
            url_pesq, data=payload_otimizado, timeout=15, allow_redirects=True
        )

        # BLINDAGEM DE REDE: Se o HTML for menor que 1000 caracteres.
        if res_post.status_code != 200 or len(res_post.text) < 1000:
            return {"encontrou": False, "motivo": "FALHA_REDE"}

        html_post = res_post.text.lower()

        if "j_username" in html_post or "acesso não permitido" in html_post:
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        # BLINDAGEM DE BANCO DE DADOS: Captura erros de timeout SQL do servidor.
        if "microsoft ole db" in html_post or "sql server" in html_post:
            return {"encontrou": False, "motivo": "FALHA_REDE"}

        url_painel_atual = res_post.url
        cpf_cliente = None

        if "datacota.asp" in url_painel_atual.lower() or "parcelas pagas" in html_post:
            html_painel = res_post.text
            match_cpf_url = re.search(r"cgc_cpf_cliente=(\d+)", url_painel_atual)
            if match_cpf_url:
                cpf_cliente = match_cpf_url.group(1)
        else:
            sopa = BeautifulSoup(res_post.text, "html.parser")
            linha_clicavel = sopa.find("tr", attrs={"onclick": True})

            if not linha_clicavel:
                return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

            onclick_txt = linha_clicavel["onclick"]
            match_url_clique = re.search(r"resultSearchCota\.asp\?[^']+", onclick_txt)

            if not match_url_clique:
                return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

            url_validacao = f"{url_base}/Attendance/{match_url_clique.group(0).strip()}"

            match_cpf = re.search(r"cgc_cpf_cliente=(\d+)", url_validacao)
            if match_cpf:
                cpf_cliente = match_cpf.group(1)

            if not cpf_cliente:
                cpf_limpo = (
                    re.sub(r"\D", "", str(info.get("cpf"))) if info.get("cpf") else None
                )
                cpf_cliente = cpf_limpo

            if not cpf_cliente:
                return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

            sessao.headers.update({"Referer": res_post.url})
            res_valida = sessao.get(url_validacao, timeout=15, allow_redirects=True)

            if "datacota.asp" in res_valida.url.lower():
                html_painel = res_valida.text
                url_painel_atual = res_valida.url
            else:
                url_painel_atual = (
                    f"{url_base}/Attendance/dataCota.asp?Codigo_Grupo={grupo}&"
                    f"Codigo_Cota={cota_base}&cgc_cpf_cliente={cpf_cliente}&tipo=0"
                )
                sessao.headers.update({"Referer": url_validacao})
                res_painel = sessao.get(url_painel_atual, timeout=15)
                html_painel = res_painel.text

            if (
                "j_username" in html_painel.lower()
                or "acesso não permitido" in html_painel.lower()
            ):
                return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        parcelas_pagas = 0
        versao_cota = None
        nome_cliente = None
        sopa_painel = BeautifulSoup(html_painel, "html.parser")

        td_header = sopa_painel.find(
            lambda tag: tag.name == "td"
            and "Grupo:" in tag.text
            and "Versão:" in tag.text
        )
        if td_header:
            strongs = td_header.find_all("strong")
            if len(strongs) >= 3:
                versao_cota = limpar_inteiro(strongs[2].text)

        td_nome = sopa_painel.find(
            lambda tag: tag.name == "td" and "Consorciado:" in tag.text
        )
        if td_nome:
            td_valor_nome = td_nome.find_next_sibling("td")
            if td_valor_nome:
                nome_cliente = td_valor_nome.text.strip()

        td_parcelas = sopa_painel.find(
            lambda tag: tag.name == "td" and "Parcelas Pagas:" in tag.text
        )
        if td_parcelas:
            td_valor = td_parcelas.find_next_sibling("td")
            if td_valor:
                parcelas_pagas = int(re.sub(r"\D", "", td_valor.text) or "0")

    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}

    desc = urllib.parse.quote("2ª Via Boleto", encoding="iso-8859-1")
    url_2via = (
        f"{url_base}/Attendance/emissSlip.asp?tipo=0&cgc_cpf_cliente={cpf_cliente}&"
        f"Codigo_Grupo={grupo}&Codigo_Cota={cota_base}&"
        f"codigo_formulario_filho_intranet=67&descricao_formulario_filho_intranet={desc}"
    )
    sessao.headers.update({"Referer": url_painel_atual})

    try:
        res_2via = sessao.get(url_2via, timeout=15)
        if (
            "j_username" in res_2via.text.lower()
            or "acesso não permitido" in res_2via.text.lower()
        ):
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        sopa2 = BeautifulSoup(res_2via.text, "html.parser")
        tabela_alvo = next(
            (tb for tb in sopa2.find_all("table") if "VENCIMENTO" in tb.text), None
        )

        boletos_atr, boletos_pend = 0, 0
        agora = datetime.now()

        if tabela_alvo:
            for linha in tabela_alvo.find_all("tr")[1:]:
                cols = linha.find_all("td")
                if len(cols) >= 3:
                    descricao = cols[1].text.strip().upper()
                    venc = cols[2].text.strip()
                    if ("PGTO PARC" in descricao or "PARCELA" in descricao) and venc:
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
            "nome": nome_cliente,
            "status_pagamento": status_pagamento,
            "parcelas_pagas": parcelas_pagas,
            "versao": versao_cota,
        }

    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}


def mapear_relatorios_via_http(sessao: requests.Session) -> dict:
    """
    Descarrega os relatórios da Autocred, estruturando o DOM HTML para
    guardar o Status e o Nome do Consorciado de forma mapeável na RAM.
    """
    mapa = {}
    alvos = {"1030": "CANCELADO", "11345": "DESISTENTE"}
    url_base = "https://intranet.consorciotradicao.com.br/autocred"

    url_rel_home = f"{url_base}/plugins/Relatorio_Carteiras/Relatorios/home.asp"
    try:
        sessao.get(url_rel_home, timeout=10)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.error("    [RELATÓRIOS] Falha ao acessar o menu pai dos relatórios.")
        return mapa

    for codigo_form, status in alvos.items():
        url_relatorio = (
            f"{url_base}/plugins/Relatorio_Carteiras/Relatorios/"
            f"relatorios.asp?Codigo_Formulario={codigo_form}"
        )
        try:
            resposta = sessao.get(url_relatorio, timeout=45)
            if (
                resposta.status_code == 200
                and "j_username" not in resposta.text.lower()
            ):
                sopa = BeautifulSoup(resposta.text, "html.parser")
                linhas_encontradas = 0

                for linha in sopa.find_all("tr"):
                    colunas = linha.find_all("td")

                    if colunas and len(colunas) >= 3:
                        txt_grupo = colunas[0].text.strip()
                        txt_cota_versao = colunas[1].text.strip()
                        nome_cliente = " ".join(colunas[2].text.upper().split())

                        g_limpo = re.sub(r"\D", "", txt_grupo)
                        if not g_limpo:
                            continue

                        match_cota = re.search(r"(\d+)\s*-\s*(\d+)", txt_cota_versao)

                        if match_cota:
                            g_id = int(g_limpo)
                            c_id = int(match_cota.group(1))
                            v_id = int(match_cota.group(2))
                            mapa[(g_id, c_id, v_id)] = (status, nome_cliente)
                            linhas_encontradas += 1

                logger.info(
                    "    [RELATÓRIOS] Form %s carregado. Encontrados %d registros.",
                    codigo_form,
                    linhas_encontradas,
                )
            else:
                logger.warning(
                    "    [RELATÓRIOS] Acesso rejeitado ao formulário %s.", codigo_form
                )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("    [RELATÓRIOS] Erro ao baixar %s: %s", codigo_form, e)

    return mapa


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


def extrair_pdf_boleto_memoria(
    sessao: requests.Session, cpf: str, grupo: str, cota: str
) -> dict:
    """
    Extração Pesada (4-7s): Baixa o PDF em memória, renderiza em imagem e extrai o QR Code e I25.
    Para ser executada isoladamente em background.
    """
    resultado = {"qr_code": "", "codigo_barras": ""}
    url_base = "https://intranet.consorciotradicao.com.br/autocred"

    try:
        desc = urllib.parse.quote("2ª Via Boleto", encoding="iso-8859-1")
        url_2via = (
            f"{url_base}/Attendance/emissSlip.asp?tipo=0&cgc_cpf_cliente={cpf}&"
            f"Codigo_Grupo={grupo}&Codigo_Cota={cota}&"
            f"codigo_formulario_filho_intranet=67&descricao_formulario_filho_intranet={desc}"
        )
        res_2via = sessao.get(url_2via, timeout=10)

        sopa_2via = BeautifulSoup(res_2via.text, "html.parser")
        link_boleto = sopa_2via.find("a", string=re.compile("PGTO PARC", re.IGNORECASE))

        if link_boleto and link_boleto.has_attr("onclick"):
            args = re.findall(r"'(.*?)'", link_boleto["onclick"])
            if len(args) >= 9:
                input_vencto = sopa_2via.find("input", {"name": "venctoinput"})
                data_hoje = (
                    input_vencto["value"]
                    if input_vencto and input_vencto.has_attr("value")
                    else args[2]
                )

                payload_boleto = {
                    "numero_aviso": args[1],
                    "vencto": args[2],
                    "venctoinput": data_hoje,
                    "valor_total": args[7],
                    "descricao": args[3],
                    "codigo_grupo": args[4],
                    "codigo_cota": args[5],
                    "codigo_movimento": args[6],
                    "codigo_agente": args[0],
                    "desc_pagamento": args[8],
                    "msg_dbt_apenas_parc_antes_venc": args[9] if len(args) > 9 else "N",
                    "Data_Limite_Vencimento_Boleto": data_hoje,
                    "FlagAlterarData": "N",
                    "sn_emite_boleto_pix": "S",
                    "codigo_origem_recurso": "0",
                }

                url_post_slip = f"{url_base}/Slip/Slip.asp"
                sessao.headers.update({"Referer": url_2via})
                res_pdf = sessao.post(url_post_slip, data=payload_boleto, timeout=15)

                if res_pdf.status_code == 200 and b"%PDF" in res_pdf.content[:10]:
                    documento = pymupdf.open(stream=res_pdf.content, filetype="pdf")

                    for pagina in documento:
                        pix = pagina.get_pixmap(dpi=300)
                        img_bytes = pix.tobytes("png")
                        imagem = Image.open(io.BytesIO(img_bytes))

                        # Filtro rigoroso para QRCODE e I25, calando warnings de outros formatos do pyzbar
                        codigos = decode(
                            imagem, symbols=[ZBarSymbol.QRCODE, ZBarSymbol.I25]
                        )

                        for codigo in codigos:
                            texto_decodificado = codigo.data.decode("utf-8")
                            if codigo.type == "QRCODE":
                                resultado["qr_code"] = texto_decodificado
                            elif codigo.type == "I25":
                                resultado["codigo_barras"] = texto_decodificado

                        if resultado["qr_code"] and resultado["codigo_barras"]:
                            break
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Falha ao processar boleto em background: %s", e)

    return resultado
