"""
Módulo de Utilitários de Formatação e Fuso Horário.

Fornece funções puras para sanitização de textos, conversão monetária/percentual,
formatação de datas ISO e controle da janela de hibernação (UTC-4).
"""

import re
from datetime import datetime, timedelta, timezone


# ============================================================================
# CONTROLE DE HORÁRIO E HIBERNAÇÃO (UTC-4)
# ============================================================================
def obter_agora_utc4() -> datetime:
    """Retorna o objeto datetime atual ajustado para o fuso horário UTC-4."""
    fuso_utc4 = timezone(timedelta(hours=-4))
    return datetime.now(fuso_utc4)


def is_hibernando() -> bool:
    """Verifica se o sistema está na janela de manutenção (21:00 - 06:30 UTC-4)."""
    agora = obter_agora_utc4()
    hora_decimal = agora.hour + (agora.minute / 60.0)
    return hora_decimal >= 21 or hora_decimal < 6.5


# ============================================================================
# LIMPEZA E FORMATAÇÃO DE DADOS
# ============================================================================
def limpar_inteiro(texto: str) -> int:
    """Extrai todos os dígitos numéricos de uma string e converte para inteiro."""
    try:
        numeros = re.sub(r"\D", "", str(texto))
        return int(numeros) if numeros else 0
    except (ValueError, TypeError):
        return 0


def limpar_valor(texto: str) -> float:
    """Converte uma string de valor monetário brasileiro (ex: 1.234,56) para float."""
    try:
        match = re.search(r"([\d\.]+,\d{2})", str(texto))
        if match:
            valor_limpo = match.group(1)
            return float(valor_limpo.replace(".", "").replace(",", "."))
        return 0.00
    except (ValueError, TypeError):
        return 0.00


def limpar_percentual(texto: str, casas_decimais: int = 4) -> float:
    """Converte strings percentuais do portal (ex: '100,0000') em float arredondado."""
    if not texto:
        return 0.0
    try:
        limpo = str(texto).strip().replace(".", "").replace(",", ".")
        return round(float(limpo), casas_decimais)
    except (ValueError, TypeError):
        return 0.0


def formatar_data(texto_data: str):
    """Converte data no padrão brasileiro (DD/MM/YYYY) para ISO (YYYY-MM-DD)."""
    if not texto_data:
        return None
    texto_limpo = str(texto_data).strip()
    if not texto_limpo or texto_limpo == "-":
        return None
    try:
        dt = datetime.strptime(texto_limpo, "%d/%m/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None
