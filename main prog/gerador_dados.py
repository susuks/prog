"""
Módulo Gerenciador de Dados e Arquivos.

Responsável por centralizar toda a manipulação de dados estáticos, configurações
de ambiente, formatação de textos (Regex), operações de Entrada/Saída (I/O) em
arquivos locais (JSON, CSV) e integração com a API do Google Sheets.
"""

import os
import json
import re
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ============================================================================
# CONSTANTES E CAMINHOS DE ARQUIVOS
# ============================================================================
ARQUIVO_FILA = "fila_vendas.csv"
ARQUIVO_EM_PROCESSAMENTO = "temp_processando.csv"
ARQUIVO_HISTORICO_SUCESSO = "historico_concluidos.csv"
ARQUIVO_PENDENTES = "pendentes_reanalise.json"
ARQUIVO_CONFIG = "config.txt"
MAX_TENTATIVAS = 10000
TEMPO_INATIVIDADE_MAXIMO = 300


# ============================================================================
# CONFIGURAÇÕES E CREDENCIAIS
# ============================================================================
def carregar_configuracoes() -> dict:
    """
    Lê o arquivo de configuração local e extrai credenciais e parâmetros.

    O arquivo deve seguir o formato 'CHAVE=VALOR'. Caso o arquivo não
    exista, valores padrão de segurança serão retornados para evitar
    falhas de importação no módulo principal.

    Returns:
        dict: Mapeamento de configurações (MATRICULA, SENHA, nomes de abas, etc).
    """
    config = {
        "MATRICULA": "",
        "SENHA": "",
        "PREFIXO_PLANILHA": "Controle de Vendas -- ",
        "NOME_ABA": "JANEIRO",
        "ANO_GERAL": "2026",
    }

    if not os.path.exists(ARQUIVO_CONFIG):
        return config

    try:
        with open(ARQUIVO_CONFIG, "r", encoding="utf-8") as f:
            for linha in f:
                if "=" in linha:
                    chave, valor = linha.split("=", 1)
                    valor_limpo = valor.replace("\n", "").replace("\r", "")
                    config[chave.strip()] = valor_limpo
        return config
    except OSError:
        return config


# Inicialização em tempo de importação para uso global
CONFIG = carregar_configuracoes()
USUARIO_LOGIN = CONFIG.get("MATRICULA")
SENHA_LOGIN = CONFIG.get("SENHA")
PREFIXO_PLANILHA = CONFIG.get("PREFIXO_PLANILHA")
NOME_ABA = CONFIG.get("NOME_ABA")
NOME_ABA_GERAL = CONFIG.get("ANO_GERAL")


# ============================================================================
# PROCESSAMENTO DE DADOS E FORMATAÇÃO (REGEX)
# ============================================================================
def limpar_inteiro(texto: str) -> int:
    """
    Extrai todos os dígitos numéricos de uma string e os converte para inteiro.

    Args:
        texto (str): Texto contendo números (ex: '001', '1 parcela paga').

    Returns:
        int: O número formatado como inteiro. Retorna 0 em caso de falha.
    """
    try:
        numeros = re.sub(r"\D", "", str(texto))
        return int(numeros) if numeros else 0
    except (ValueError, TypeError):
        return 0


def limpar_valor(texto: str) -> float:
    """
    Converte uma string de valor monetário brasileiro para o formato float.

    Args:
        texto (str): String com formato de moeda (ex: 'R$ 1.500,00').

    Returns:
        float: Valor numérico computável para planilhas. Retorna 0.00 se falhar.
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

    Returns:
        dict: Dicionário contendo o estado atual dos contratos pendentes.
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

    Args:
        dados (dict): Dicionário atualizado de contratos para gravação.
    """
    with open(ARQUIVO_PENDENTES, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)


def adicionar_para_reanalise(
    contrato: str,
    vendedor_nome: str,
    vendedor_tel: str,
    nome_planilha: str,
    origem: str,
    dados_completos: dict,
) -> None:
    """
    Registra um contrato pendente de pagamento na memória de curto prazo (JSON).

    Args:
        contrato (str): Número identificador do contrato (ex: '22134896').
        vendedor_nome (str): Nome do vendedor associado à venda.
        vendedor_tel (str): Telefone do vendedor para alertas futuros.
        nome_planilha (str): Nome da planilha individual destino.
        origem (str): Origem da captação da venda (ex: 'WhatsApp').
        dados_completos (dict): Estrutura bruta originada do CSV da fila.
    """
    pendentes = carregar_pendentes()
    pendentes[contrato] = {
        "vendedor_nome": vendedor_nome,
        "vendedor_tel": vendedor_tel,
        "nome_planilha": nome_planilha,
        "origem": origem,
        "dados_originais": dados_completos,
        "tentativas": 0,
    }
    salvar_pendentes(pendentes)
    print(f"   [AGENDADO] Contrato {contrato} adicionado à reanálise.")


