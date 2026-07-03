"""
Módulo Gerenciador de Dados e Arquivos.

Responsável por centralizar a manipulação de dados estáticos, configurações
de ambiente, formatação de textos (Regex), operações de Entrada/Saída (I/O) em
ficheiros locais (JSON, CSV) e integração com a API do Google Sheets.
"""

import os
import json
import re
import time
import logging
from datetime import datetime, timedelta, timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from bs4 import BeautifulSoup

logger = logging.getLogger("EnterpriseDados")

# ============================================================================
# CONSTANTES E CAMINHOS DE ARQUIVOS
# ============================================================================
ARQUIVO_FILA = "files/fila_vendas.csv"
ARQUIVO_EM_PROCESSAMENTO = "files/temp_processando.csv"
ARQUIVO_HISTORICO_SUCESSO = "files/historico_concluidos.csv"
ARQUIVO_PENDENTES = "files/pendentes_reanalise.json"
ARQUIVO_ADIMPLENCIA = "files/adimplencia.json"
ARQUIVO_CACHE_PLANILHAS = "files/cache_planilhas.json"
ARQUIVO_CONFIG = "files/config.txt"

# Cache Frequente e Diário (Relatórios Offline)
CACHE_CANCELADOS = "files/cache_cancelados.html"
CACHE_DESISTENTES = "files/cache_desistentes.html"
CACHE_CANCELADOS_DIARIO = "files/cache_cancelados_diario.html"
CACHE_DESISTENTES_DIARIO = "files/cache_desistentes_diario.html"

MAX_TENTATIVAS = 10000
TEMPO_INATIVIDADE_MAXIMO = 300

# Freio de segurança da API do Google (Reduzir para 0.0 após aumento de cota no Cloud)
PAUSA_API_GOOGLE = 1.0


# ============================================================================
# CONFIGURAÇÕES E CREDENCIAIS
# ============================================================================
def carregar_configuracoes() -> dict:
    """
    Lê o ficheiro de configuração local (config.txt) e extrai credenciais,
    parâmetros de sistema e tempos de intervalo para as rotinas de automação.
    """
    config = {
        "MATRICULA": "",
        "SENHA": "",
        "PREFIXO_PLANILHA": "Controle de Vendas -- ",
        "ANO_GERAL": "2026",
        "MODO_DESKTOP": False,
        "INTERVALO_SINC_RELATORIOS": 3600,
        "INTERVALO_REANALISE_ADIMPLENCIA": 14400,
        "LIMITE_DIAS_DESATIVACAO": 45,
    }

    if not os.path.exists(ARQUIVO_CONFIG):
        return config

    try:
        with open(ARQUIVO_CONFIG, "r", encoding="utf-8") as f:
            for linha in f:
                if "=" in linha:
                    chave, valor = linha.split("=", 1)
                    chave = chave.strip()
                    valor_limpo = valor.replace("\n", "").replace("\r", "")

                    if chave == "MODO_DESKTOP":
                        config[chave] = valor_limpo.strip().lower() in [
                            "true",
                            "1",
                            "sim",
                            "v",
                        ]
                    elif chave in [
                        "INTERVALO_SINC_RELATORIOS",
                        "INTERVALO_REANALISE_ADIMPLENCIA",
                        "LIMITE_DIAS_DESATIVACAO",
                    ]:
                        try:
                            config[chave] = int(valor_limpo)
                        except ValueError:
                            pass
                    else:
                        config[chave] = valor_limpo
        return config
    except OSError:
        return config


def obter_mes_utc4() -> str:
    """Calcula o fuso horário UTC-4 e retorna o mês atual em português."""
    meses_pt = {
        1: "JANEIRO",
        2: "FEVEREIRO ",
        3: "MARÇO",
        4: "ABRIL",
        5: "MAIO",
        6: "JUNHO",
        7: "JULHO",
        8: "AGOSTO",
        9: "SETEMBRO",
        10: "OUTUBRO ",
        11: "NOVEMBRO",
        12: "DEZEMBRO",
    }
    fuso_utc4 = timezone(timedelta(hours=-4))
    agora = datetime.now(fuso_utc4)
    return meses_pt[agora.month]


CONFIG = carregar_configuracoes()
USUARIO_LOGIN = CONFIG.get("MATRICULA")
SENHA_LOGIN = CONFIG.get("SENHA")
PREFIXO_PLANILHA = CONFIG.get("PREFIXO_PLANILHA")
NOME_ABA_GERAL = CONFIG.get("ANO_GERAL")
MODO_DESKTOP = CONFIG.get("MODO_DESKTOP")


