"""
Módulo de Integração (API CDA) com Modo de Teste e Retenção de JSON.

Monitora o endpoint de atualização da API Consórcio Tradição.
Verifica o último estado salvo em disco para evitar reenvios em reinicializações,
extrai os arquivos via API e realiza scraping do relatório de desistentes.
"""

import os
import sys
import time
import socket
import gzip
import json
import shutil
from typing import Optional

import requests
import urllib3.util.connection as urllib3_cn

from core.config import (
    KEY_API_TRADICAO,
    TOKEN_SITE_AUTOCRED,
    PASTA_TEMP_CDA,
    URL_ATUALIZACAO_API,
    URL_UPLOAD_AUTOCRED,
    ENDPOINTS_API_TRADICAO,
    configurar_logger,
)
from core.autenticacao import criar_sessao_http_autenticada
from core.extrator_portal import extrair_relatorio_desistentes

# ============================================================================
# CONFIGURAÇÃO DE LOGGING E AMBIENTE
# ============================================================================
logger_cda = configurar_logger("CDAWorker", "files/cda_sistema.log")

MODO_TESTE = False  # Se True, salva os JSONs localmente sem enviar ao site
ARQUIVO_ESTADO_LOCAL = os.path.join(PASTA_TEMP_CDA, "ultima_atualizacao.json")


def allowed_gai_family() -> int:
    """Força rede IPv4 para evitar bloqueios 403 do Cloudflare."""
    return socket.AF_INET


urllib3_cn.allowed_gai_family = allowed_gai_family


def validar_credenciais_cda() -> None:
    """Garante que KEY e TOKEN_SITE foram carregados do arquivo .env."""
    if not KEY_API_TRADICAO or not TOKEN_SITE_AUTOCRED:
        logger_cda.error(
            "[ERRO FATAL] Variáveis KEY= e/ou TOKEN_SITE= ausentes no .env."
        )
        sys.exit(1)


def carregar_ultimo_estado_disco() -> Optional[str]:
    """
    Lê a última assinatura de atualização salva em disco (api_trad/ultima_atualizacao.json)
    para evitar sincronizações desnecessárias ao reiniciar o serviço.
    """
    if os.path.exists(ARQUIVO_ESTADO_LOCAL):
        try:
            with open(ARQUIVO_ESTADO_LOCAL, "r", encoding="utf-8") as f_estado:
                conteudo = f_estado.read().strip()
                if conteudo:
                    logger_cda.info(
                        "[CACHE DISCO] Último estado recuperado localmente: %s",
                        conteudo,
                    )
                    return conteudo
        except OSError as e_io:
            logger_cda.warning(
                "[CACHE DISCO] Não foi possível ler o estado anterior: %s", e_io
            )
    return None


def salvar_estado_disco(estado: str) -> None:
    """Garante a persistência local da assinatura de atualização após o ciclo."""
    try:
        os.makedirs(PASTA_TEMP_CDA, exist_ok=True)
        with open(ARQUIVO_ESTADO_LOCAL, "w", encoding="utf-8") as f_estado:
            f_estado.write(estado.strip())
    except OSError as e_io:
        logger_cda.warning(
            "[CACHE DISCO] Falha ao gravar estado atualizado no disco: %s", e_io
        )


def obter_hash_atualizacao() -> Optional[str]:
    """Consulta o endpoint de atualização para verificar alterações na base."""
    try:
        resposta = requests.get(
            URL_ATUALIZACAO_API,
            headers={"X-API-Key": KEY_API_TRADICAO},
            timeout=30,
        )
        resposta.raise_for_status()
        return resposta.text.strip()
    except requests.exceptions.RequestException as e:
        logger_cda.error("Erro ao verificar atualização na API Tradição: %s", e)
        return None


def obter_sessao_http_cda() -> Optional[requests.Session]:
    """Solicita ao módulo central de autenticação uma sessão HTTP validada."""
    logger_cda.info("[SISTEMA] Iniciando IA de Login para extração do portal...")
    try:
        sessao = criar_sessao_http_autenticada()
        logger_cda.info("[SESSÃO FORJADA] Acesso à intranet concedido.")
        return sessao
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger_cda.error("[ERRO DE LOGIN] Falha ao autenticar no portal: %s", e)
        return None


