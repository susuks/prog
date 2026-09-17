"""
Módulo Gerenciador de Dados e Arquivos.

Responsável por centralizar toda a manipulação de dados estáticos, configurações
de ambiente, formatação de textos (Regex), operações de Entrada/Saída (I/O) em
arquivos locais (JSON, CSV) e integração com a API do Google Sheets.
"""

import os
import json
import re
import time
import logging
from datetime import datetime, timedelta, timezone
import gspread
from oauth2client.service_account import ServiceAccountCredentials

logger = logging.getLogger("EnterpriseBot")

# ============================================================================
# CONSTANTES E CAMINHOS DE ARQUIVOS
# ============================================================================
ARQUIVO_FILA = "files/fila_vendas.csv"
ARQUIVO_EM_PROCESSAMENTO = "files/temp_processando.csv"
ARQUIVO_HISTORICO_SUCESSO = "files/historico_concluidos.csv"
ARQUIVO_ADIMPLENCIA = "files/adimplencia.json"
ARQUIVO_EXPIRADOS = "files/expirados.json"
ARQUIVO_CACHE_PLANILHAS = "files/cache_planilhas.json"
ARQUIVO_CONFIG = "files/config.txt"

MAX_TENTATIVAS = 10000
TEMPO_INATIVIDADE_MAXIMO = 300


# ============================================================================
# CONFIGURAÇÕES E CREDENCIAIS
# ============================================================================
def carregar_configuracoes() -> dict:
    """
    Lê o arquivo de configuração local e extrai credenciais e parâmetros.
    """
    config = {
        "MATRICULA": "",
        "SENHA": "",
        "PREFIXO_PLANILHA": "Controle de Vendas -- ",
        "ANO_GERAL": "2026",
        "MODO_DESKTOP": False,
    }

    if not os.path.exists(ARQUIVO_CONFIG):
        return config

    with open(ARQUIVO_CONFIG, "r", encoding="utf-8") as f:
        for linha in f:
            if "=" in linha:
                chave, valor = linha.split("=", 1)
                chave = chave.strip()
                valor_limpo = valor.replace("\n", "").replace("\r", "")

                if chave == "MODO_DESKTOP":
                    config[chave] = valor_limpo.lower() in [
                        "true",
                        "1",
                        "sim",
                        "v",
                    ]
                else:
                    config[chave] = valor_limpo
    return config


def obter_mes_utc4() -> str:
    """
    Calcula a data e hora atual no fuso horário UTC-4 e retorna o mês correspondente.
    Avaliação dinâmica para evitar o congelamento da variável em viradas de mês.
    """
    meses_pt = {
        1: "JANEIRO",
        2: "FEVEREIRO",
        3: "MARÇO",
        4: "ABRIL",
        5: "MAIO",
        6: "JUNHO",
        7: "JULHO",
        8: "AGOSTO",
        9: "SETEMBRO",
        10: "OUTUBRO",
        11: "NOVEMBRO",
        12: "DEZEMBRO",
    }
    fuso_utc4 = timezone(timedelta(hours=-4))
    agora = datetime.now(fuso_utc4)
    return meses_pt[agora.month]


CONFIG = carregar_configuracoes()
USUARIO_LOGIN = CONFIG.get("MATRICULA")
SENHA_LOGIN = CONFIG.get("SENHA")
TOKEN_API = CONFIG.get("TOKEN_API")
PREFIXO_PLANILHA = CONFIG.get("PREFIXO_PLANILHA")
NOME_ABA_GERAL = CONFIG.get("ANO_GERAL")
MODO_DESKTOP = CONFIG.get("MODO_DESKTOP")


# ============================================================================
# PROCESSAMENTO DE DADOS E FORMATAÇÃO (REGEX)
# ============================================================================
def limpar_inteiro(texto: str) -> int:
    """
    Extrai todos os dígitos numéricos de uma string e os converte para inteiro.
    """
    try:
        numeros = re.sub(r"\D", "", str(texto))
        return int(numeros) if numeros else 0
    except (ValueError, TypeError):
        return 0


