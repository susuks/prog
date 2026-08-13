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
ARQUIVO_PENDENTES = "files/pendentes_reanalise.json"
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
                    else:
                        config[chave] = valor_limpo
        return config
    except OSError:
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


# Inicialização em tempo de importação para uso global
CONFIG = carregar_configuracoes()
USUARIO_LOGIN = CONFIG.get("MATRICULA")
SENHA_LOGIN = CONFIG.get("SENHA")
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
            valor_texto = match.group(1).replace(".", "").replace(",", ".")
            return float(valor_texto)
        return 0.00
    except (ValueError, TypeError):
        return 0.00


# ============================================================================
# MANIPULAÇÃO DE ARQUIVOS LOCAIS (JSON / CSV)
# ============================================================================
def carregar_pendentes() -> dict:
    """
    Carrega o arquivo JSON que armazena os contratos na fila de reanálise.
    """
    if os.path.exists(ARQUIVO_PENDENTES):
        try:
            with open(ARQUIVO_PENDENTES, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def salvar_pendentes(dados: dict) -> None:
    """
    Persiste o dicionário de contratos pendentes no arquivo JSON.
    """
    with open(ARQUIVO_PENDENTES, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)


def verificar_contrato_registrado(contrato: str) -> bool:
    """
    Consulta a memória local (pendentes e histórico) para evitar duplicidade.
    """
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
    """
    Registra um contrato pendente de pagamento na memória de curto prazo (JSON),
    incluindo a data de inclusão e a aba original para controle preciso.
    """
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
    logger.info(
        "   [AGENDADO] Contrato %s adicionado à reanálise de 30 dias (Aba: %s).",
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
                cabecalho = (
                    "contrato,planilha_destino,data_registro,"
                    "vendedor_nome,vendedor_tel,status_pagamento\n"
                )
                f.write(cabecalho)

            data_hora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            safe_planilha = str(nome_planilha).replace(",", ".")
            safe_nome = str(vendedor_nome).replace(",", ".")

            linha = (
                f"{contrato},{safe_planilha},{data_hora},"
                f"{safe_nome},{vendedor_tel},{status_pag}\n"
            )
            f.write(linha)
    except OSError as e:
        logger.error("   [ERRO HISTÓRICO] Falha ao gravar log local: %s", e)


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

    # 1. Encontra a próxima linha disponível
    if len(coluna_alvo) >= start_row:
        for i in range(start_row - 1, len(coluna_alvo)):
            if not str(coluna_alvo[i]).strip():
                linha_vazia = i + 1
                break
        else:
            linha_vazia = len(coluna_alvo) + 1

    # 2. Expansão Dinâmica da Grade (Prevenção de Colapso Out-of-Bounds)
    try:
        # Se a linha de destino ultrapassar o limite atual da folha...
        if linha_vazia > sheet.row_count:
            sheet.add_rows(500)  # Adiciona um lote de 500 linhas para criar margem
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
            if str(contrato).strip() == str(valor).strip():
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

    # Restauração do Fuso Horário de Campo Grande para a Coluna R
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
        data_registro_atual,  # Coluna R preservada
    ]

    sheet.update_cell(linha, 1, status_pag)
    sheet.update(
        range_name=f"C{linha}:G{linha}",
        values=[dados_cadastrais],
        value_input_option="USER_ENTERED",
    )
    # Extensão do intervalo restaurada até à coluna R
    sheet.update(
        range_name=f"I{linha}:R{linha}",
        values=[dados_financeiros],
        value_input_option="USER_ENTERED",
    )

    return status_pag