def salvar_historico_concluido(
    contrato: str,
    nome_planilha: str,
    vendedor_nome: str,
    vendedor_tel: str,
    status_pag: str,
) -> None:
    """
    Grava os metadados de um contrato processado definitivamente em log CSV.

    Args:
        contrato (str): Número do contrato processado.
        nome_planilha (str): Nome do destino no Google Sheets.
        vendedor_nome (str): Nome do vendedor.
        vendedor_tel (str): Telefone de contato do vendedor.
        status_pag (str): O status final consolidado.
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
        print(f"   [ERRO HISTÓRICO] Falha ao gravar log local: {e}")


# ============================================================================
# COMUNICAÇÃO COM GOOGLE SHEETS
# ============================================================================
def conectar_google_sheets(nome_planilha: str, aba: str):
    """
    Estabelece uma conexão autenticada via API com uma planilha e aba específica.

    Args:
        nome_planilha (str): Título exato do arquivo no Google Drive.
        aba (str): Título da página (worksheet) a ser editada.

    Returns:
        gspread.models.Worksheet: Instância da aba pronta para I/O.
        None: Retorna None em caso de falha de autenticação ou se não existir.
    """
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            "credentials.json", scope
        )
        cliente = gspread.authorize(creds)
        return cliente.open(nome_planilha).worksheet(aba)
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def encontrar_proxima_linha_vazia(sheet, start_row: int, check_col: int) -> int:
    """
    Varre verticalmente uma coluna chave para encontrar a primeira célula vazia.

    Args:
        sheet (gspread.models.Worksheet): Aba da planilha ativa.
        start_row (int): Índice da linha onde a tabela lógica começa.
        check_col (int): Índice numérico da coluna usada como referência de preenchimento.

    Returns:
        int: O número da linha disponível para injeção de dados.
    """
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
    """
    Localiza o índice da linha na qual um contrato específico foi gravado.
    Utilizado primeiramente pela fase de Reanálise.

    Args:
        sheet (gspread.models.Worksheet): Aba ativa a ser consultada.
        contrato (str): Número do contrato alvo da busca.
        col_idx (int): Índice da coluna onde os IDs de contrato residem.

    Returns:
        int: O número da linha exata.
        None: Caso o contrato não conste na tabela.
    """
    try:
        valores = sheet.col_values(col_idx)
        for i, valor in enumerate(valores, start=1):
            if str(contrato).strip() == str(valor).strip():
                return i
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"   [ERRO BUSCA PLANILHA] Falha ao localizar cota: {e}")

    return None


def atualizar_planilha_vendedor(
    sheet, row_csv: dict, dados_site: dict, contrato: str
) -> str:
    """
    Compila os dados raspados e injeta na planilha individual do Vendedor.

    Args:
        sheet (Worksheet): Instância da planilha do vendedor.
        row_csv (dict): Metadados brutos enviados via WhatsApp.
        dados_site (dict): Dicionário com informações extraídas do portal Autocred.
        contrato (str): Identificador numérico da cota.

    Returns:
        str: Status computado ("1º Parcela Paga" ou string vazia se pendente).
    """
    linha = encontrar_proxima_linha_vazia(sheet, start_row=12, check_col=4)
    status_pag = "1º Parcela Paga" if dados_site["pago"] else ""

    dados_cadastrais = [
        str(dados_site["data_venda"]),
        str(dados_site["nome"]),
        str(dados_site["telefone"]),
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
        dados_site["credito"],
        lance_val,
        "",
        str(contrato),
        str(dados_site["grupo"]),
        str(dados_site["cota"]),
    ]

    sheet.update_cell(linha, 2, status_pag)
    sheet.update(
        range_name=f"D{linha}:H{linha}",
        values=[dados_cadastrais],
        value_input_option="USER_ENTERED",
    )
    sheet.update(
        range_name=f"J{linha}:O{linha}",
        values=[dados_financeiros],
        value_input_option="USER_ENTERED",
    )

    return status_pag


def atualizar_planilha_geral(
    sheet, row_csv: dict, dados_site: dict, contrato: str
) -> str:
    """
    Compila os dados raspados e injeta na planilha GERAL (Gestão Centralizada).
    Nota: A matriz de colunas nesta planilha é deslocada -1 casa para a esquerda.

    Args:
        sheet (Worksheet): Instância da aba centralizada (Ex: Ano 2026).
        row_csv (dict): Metadados brutos enviados via WhatsApp.
        dados_site (dict): Dicionário com informações extraídas do portal.
        contrato (str): Identificador numérico da cota.

    Returns:
        str: Status computado ("1º Parcela Paga" ou string vazia se pendente).
    """
    linha = encontrar_proxima_linha_vazia(sheet, start_row=7, check_col=3)
    status_pag = "1º Parcela Paga" if dados_site["pago"] else ""

    dados_cadastrais = [
        str(dados_site["data_venda"]),
        str(dados_site["nome"]),
        str(dados_site["telefone"]),
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
        dados_site["credito"],
        lance_val,
        "",
        str(contrato),
        str(dados_site["grupo"]),
        str(dados_site["cota"]),
    ]

    sheet.update_cell(linha, 1, status_pag)
    sheet.update(
        range_name=f"C{linha}:G{linha}",
        values=[dados_cadastrais],
        value_input_option="USER_ENTERED",
    )
    sheet.update(
        range_name=f"I{linha}:N{linha}",
        values=[dados_financeiros],
        value_input_option="USER_ENTERED",
    )

    return status_pag
