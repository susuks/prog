"""
Módulo de Contratos de Dados (Schemas).

Garante a tipagem, sanitização e formatação estrita dos payloads entregues
para o front-end e para o endpoint de sincronização da Autocred.
"""

import re
from dataclasses import dataclass, asdict
from typing import Optional
from core.utils import formatar_data


@dataclass
class DesistenteSchema:
    """Contrato estrito para cada registro do relatório de desistentes."""

    grupo: int
    cota: int
    versao: int
    nome: str
    data_adesao: Optional[str]
    contemplacao: Optional[str]
    percentual_a_pagar: str
    cpf: Optional[str]
    bloqueado: bool

    @classmethod
    def criar_de_bruto(
        cls,
        grupo: int,
        cota: int,
        versao: int,
        nome: str,
        txt_data_adesao: str,
        txt_contemplacao: str,
        txt_percentual: str,
        cpf: Optional[str],
        bloqueado: bool,
    ) -> "DesistenteSchema":
        """Constrói e valida o schema a partir das strings brutas do HTML."""
        perc_limpo = str(txt_percentual or "0").replace(".", "").replace(",", ".")
        try:
            perc_formatado = f"{float(perc_limpo):.4f}"
        except ValueError:
            perc_formatado = "0.0000"

        cpf_limpo = re.sub(r"\D", "", str(cpf)) if cpf else None
        if cpf_limpo and len(cpf_limpo) < 11:
            cpf_limpo = None

        return cls(
            grupo=int(grupo),
            cota=int(cota),
            versao=int(versao),
            nome=" ".join(str(nome or "-").upper().split()),
            data_adesao=formatar_data(txt_data_adesao),
            contemplacao=formatar_data(txt_contemplacao),
            percentual_a_pagar=perc_formatado,
            cpf=cpf_limpo,
            bloqueado=bool(bloqueado),
        )

    def para_dict(self) -> dict:
        """Serializa o contrato validado para dicionário JSON."""
        return asdict(self)


@dataclass
class DadosVendaSchema:
    """Contrato estrito para o retorno cadastral da rota /processar_venda."""

    contrato: str
    cpf: str
    grupo: str
    cota: str
    versao: Optional[int]
    estado_uf: str
    valor_credito: float
    valor_parcela: float
    data_venda_painel: str
    primeira_parcela_paga: bool
    telefone_cliente: str

    @classmethod
    def criar_de_dados_site(cls, contrato: str, dados_site: dict) -> "DadosVendaSchema":
        """Normaliza o dicionário raspado do portal para o contrato da API."""
        tel_bruto = str(dados_site.get("telefone", ""))
        numeros_tel = re.sub(r"\D", "", tel_bruto)
        tel_formatado = f"55{numeros_tel}" if numeros_tel else ""

        versao_raw = dados_site.get("versao")
        versao_int = int(versao_raw) if versao_raw is not None else None

        return cls(
            contrato=str(contrato).strip(),
            cpf=str(dados_site.get("cpf", "-")),
            grupo=str(dados_site.get("grupo", "-")),
            cota=str(dados_site.get("cota", "-")),
            versao=versao_int,
            estado_uf=str(dados_site.get("estado", "")).upper(),
            valor_credito=round(float(dados_site.get("credito", 0.0)), 2),
            valor_parcela=round(float(dados_site.get("valor_parcela", 0.0)), 2),
            data_venda_painel=str(dados_site.get("data_venda", "")),
            primeira_parcela_paga=bool(dados_site.get("pago", False)),
            telefone_cliente=tel_formatado,
        )

    def para_dict(self) -> dict:
        """Serializa o contrato validado para dicionário JSON."""
        return asdict(self)
