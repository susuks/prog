"""
Ferramenta de Coleta Manual e Geração de Massa de Dados (Testes V6).
Processa um arquivo CSV contendo Grupo/Cota e gera a estrutura JSON formatada
para injeção direta no motor de adimplência corporativo.
"""

import os
import csv
import json
import logging
import time

from motor_navegacao import iniciar_navegador, fazer_login_com_ia, buscar_por_grupo_cota

# Configuração de Output do Terminal
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ColetorDeMassa")

ARQUIVO_ENTRADA = "files/cotas.csv"
ARQUIVO_SAIDA_JSON = "files/adimplencia_gerado.json"
ARQUIVO_SAIDA_TXT = "files/contratos_coletados.txt"


def iniciar_extracao():
    """Lê o arquivo CSV, busca os contratos no portal e formata as saídas."""

    if not os.path.exists(ARQUIVO_ENTRADA):
        logger.error("Arquivo de entrada não localizado: %s", ARQUIVO_ENTRADA)
        return

    # Inicialização da Estrutura de Memória
    json_saida = {}
    if os.path.exists(ARQUIVO_SAIDA_JSON):
        try:
            with open(ARQUIVO_SAIDA_JSON, "r", encoding="utf-8") as f:
                json_saida = json.load(f)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    # Inicialização do Motor Selenium
    driver = iniciar_navegador()
    logger.info("Executando login para coleta de dados...")

    if not fazer_login_com_ia(driver):
        logger.critical("Falha crítica na autenticação. Abortando coleta.")
        driver.quit()
        return

    logger.info("Iniciando leitura do arquivo CSV...")

    with open(ARQUIVO_ENTRADA, mode="r", encoding="utf-8-sig") as ficheiro_csv:
        leitor = csv.DictReader(ficheiro_csv)

        for linha in leitor:
            grupo = str(linha.get("grupo", "")).strip()
            cota = str(linha.get("cota", "")).strip()

            if not grupo or not cota:
                continue

            logger.info("Coletando dados para -> Grupo: %s | Cota: %s", grupo, cota)

            # Executa a pesquisa visual no portal Autocred
            contrato = buscar_por_grupo_cota(driver, grupo, cota)

            if contrato:
                logger.info("[SUCESSO] Contrato extraído: %s", contrato)

                # Formatação e Inserção no JSON de Adimplência
                if contrato not in json_saida:
                    json_saida[contrato] = {
                        "vendedor_nome": "1",
                        "nome_planilha": "Controle de Vendas -- 1",
                        "aba_original": "JUNHO",
                        "grupo": grupo,
                        "cota": cota,
                        "monitorar": True,
                        "ultima_verificacao": 0,
                        "data_limbo": None,
                    }

                    # Gravação Incremental para Prevenção de Perdas (Crash)
                    with open(ARQUIVO_SAIDA_JSON, "w", encoding="utf-8") as f_json:
                        json.dump(json_saida, f_json, indent=4)

                    # Gravação Simples no Bloco de Notas
                    with open(ARQUIVO_SAIDA_TXT, "a", encoding="utf-8") as f_txt:
                        f_txt.write(f"{contrato}\n")
            else:
                logger.warning(
                    "[FALHA] Cadastro inacessível ou não encontrado para a cota %s.",
                    cota,
                )

            # Pausa para controle de taxa (Rate Limiting) e prevenção de bloqueio
            time.sleep(2)

    logger.info("Coleta finalizada com êxito. O WebDriver será encerrado.")
    driver.quit()


if __name__ == "__main__":
    iniciar_extracao()