def limpar_valor(texto: str) -> float:
    """
    Converte uma string de valor monetário brasileiro para o formato float.
    """
    try:
        match = re.search(r"([\d\.]+,\d{2})", str(texto))
        if match:
            valor_limpo = match.group(1)
            valor_float = float(valor_limpo.replace(".", "").replace(",", "."))
            return valor_float
        return 0.00
    except (ValueError, TypeError):
        return 0.00


# ============================================================================
# MANIPULAÇÃO DE ARQUIVOS LOCAIS UNIFICADA (JSON / CSV)
# ============================================================================
def carregar_adimplencia() -> dict:
    """Carrega o arquivo JSON unificado de auditorias do sistema."""
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


def carregar_expirados() -> dict:
    """Carrega o arquivo JSON de vendas perdidas (Não pagaram 1ª parcela em 30 dias)."""
    if os.path.exists(ARQUIVO_EXPIRADOS):
        try:
            with open(ARQUIVO_EXPIRADOS, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def salvar_expirados(dados: dict) -> None:
    """Grava as alterações no arquivo JSON de contratos expirados."""
    temp_file = ARQUIVO_EXPIRADOS + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)
    os.replace(temp_file, ARQUIVO_EXPIRADOS)


def verificar_contrato_registrado(contrato: str) -> bool:
    """
    Consulta a memória local estrita (Adimplência, Expirados e Histórico)
    para evitar duplicidade absoluta no sistema.
    """
    if str(contrato) in carregar_adimplencia():
        return True

    if str(contrato) in carregar_expirados():
        return True

    if os.path.exists(ARQUIVO_HISTORICO_SUCESSO):
        try:
            with open(ARQUIVO_HISTORICO_SUCESSO, "r", encoding="utf-8") as f:
                if f"{contrato}," in f.read():
                    return True
        except OSError:
            pass

    return False


def registrar_contrato_unificado(
    contrato: str,
    vendedor_nome: str,
    nome_planilha: str,
    vendedor_tel: str,
    origem: str,
    dados_site: dict,
    aba_original: str,
) -> None:
    """
    Substitui as antigas filas pendentes e migrações. Registra o contrato
    diretamente no JSON definitivo, formatando matematicamente Cota e Versão.
    """
    adimplencia = carregar_adimplencia()

    cpf_cru = dados_site.get("cpf")
    versao = dados_site.get("versao")

    adimplencia[str(contrato)] = {
        "vendedor_nome": vendedor_nome,
        "vendedor_tel": vendedor_tel,
        "origem": origem,
        "nome_planilha": nome_planilha,
        "aba_original": aba_original,
        "grupo": limpar_inteiro(dados_site.get("grupo", "")),
        "cota": limpar_inteiro(dados_site.get("cota", "")),
        "versao": limpar_inteiro(versao) if versao is not None else None,
        "nome": dados_site.get("nome", ""),
        "cpf": re.sub(r"\D", "", str(cpf_cru)) if cpf_cru else None,
        "primeira_parcela_paga": dados_site.get("pago", False),
        "data_inclusao": time.time(),
        "monitorar": True,
        "ultima_verificacao": 0,
        "data_limbo": None,
        "ultimo_status": "PAGO" if dados_site.get("pago") else "PENDENTE",
        "ultimas_parcelas": 1 if dados_site.get("pago") else 0,
    }
    salvar_adimplencia(adimplencia)
    logger.info(
        "   [BASE UNIFICADA] Contrato %s registrado com sucesso (Aba: %s).",
        contrato,
        aba_original,
    )