def baixar_e_enviar() -> None:
    """
    Coordena o download seguro (comprimindo on-the-fly para .gz)
    e o upload direto (Stream application/gzip) para o sistema Autocred.
    """
    headers_tradicao = {
        "X-API-Key": KEY_API_TRADICAO,
        "Accept": "application/json",
    }
    headers_site = {
        "Authorization": f"Bearer {TOKEN_SITE_AUTOCRED}",
        "Content-Type": "application/gzip",
    }
    os.makedirs(PASTA_TEMP_CDA, exist_ok=True)

    if MODO_TESTE:
        logger_cda.warning(
            "[AVISO] MODO_TESTE ativo. Arquivos baixados NÃO serão enviados ao site."
        )

    # 1. DOWNLOAD DA API OFICIAL
    for nome, url in ENDPOINTS_API_TRADICAO.items():
        nome_arquivo_json = f"{nome}.json"
        caminho_json = os.path.join(PASTA_TEMP_CDA, nome_arquivo_json)

        logger_cda.info("[API TRADIÇÃO] Baixando endpoint: %s...", nome)
        try:
            r_down = requests.get(
                url, headers=headers_tradicao, stream=True, timeout=60
            )
            r_down.raise_for_status()

            with open(caminho_json, "wb") as f_out:
                for chunk in r_down.iter_content(chunk_size=8192):
                    if chunk:
                        f_out.write(chunk)

            if not MODO_TESTE:
                nome_arquivo_gz = f"{nome}.json.gz"
                caminho_gz = os.path.join(PASTA_TEMP_CDA, nome_arquivo_gz)

                logger_cda.info(
                    "[UPLOAD] Comprimindo e sincronizando %s...", nome_arquivo_gz
                )
                with open(caminho_json, "rb") as f_in, gzip.open(
                    caminho_gz, "wb"
                ) as f_out:
                    shutil.copyfileobj(f_in, f_out)

                with open(caminho_gz, "rb") as f_in:
                    r_up = requests.post(
                        URL_UPLOAD_AUTOCRED,
                        headers=headers_site,
                        params={"nome": nome_arquivo_gz},
                        data=f_in,
                        timeout=120,
                    )
                    r_up.raise_for_status()

                if os.path.exists(caminho_gz):
                    os.remove(caminho_gz)

        except requests.exceptions.RequestException as e:
            logger_cda.error("[%s] Falha no processamento: %s", nome, e)

    # 2. SCRAPING DA INTRANET (RELATÓRIO DESISTENTES LEGADO)
    sessao_cda = obter_sessao_http_cda()
    if sessao_cda:
        dados_desistentes = extrair_relatorio_desistentes(sessao_cda)
        sessao_cda.close()

        if dados_desistentes:
            nome_arquivo_json = "desistentes_portal.json"
            caminho_json = os.path.join(PASTA_TEMP_CDA, nome_arquivo_json)

            with open(caminho_json, "w", encoding="utf-8") as f_out:
                json.dump(dados_desistentes, f_out, ensure_ascii=False, indent=4)

            if not MODO_TESTE:
                nome_arquivo_gz = "desistentes_portal.json.gz"
                caminho_gz = os.path.join(PASTA_TEMP_CDA, nome_arquivo_gz)

                logger_cda.info(
                    "[UPLOAD] Comprimindo e enviando %s...", nome_arquivo_gz
                )
                try:
                    with open(caminho_json, "rb") as f_in, gzip.open(
                        caminho_gz, "wb"
                    ) as f_out:
                        shutil.copyfileobj(f_in, f_out)

                    with open(caminho_gz, "rb") as f_in:
                        r_up = requests.post(
                            URL_UPLOAD_AUTOCRED,
                            headers=headers_site,
                            params={"nome": nome_arquivo_gz},
                            data=f_in,
                            timeout=120,
                        )
                        r_up.raise_for_status()
                    logger_cda.info(
                        "[DESISTENTES PORTAL] Sincronização concluída com sucesso."
                    )

                    if os.path.exists(caminho_gz):
                        os.remove(caminho_gz)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    logger_cda.error(
                        "[DESISTENTES PORTAL] Falha na sincronização: %s", e
                    )


def iniciar_monitoramento() -> None:
    """Mantém o serviço de verificação ativo (Polling contínuo 24/7)."""
    validar_credenciais_cda()
    logger_cda.info(">>> INICIANDO MOTOR CDA MISTO (API + INTRANET) <<<")
    ultimo_estado = carregar_ultimo_estado_disco()

    while True:
        logger_cda.info("[POLLING] Verificando integridade no portal Consórcio...")
        estado_atual = obter_hash_atualizacao()

        if estado_atual and estado_atual != ultimo_estado:
            logger_cda.info(
                "[NOVA BUILD] Alteração detectada (%s)! Iniciando extração total...",
                estado_atual,
            )
            baixar_e_enviar()
            salvar_estado_disco(estado_atual)
            ultimo_estado = estado_atual
        elif estado_atual and estado_atual == ultimo_estado:
            logger_cda.info(
                "[POLLING] Base intacta ( já sincronizada com o disco). Modo espera (1h)..."
            )

        time.sleep(3600)


if __name__ == "__main__":
    try:
        iniciar_monitoramento()
    except KeyboardInterrupt:
        logger_cda.info("[AVISO] Encerramento manual recebido (Ctrl+C).")
        sys.exit(0)
