import time
import re
import os
import logging
import random
import requests
from bs4 import BeautifulSoup
from motor_navegacao import iniciar_navegador, fazer_login_com_ia

ARQUIVO_JSON = "files/adimplencia.json"
ARQUIVO_LOG = "diagnostico_http_v5.txt"

logging.basicConfig(level=logging.INFO, format="%(message)s")


def criar_sessao_hibrida():
    logging.info("=== Iniciando Sequestro de Sessão ===")
    driver = iniciar_navegador()
    sessao = requests.Session()
    sessao.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
        }
    )

    if fazer_login_com_ia(driver):
        for cookie in driver.get_cookies():
            sessao.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"])
        driver.quit()
        logging.info("[SUCESSO] Sessão HTTP forjada.")
        return sessao
    driver.quit()
    return None


def testar_extracao(sessao, contrato):
    log_execucao = []
    log_execucao.append(f"\n--- INICIANDO SONDA: CONTRATO {contrato} ---")

    url_busca = "https://intranet.consorciotradicao.com.br/autocred/Attendance/searchCota.asp?codigo_formulario_intranet=4&descricao_formulario_intranet=Consorciado"

    log_execucao.append("Fase A (POST de Busca)...")
    sessao.get(url_busca)
    payload = f"Grupo=&Cota=&cgc_cpf_cliente=&nome=&NumeroContrato={contrato}&event=3"

    try:
        res_busca = sessao.post(
            url_busca, data=payload, timeout=10, allow_redirects=True
        )
        texto_html = res_busca.text

        if "Não reconheço os dados" in texto_html:
            log_execucao.append("[FALHA] Contrato não reconhecido pelo servidor.")
            return log_execucao

        with open(f"dump_sucesso_busca_{contrato}.html", "w", encoding="utf-8") as f:
            f.write(texto_html)
            log_execucao.append(
                f"[SISTEMA] HTML da tabela salvo em: dump_sucesso_busca_{contrato}.html"
            )

        sopa = BeautifulSoup(texto_html, "html.parser")
        elemento_texto = sopa.find(string=re.compile(str(contrato)))

        if elemento_texto:
            linha_tr = elemento_texto.find_parent("tr")
            if linha_tr:
                log_execucao.append(
                    "[ANÁLISE JS] Linha TR localizada! Procurando gatilhos de clique..."
                )
                para_analise = linha_tr.find_all(True)
                gatilhos_encontrados = 0
                for tag in para_analise:
                    if tag.has_attr("onclick"):
                        log_execucao.append(f"  -> GATILHO ONCLICK: {tag['onclick']}")
                        gatilhos_encontrados += 1
                    if tag.has_attr("href"):
                        log_execucao.append(f"  -> GATILHO HREF: {tag['href']}")
                        gatilhos_encontrados += 1

                if gatilhos_encontrados == 0:
                    log_execucao.append(
                        "  -> NENHUM GATILHO DIRETO ENCONTRADO na linha."
                    )
        else:
            log_execucao.append(
                "[ERRO] Contrato não localizado visualmente no HTML retornado."
            )

    except Exception as e:
        log_execucao.append(f"[ERRO] Falha de Rede: {e}")

    return log_execucao


if __name__ == "__main__":
    import json

    if not os.path.exists(ARQUIVO_JSON):
        logging.error("Arquivo JSON não encontrado.")
        exit(1)

    sessao_teste = criar_sessao_hibrida()
    if not sessao_teste:
        exit(1)

    with open(ARQUIVO_JSON, "r", encoding="utf-8") as f:
        adimplencia = json.load(f)

    # Filtrar apenas os contratos ativos
    contratos_ativos = [
        (c, info) for c, info in adimplencia.items() if info.get("monitorar", True)
    ]

    # Selecionar 5 aleatoriamente (ou o total disponível se for menor que 5)
    limite = min(5, len(contratos_ativos))
    amostra_aleatoria = random.sample(contratos_ativos, limite)

    with open(ARQUIVO_LOG, "w", encoding="utf-8") as f_log:
        f_log.write(
            f"=== RELATÓRIO DE DIAGNÓSTICO HTTP V5.1 (ALEATÓRIO) - {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
        )

        for contrato, info in amostra_aleatoria:
            logs = testar_extracao(sessao_teste, contrato)
            for linha in logs:
                logging.info(linha)
                f_log.write(linha + "\n")
            time.sleep(1)

    logging.info(
        f"\nTeste finalizado. Consulte o '{ARQUIVO_LOG}' ou os arquivos HTML gerados."
    )