def salvar_historico_concluido(
    contrato: str,
    nome_planilha: str,
    vendedor_nome: str,
    vendedor_tel: str,
    status_pag: str,
) -> None:
    """
    Grava os metadados de um contrato processado definitivamente em log CSV.
    """
    existe = os.path.exists(ARQUIVO_HISTORICO_SUCESSO)
    try:
        with open(ARQUIVO_HISTORICO_SUCESSO, "a", encoding="utf-8") as f:
            if not existe:
                f.write(
                    "contrato,planilha_destino,data_registro,vendedor_nome,vendedor_tel,status\n"
                )

            data_hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            safe_planilha = str(nome_planilha).replace(",", ".")
            safe_nome = str(vendedor_nome).replace(",", ".")

            linha = (
                f"{contrato},{safe_planilha},{data_hora},"
                f"{safe_nome},{vendedor_tel},{status_pag}\n"
            )
            f.write(linha)
    except OSError as e:
        logger.error("   [ERRO HISTÓRICO] Falha ao salvar no histórico: %s", e)


# ============================================================================
# COMUNICAÇÃO COM GOOGLE SHEETS E CACHE DE IDs
# ============================================================================
_ESTADO_CONEXAO = {"cliente_gspread": None}


def obter_cliente_gspread():
    """Garante que a autenticação no Google é feita apenas 1 vez por sessão."""
    if _ESTADO_CONEXAO["cliente_gspread"] is None:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        try:
            creds = ServiceAccountCredentials.from_json_keyfile_name(
                "files/credentials.json", scope
            )
            _ESTADO_CONEXAO["cliente_gspread"] = gspread.authorize(creds)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(
                "   [ERRO CREDENCIAIS] Falha ao autorizar Google Sheets: %s", e
            )

    return _ESTADO_CONEXAO["cliente_gspread"]


def conectar_google_sheets(nome_planilha: str, aba: str):
    """
    Estabelece uma conexão com uma planilha utilizando um cliente global persistente.
    """
    cliente = obter_cliente_gspread()
    if not cliente:
        return None

    cache = {}
    if os.path.exists(ARQUIVO_CACHE_PLANILHAS):
        try:
            with open(ARQUIVO_CACHE_PLANILHAS, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            cache = {}

    if nome_planilha in cache:
        id_planilha = cache[nome_planilha]
        try:
            planilha = cliente.open_by_key(id_planilha)
        except Exception:  # pylint: disable=broad-exception-caught
            planilha = None
    else:
        planilha = None

    if not planilha:
        try:
            planilha = cliente.open(nome_planilha)
            cache[nome_planilha] = planilha.id
            with open(ARQUIVO_CACHE_PLANILHAS, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=4)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "   [AVISO] Planilha '%s' não encontrada no Drive.", nome_planilha
            )
            return None

    try:
        return planilha.worksheet(aba)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning(
            "   [AVISO] Aba '%s' não encontrada na planilha '%s'. Detalhe: %s",
            aba,
            nome_planilha,
            e,
        )
        return None


def encontrar_proxima_linha_vazia(sheet, start_row: int, check_col: int) -> int:
    """
    Varre verticalmente uma coluna chave para encontrar a primeira célula vazia.
    Se o limite físico da planilha for atingido, expande a grade automaticamente.
    """
    coluna_alvo = sheet.col_values(check_col)
    linha_vazia = start_row

    if len(coluna_alvo) >= start_row:
        for i in range(start_row - 1, len(coluna_alvo)):
            if not str(coluna_alvo[i]).strip():
                linha_vazia = i + 1
                break
        else:
            linha_vazia = len(coluna_alvo) + 1

    try:
        if linha_vazia > sheet.row_count:
            sheet.add_rows(15)
            logger.info(
                "   [EXPANSÃO] O limite da aba '%s' foi atingido. "
                "Grade expandida em +500 linhas automaticamente.",
                sheet.title,
            )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning(
            "   [AVISO] Falha ao verificar/expandir os limites da aba '%s': %s",
            sheet.title,
            e,
        )

    return linha_vazia


