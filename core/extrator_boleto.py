"""
Módulo de Processamento de Boletos PDF em Memória.

Isola as dependências gráficas pesadas (PyMuPDF, Pillow e Pyzbar) para renderizar
a 2ª via de boletos exclusivamente em memória RAM e extrair QR Code (PIX) e código I25.
"""

import io
import re
import logging
import urllib.parse
import requests
from bs4 import BeautifulSoup
import pymupdf
from PIL import Image
from pyzbar.pyzbar import decode, ZBarSymbol

from core.config import URL_BASE_PORTAL

logger = logging.getLogger("EnterpriseBoleto")


def extrair_pdf_boleto_memoria(
    sessao: requests.Session, cpf: str, grupo: str, cota: str
) -> dict:
    """
    Baixa o PDF do boleto em stream de memória, converte em imagem a 300 DPI
    e decodifica os dados de pagamento (QR Code PIX e Código de Barras I25).
    """
    resultado = {"qr_code": "", "codigo_barras": ""}

    try:
        desc = urllib.parse.quote("2ª Via Boleto", encoding="iso-8859-1")
        url_2via = (
            f"{URL_BASE_PORTAL}/Attendance/emissSlip.asp?tipo=0&cgc_cpf_cliente={cpf}&"
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

                url_post_slip = f"{URL_BASE_PORTAL}/Slip/Slip.asp"
                sessao.headers.update({"Referer": url_2via})
                res_pdf = sessao.post(url_post_slip, data=payload_boleto, timeout=15)

                if res_pdf.status_code == 200 and b"%PDF" in res_pdf.content[:10]:
                    with pymupdf.open(stream=res_pdf.content, filetype="pdf") as doc:
                        for pagina in doc:
                            pix = pagina.get_pixmap(dpi=300)
                            img_bytes = pix.tobytes("png")
                            imagem = Image.open(io.BytesIO(img_bytes))

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
        logger.error("Falha ao processar boleto em memória: %s", e)

    return resultado