# ============================================================================
# PROCESSAMENTO DE DADOS (REGEX) E MANIPULAÇÃO JSON/CSV
# ============================================================================
def limpar_inteiro(texto: str) -> int:
    """Extrai todos os dígitos numéricos e converte para inteiro."""
    try:
        numeros = re.sub(r"\D", "", str(texto))
        return int(numeros) if numeros else 0
    except (ValueError, TypeError):
        return 0


def limpar_valor(texto: str) -> float:
    """Converte um valor monetário no formato brasileiro para float."""
    try:
        match = re.search(r"([\d\.]+,\d{2})", str(texto))
        if match:
            valor_texto = match.group(1).replace(".", "").replace(",", ".")
            return float(valor_texto)
        return 0.00
    except (ValueError, TypeError):
        return 0.00


def carregar_pendentes() -> dict:
    """Lê o ficheiro JSON de contratos pendentes de reanálise (1ª parcela)."""
    if os.path.exists(ARQUIVO_PENDENTES):
        try:
            with open(ARQUIVO_PENDENTES, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # pylint: disable=broad-exception-caught
            return {}
    return {}


def salvar_pendentes(dados: dict) -> None:
    """Grava as alterações no ficheiro JSON de contratos pendentes."""
    with open(ARQUIVO_PENDENTES, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)


def verificar_contrato_registrado(contrato: str) -> bool:
    """Garante que um contrato não seja processado em duplicidade."""
    pendentes = carregar_pendentes()
    if str(contrato) in pendentes:
        return True

    if os.path.exists(ARQUIVO_HISTORICO_SUCESSO):
        try:
            with open(ARQUIVO_HISTORICO_SUCESSO, "r", encoding="utf-8") as f:
                for linha in f:
                    if linha.startswith(f"{contrato},"):
                        return True
        except OSError:
            pass
    return False


def adicionar_para_reanalise(
    contrato: str,
    vendedor_nome: str,
    vendedor_tel: str,
    nome_planilha: str,
    origem: str,
    dados_completos: dict,
    aba_original: str,
) -> None:
    """Adiciona um novo contrato à fila de reanálise de primeira parcela."""
    pendentes = carregar_pendentes()
    pendentes[contrato] = {
        "vendedor_nome": vendedor_nome,
        "vendedor_tel": vendedor_tel,
        "nome_planilha": nome_planilha,
        "origem": origem,
        "dados_originais": dados_completos,
        "tentativas": 0,
        "ultima_verificacao": 0,
        "data_inclusao": time.time(),
        "aba_original": aba_original,
    }
    salvar_pendentes(pendentes)


def salvar_historico_concluido(
    contrato: str,
    nome_planilha: str,
    vendedor_nome: str,
    vendedor_tel: str,
    status_pag: str,
) -> None:
    """Regista localmente num CSV o histórico de contratos com 1ª parcela paga."""
    existe = os.path.exists(ARQUIVO_HISTORICO_SUCESSO)
    try:
        with open(ARQUIVO_HISTORICO_SUCESSO, "a", encoding="utf-8") as f:
            if not existe:
                cabecalho = (
                    "contrato,planilha_destino,data_registro,"
                    "vendedor_nome,vendedor_tel,status_pagamento\n"
                )
                f.write(cabecalho)

            data_hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            safe_planilha = str(nome_planilha).replace(",", ".")
            safe_nome = str(vendedor_nome).replace(",", ".")

            # Quebra de linha aplicada para respeitar o limite de 100 caracteres do PEP 8
            linha = (
                f"{contrato},{safe_planilha},{data_hora},"
                f"{safe_nome},{vendedor_tel},{status_pag}\n"
            )
            f.write(linha)
    except OSError as e:
        logger.error("   [ERRO HISTÓRICO] Falha ao gravar log local: %s", e)


# ============================================================================
# MÓDULO CRM: GESTÃO DE ADIMPLÊNCIA E CACHE OFFLINE
# ============================================================================
def carregar_adimplencia() -> dict:
    """Carrega a base de clientes do CRM."""
    if os.path.exists(ARQUIVO_ADIMPLENCIA):
        try:
            with open(ARQUIVO_ADIMPLENCIA, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # pylint: disable=broad-exception-caught
            return {}
    return {}


def salvar_adimplencia(dados: dict) -> None:
    """Persiste o banco de dados do CRM em disco."""
    with open(ARQUIVO_ADIMPLENCIA, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)


def migrar_para_adimplencia(contrato: str, info_pendente: dict, grupo: str, cota: str):
    """
    Transfere o cliente para o CRM e inicializa a infraestrutura de Cache Delta.
    """
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
            "ultimo_status": None,  # Delta Cache: Status Financeiro
            "ultimas_parcelas": 0,  # Delta Cache: Pagamentos
        }
        salvar_adimplencia(bd)


def buscar_status_offline_regex(grupo: str, cota: str, cota_versao: str = None) -> str:
    """Parsing estrutural (BeautifulSoup) para validação exata do cliente em offline."""
    if not cota_versao:
        return ""

    grupo_alvo = limpar_inteiro(grupo)
    cota_base_alvo = limpar_inteiro(str(cota).split("-", maxsplit=1)[0])

    versao_alvo = None
    match_v = re.search(r"(\d+)\s*-\s*(\d+)", str(cota_versao))
    if match_v:
        versao_alvo = limpar_inteiro(match_v.group(2))
    else:
        return ""

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

                            if match_cota_html:
                                cota_base_html = limpar_inteiro(
                                    match_cota_html.group(1)
                                )
                                versao_html = limpar_inteiro(match_cota_html.group(2))

                                if (
                                    grupo_html == grupo_alvo
                                    and cota_base_html == cota_base_alvo
                                ):
                                    if versao_html == versao_alvo:
                                        return status
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("Falha no Parsing do ficheiro %s: %s", arquivo, e)
    return ""


# ============================================================================
# COMUNICAÇÃO COM GOOGLE SHEETS
# ============================================================================
def conectar_google_sheets(nome_planilha: str, aba: str):
    """Estabelece ligação à API do Google Sheets e devolve a aba requisitada."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            "files/credentials.json", scope
        )
        cliente = gspread.authorize(creds)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("   [ERRO CREDENCIAIS] Falha no Google Sheets: %s", e)
        return None

    cache = {}
    if os.path.exists(ARQUIVO_CACHE_PLANILHAS):
        try:
            with open(ARQUIVO_CACHE_PLANILHAS, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    planilha = None
    if nome_planilha in cache:
        try:
            planilha = cliente.open_by_key(cache[nome_planilha])
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    if not planilha:
        try:
            planilha = cliente.open(nome_planilha)
            cache[nome_planilha] = planilha.id
            with open(ARQUIVO_CACHE_PLANILHAS, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=4)
        except Exception:  # pylint: disable=broad-exception-caught
            return None

    try:
        return planilha.worksheet(aba)
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def encontrar_proxima_linha_vazia(sheet, start_row: int, check_col: int) -> int:
    """Localiza a primeira linha vazia numa coluna específica da folha."""
    coluna_alvo = sheet.col_values(check_col)
    if len(coluna_alvo) < start_row:
        return start_row

    for i in range(start_row - 1, len(coluna_alvo) + 20):
        try:
            if i >= len(coluna_alvo) or not coluna_alvo[i]:
                return i + 1
        except Exception:  # pylint: disable=broad-exception-caught
            return i + 1
    return start_row


def encontrar_linha_do_contrato(sheet, contrato: str, col_idx: int) -> int:
    """Localiza a linha em que um determinado contrato está registado na folha."""
    try:
        valores = sheet.col_values(col_idx)
        for i, valor in enumerate(valores, start=1):
            if str(contrato).strip() == str(valor).strip():
                return i
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("   [ERRO BUSCA PLANILHA] Falha ao localizar cota: %s", e)
    return 0


def atualizar_planilha_vendedor(
    sheet, row_csv: dict, dados_site: dict, contrato: str
) -> str:
    """Regista os dados cadastrais e financeiros do contrato na folha do vendedor."""
    linha = encontrar_proxima_linha_vazia(sheet, start_row=12, check_col=4)
    status_pag = "1º Parcela Paga" if dados_site.get("pago") else ""

    telefone_limpo = re.sub(r"\D", "", str(dados_site.get("telefone", "")))
    if telefone_limpo and not telefone_limpo.startswith("55"):
        telefone_limpo = f"55{telefone_limpo}"

    dados_cadastrais = [
        str(dados_site.get("data_venda", "")),
        str(dados_site.get("nome", "")),
        telefone_limpo,
        "",
        str(row_csv.get("origem", "")),
    ]

    lance_val = 0.00
    try:
        str_lance = str(row_csv.get("lance livre", 0))
        limpo = str_lance.replace("R$", "").replace(".", "").replace(",", ".").strip()
        lance_val = float(limpo)
    except (ValueError, TypeError):
        pass

    cota_planilha = str(dados_site.get("cota", "")).split("-", maxsplit=1)[0].strip()

    dados_financeiros = [
        dados_site.get("credito", 0.00),
        lance_val,
        "",
        str(contrato),
        str(dados_site.get("grupo", "")),
        cota_planilha,
        str(dados_site.get("estado", "")),
    ]

    sheet.update_cell(linha, 2, status_pag)
    sheet.update(
        range_name=f"D{linha}:H{linha}",
        values=[dados_cadastrais],
        value_input_option="USER_ENTERED",
    )
    sheet.update(
        range_name=f"J{linha}:P{linha}",
        values=[dados_financeiros],
        value_input_option="USER_ENTERED",
    )
    return status_pag


def atualizar_planilha_geral(
    sheet, row_csv: dict, dados_site: dict, contrato: str
) -> str:
    """Regista os dados completos do contrato na folha GERAL (Mensal ou Anual)."""
    linha = encontrar_proxima_linha_vazia(sheet, start_row=7, check_col=3)
    status_pag = "1º Parcela Paga" if dados_site.get("pago") else ""

    telefone_limpo = re.sub(r"\D", "", str(dados_site.get("telefone", "")))
    if telefone_limpo and not telefone_limpo.startswith("55"):
        telefone_limpo = f"55{telefone_limpo}"

    dados_cadastrais = [
        str(dados_site.get("data_venda", "")),
        str(dados_site.get("nome", "")),
        telefone_limpo,
        "",
        str(row_csv.get("origem", "")),
    ]

    lance_val = 0.00
    try:
        str_lance = str(row_csv.get("lance livre", 0))
        limpo = str_lance.replace("R$", "").replace(".", "").replace(",", ".").strip()
        lance_val = float(limpo)
    except (ValueError, TypeError):
        pass

    fuso_utc4 = timezone(timedelta(hours=-4))
    data_registro_atual = datetime.now(fuso_utc4).strftime("%d/%m/%Y")
    cota_planilha = str(dados_site.get("cota", "")).split("-", maxsplit=1)[0].strip()

    dados_financeiros = [
        dados_site.get("credito", 0.00),
        lance_val,
        "",
        str(contrato),
        str(dados_site.get("grupo", "")),
        cota_planilha,
        str(dados_site.get("estado", "")),
        str(dados_site.get("cpf", "")),
        str(row_csv.get("vendedor", "")),
        data_registro_atual,
    ]

    sheet.update_cell(linha, 1, status_pag)
    sheet.update(
        range_name=f"C{linha}:G{linha}",
        values=[dados_cadastrais],
        value_input_option="USER_ENTERED",
    )
    sheet.update(
        range_name=f"I{linha}:R{linha}",
        values=[dados_financeiros],
        value_input_option="USER_ENTERED",
    )
    return status_pag


def registrar_adimplencia_planilhas(
    sheet,
    contrato: str,
    status_pgto: str,
    parcelas: int,
    status_cliente: str,
    col_busca: int,
):
    """
    Submete a atualização do cliente (Delta) nas extremidades financeiras da folha de cálculo.
    """
    linha = encontrar_linha_do_contrato(sheet, contrato, col_busca)
    if linha:
        try:
            dados_adimplencia = [str(status_pgto), int(parcelas), str(status_cliente)]
            sheet.update(
                range_name=f"S{linha}:U{linha}",
                values=[dados_adimplencia],
                value_input_option="USER_ENTERED",
            )
            time.sleep(PAUSA_API_GOOGLE)
            return True
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Falha ao atualizar colunas no contrato %s: %s", contrato, e)
    return False


def registrar_apenas_situacao_cliente(
    sheet, contrato: str, status_cliente: str, col_busca: int
):
    """Atualiza a coluna de Situação (ex: 'Inacessível') sem alterar dados financeiros."""
    linha = encontrar_linha_do_contrato(sheet, contrato, col_busca)
    if linha:
        try:
            sheet.update_cell(linha, 21, str(status_cliente))
            time.sleep(PAUSA_API_GOOGLE)
            return True
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Falha ao atualizar Situação no contrato %s: %s", contrato, e)
    return False