def encontrar_linha_do_contrato(sheet, contrato: str, col_idx: int) -> int:
    """
    Localiza o índice da linha na qual um contrato específico foi gravado.
    """
    try:
        valores = sheet.col_values(col_idx)
        for i, valor in enumerate(valores, start=1):
            if str(valor).strip() == str(contrato).strip():
                return i
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("   [ERRO BUSCA PLANILHA] Falha ao localizar cota: %s", e)
    return None


def atualizar_planilha_vendedor(
    sheet, row_csv: dict, dados_site: dict, contrato: str
) -> str:
    """
    Compila os dados raspados e injeta na planilha individual do Vendedor.
    """
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
        val_limpo = (
            str_lance.replace("R$", "").replace(".", "").replace(",", ".").strip()
        )
        lance_val = float(val_limpo)
    except (ValueError, TypeError):
        pass

    dados_financeiros = [
        dados_site.get("credito", 0.00),
        lance_val,
        "",
        str(contrato),
        str(dados_site.get("grupo", "")),
        str(dados_site.get("cota", "")),
        str(dados_site.get("estado", "")),
        "PAGO" if dados_site.get("pago") else "PENDENTE", # Adiciona Status Pagamento
        1 if dados_site.get("pago") else 0,               # Adiciona Parcela
        "Ativo"                                           # Adiciona Status Cliente
    ]

    sheet.update_cell(linha, 2, status_pag)
    sheet.update(
        range_name=f"D{linha}:H{linha}",
        values=[dados_cadastrais],
        value_input_option="USER_ENTERED",
    )
    # MUDE O FINAL DE P{linha} PARA S{linha}
    sheet.update(
        range_name=f"J{linha}:S{linha}", 
        values=[dados_financeiros],
        value_input_option="USER_ENTERED",
    )

    return status_pag


def atualizar_planilha_geral(
    sheet, row_csv: dict, dados_site: dict, contrato: str
) -> str:
    """
    Compila os dados raspados e injeta na matriz unificada da planilha GERAL.
    O sistema ignora as abas mensais extintas, mas preserva rigorosamente
    o registo da Data de Adição (Coluna R) para os cálculos de KPI do painel.
    """
    # Procura linha vazia a partir da linha 2 (pois o cabeçalho subiu) na coluna C (Data Venda)
    linha = encontrar_proxima_linha_vazia(sheet, start_row=2, check_col=3)
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
        val_limpo = (
            str_lance.replace("R$", "").replace(".", "").replace(",", ".").strip()
        )
        lance_val = float(val_limpo)
    except (ValueError, TypeError):
        pass

    fuso_utc4 = timezone(timedelta(hours=-4))
    data_registro_atual = datetime.now(fuso_utc4).strftime("%d/%m/%Y")

    dados_financeiros = [
        dados_site.get("credito", 0.00),
        lance_val,
        "",
        str(contrato),
        str(dados_site.get("grupo", "")),
        str(dados_site.get("cota", "")),
        str(dados_site.get("estado", "")),
        str(dados_site.get("cpf", "")),
        str(row_csv.get("vendedor", "")),
        data_registro_atual,
        "PAGO" if dados_site.get("pago") else "PENDENTE", # Adiciona Status Pagamento
        1 if dados_site.get("pago") else 0,               # Adiciona Parcela
        "Ativo"                                           # Adiciona Status Cliente
    ]

    sheet.update_cell(linha, 1, status_pag)
    sheet.update(
        range_name=f"C{linha}:G{linha}",
        values=[dados_cadastrais],
        value_input_option="USER_ENTERED",
    )
    # MUDE O FINAL DE R{linha} PARA U{linha}
    sheet.update(
        range_name=f"I{linha}:U{linha}",
        values=[dados_financeiros],
        value_input_option="USER_ENTERED",
    )

    versao_cota = dados_site.get("versao")
    if versao_cota is not None:
        try:
            sheet.update_cell(linha, 24, f"{versao_cota:02d}")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("   [ERRO PLANILHA] Falha ao gravar versão na coluna X: %s", e)

    return status_pag
