"""
Módulo Exclusivo do Controle de Adimplência (CDA) - HTTP Puro (Reconstrução Zero).

Extração direta de dados na intranet corporativa e orquestração de cache
inteligente em memória RAM para otimização de consultas O(1) no Google Sheets.
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
        """
        Gera um identificador criptográfico único para indexar a aba e coluna.
        """
        return f"{sheet.spreadsheet.id}|{sheet.title}|{col_idx}"

    def _construir_mapa(self, sheet, col_idx: int) -> dict:
        """
        Faz o download em lote da coluna via API e processa a correspondência.
        """
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

        Aplica contingência (Cache Miss) e recarga automática caso a
        tabela do lado do servidor possua novos contratos adicionados.
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
    """
    Lê e processa o ficheiro JSON que hospeda o estado estrutural das auditorias.
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
    Persiste os dados de auditoria em memória física (JSON).
    """
    with open(ARQUIVO_ADIMPLENCIA, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)


def migrar_para_adimplencia(contrato: str, info_pendente: dict, grupo: str, cota: str):
    """
    Efetua o transbordo estrutural de um cliente pago para a fila longa.
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
    Formata e insere dados financeiros e cadastrais na matriz das planilhas Google.
    """
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
    """
    Realiza atualização unitária focada na condição cadastral (Inacessível/Cancelado).
    """
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
    """
    Auditoria de blocos HTML retornado, em busca de assinaturas de exclusão ASP.
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
    """
    Algoritmo de extração estabilizado via POST Dinâmico.
    Descarrega o formulário ASP original, mapeia os campos ocultos (Tokens/Estado)
    e injeta os parâmetros de forma orgânica para contornar bloqueios VBScript.
    """
    grupo_str = str(info.get("grupo")).strip()
    cota_str = str(info.get("cota")).strip()
    cota_base_str = (
        cota_str.split("-", maxsplit=1)[0].strip() if "-" in cota_str else cota_str
    )

    grupo = int(grupo_str) if grupo_str.isdigit() else 0
    cota_base = int(cota_base_str) if cota_base_str.isdigit() else 0

    url_base = "https://intranet.consorciotradicao.com.br/autocred"
    url_left = f"{url_base}/LeftFrame.asp?codigo_modulo=AG"
    url_pesquisa = (
        f"{url_base}/Attendance/searchCota.asp?"
        f"codigo_formulario_intranet=4&descricao_formulario_intranet=Consorciado"
    )

    # 1. WARMUP
    sessao.headers.update({"Referer": url_left})
    try:
        res_pre = sessao.get(url_left, timeout=10)
        if (
            "j_username" in res_pre.text.lower()
            or "acesso não permitido" in res_pre.text.lower()
        ):
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}
    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}

    # 2. RASPAGEM DO FORMULÁRIO ORIGINAL (Prevenção do Erro VBScript 800a000d)
    try:
        res_form = sessao.get(url_pesquisa, timeout=15)
        html_form = res_form.text.lower()

        if "j_username" in html_form or "acesso não permitido" in html_form:
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        sopa_form = BeautifulSoup(res_form.text, "html.parser")
        form_busca = sopa_form.find("form")

        if not form_busca:
            return {"encontrou": False, "motivo": "NAO_ENCONTRADO"}

        # Extração de todos os parâmetros exigidos pelo ASP
        payload_dinamico = {}
        for campo in form_busca.find_all(["input", "select"]):
            nome = campo.get("name")
            if not nome:
                continue

            if campo.name == "select":
                opcao = campo.find("option", selected=True)
                if opcao:
                    payload_dinamico[nome] = opcao.get("value", "")
                else:
                    primeira = campo.find("option")
                    payload_dinamico[nome] = (
                        primeira.get("value", "") if primeira else ""
                    )
            else:
                payload_dinamico[nome] = campo.get("value", "")

        # Injeção cirúrgica do contrato mantendo o ecosistema do formulário intacto
        payload_dinamico["NumeroContrato"] = contrato

    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}

    # 3. PESQUISA POST (Payload Orgânico via Dicionário)
    try:
        # A biblioteca requests codifica o dicionário adequadamente, prevenindo strings anómalas
        res_post = sessao.post(
            url_pesquisa, data=payload_dinamico, timeout=15, allow_redirects=True
        )
        html_post = res_post.text.lower()

        if "j_username" in html_post or "acesso não permitido" in html_post:
            return {"encontrou": False, "motivo": "SESSAO_CAIU"}

        url_painel_atual = res_post.url
        cpf_cliente = None

        # 4. TRATAMENTO DO RETORNO (Tabela ou Redirecionamento Direto)
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
                cpf_cliente = (
                    re.sub(r"\D", "", str(info.get("cpf"))) if info.get("cpf") else None
                )

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

        # 5. EXTRAÇÃO DO PAINEL PRINCIPAL
        parcelas_pagas = 0
        sopa_painel = BeautifulSoup(html_painel, "html.parser")
        td_parcelas = sopa_painel.find(
            lambda tag: tag.name == "td" and "Parcelas Pagas:" in tag.text
        )
        if td_parcelas:
            td_valor = td_parcelas.find_next_sibling("td")
            if td_valor:
                parcelas_pagas = int(re.sub(r"\D", "", td_valor.text) or "0")

    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}

    # 6. 2ª VIA DO BOLETO (Para o Status)
    desc = urllib.parse.quote("2ª Via Boleto", encoding="iso-8859-1")
    url_2via = (
        f"{url_base}/Attendance/emissSlip.asp?"
        f"tipo=0&cgc_cpf_cliente={cpf_cliente}&Codigo_Grupo={grupo}&Codigo_Cota={cota_base}&"
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

    except Exception:  # pylint: disable=broad-exception-caught
        return {"encontrou": False, "motivo": "FALHA_REDE"}


def mapear_relatorios_via_http(sessao: requests.Session) -> dict:
    """
    Carrega o relatório financeiro consolidado de abstenção cadastral.

    Acessa os diretórios de Inativos (Cancelados/Desistentes) e devolve um
    dicionário estruturado mapeando grupos e cotas às suas respetivas falhas.
    """
    mapa = {}
    alvos = {"1030": "CANCELADO", "11345": "DESISTENTE"}
    url_base = "https://intranet.consorciotradicao.com.br/autocred"
    url_left = f"{url_base}/LeftFrame.asp?codigo_modulo=AG"
    sessao.headers.update({"Referer": url_left})

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
