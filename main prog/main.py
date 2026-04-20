"""
Módulo principal do sistema de coleta e reanálise de contratos.
Gerencia o loop infinito e a integração entre o Google Sheets e o portal.
"""

import os
import time
import shutil
import pandas as pd

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# Importação explícita de todas as ferramentas do seu módulo
from coletor_utils import (
    ARQUIVO_FILA, ARQUIVO_EM_PROCESSAMENTO, PREFIXO_PLANILHA,
    MAX_TENTATIVAS, TEMPO_INATIVIDADE_MAXIMO,
    fazer_login_automatico, conectar_google_sheets, buscar_contrato,
    extrair_dados_completos, atualizar_planilha, salvar_historico_concluido,
    adicionar_para_reanalise, carregar_pendentes, verificar_apenas_pagamento,
    encontrar_linha_do_contrato, salvar_pendentes, manter_sessao_viva
)


def loop_servico():
    """
    Loop principal de processamento. Controla novos contratos, 
    revisões na fila contínua e a vitalidade da sessão do navegador.
    """
    print(">>> SERVIÇO DE COLETA (MODO MODULARIZADO) <<<")

    # Inicializa o ChromeDriver
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))

    if not fazer_login_automatico(driver):
        print("[CRÍTICO] Falha na autenticação inicial.")
        return

    ultimo_keep_alive = time.time()

    while True:
        try:
            # -------------------------------------------------------------
            # FASE 1: PROCESSAMENTO DE NOVOS CONTRATOS (PRIORIDADE)
            # -------------------------------------------------------------
            if os.path.exists(ARQUIVO_FILA):
                try:
                    shutil.move(ARQUIVO_FILA, ARQUIVO_EM_PROCESSAMENTO)
                except OSError:
                    time.sleep(1)
                    continue

                ultimo_keep_alive = time.time()

                try:
                    df = pd.read_csv(ARQUIVO_EM_PROCESSAMENTO, sep=',', dtype=str)
                    df.columns = [c.strip() for c in df.columns]
                except (OSError, pd.errors.EmptyDataError):
                    if os.path.exists(ARQUIVO_EM_PROCESSAMENTO):
                        os.remove(ARQUIVO_EM_PROCESSAMENTO)
                    continue

                for _, row in df.iterrows():
                    contrato = row.get('contrato')
                    vendedor = row.get('vendedor')
                    vendedor_tel = row.get('telefone')

                    if pd.isna(contrato):
                        continue

                    print(f"\nProcessando Novo: {contrato} ({vendedor})...")
                    nome_planilha = f"{PREFIXO_PLANILHA}{str(vendedor).strip()}"
                    sheet = conectar_google_sheets(nome_planilha)

                    if not sheet:
                        print(f"   [ERRO] Planilha {nome_planilha} indisponível.")
                        continue

                    if buscar_contrato(driver, contrato):
                        dados = extrair_dados_completos(driver)
                        atualizar_planilha(sheet, row, dados, contrato)

                        texto_status = "1º Parcela Paga" if dados['pago'] else "1º Parcela Não Paga"
                        salvar_historico_concluido(
                            contrato, nome_planilha, vendedor,
                            vendedor_tel, texto_status
                        )

                        if not dados['pago']:
                            adicionar_para_reanalise(
                                contrato, vendedor, vendedor_tel,
                                nome_planilha, row.get('origem'), row.to_dict()
                            )

                        print("   [SUCESSO] Processado.")
                    else:
                        print("   [ERRO] Contrato não achado no site.")

                if os.path.exists(ARQUIVO_EM_PROCESSAMENTO):
                    os.remove(ARQUIVO_EM_PROCESSAMENTO)

            # -------------------------------------------------------------
            # FASE 2: REANÁLISE RÁPIDA CONTÍNUA
            # -------------------------------------------------------------
            pendentes = carregar_pendentes()
            mudou_pendentes = False
            lista_pendentes = list(pendentes.items())

            for contrato, info in lista_pendentes:
                if os.path.exists(ARQUIVO_FILA):
                    print("\n[INTERRUPÇÃO] Novo contrato na fila! Pausando...")
                    break

                ultimo_keep_alive = time.time()

                # CORREÇÃO 1: Garante que a tentativa seja computada e salva de imediato
                info['tentativas'] = info.get('tentativas', 0) + 1
                tentativa_atual = info['tentativas']
                mudou_pendentes = True

                msg_tentativa = f"\n[REANÁLISE] Verificando {contrato} " \
                                f"(Tentativa {tentativa_atual}/{MAX_TENTATIVAS})..."
                print(msg_tentativa)

                if buscar_contrato(driver, contrato):
                    pagou = verificar_apenas_pagamento(driver)

                    if pagou:
                        print("   [PAGAMENTO] Buscando linha original...")
                        sheet = conectar_google_sheets(info['nome_planilha'])

                        if sheet:
                            linha_existente = encontrar_linha_do_contrato(
                                sheet, contrato
                            )

                            if linha_existente:
                                sheet.update_cell(
                                    linha_existente, 2, "1º Parcela Paga"
                                )
                                print(f"   [SUCESSO] Linha {linha_existente} atualizada.")
                            else:
                                print("   [AVISO] Nova linha de segurança gerada.")
                                dados_completos = extrair_dados_completos(driver)
                                atualizar_planilha(
                                    sheet, info['dados_originais'],
                                    dados_completos, contrato
                                )

                            salvar_historico_concluido(
                                contrato, info['nome_planilha'],
                                info['vendedor_nome'], info['vendedor_tel'],
                                "1º Parcela Paga (Reanálise)"
                            )
                            del pendentes[contrato]
                    else:
                        print("   [AINDA NÃO PAGO] Próximo...")
                        if tentativa_atual >= MAX_TENTATIVAS:
                            print("   [EXPIROU] Desistindo (limite atingido).")
                            del pendentes[contrato]
                else:
                    # CORREÇÃO 2: A Cota sumiu do sistema
                    print("   [NÃO ENCONTRADO] Cota excluída ou indisponível. Removendo da fila.")
                    del pendentes[contrato]

            if mudou_pendentes:
                salvar_pendentes(pendentes)

            # -------------------------------------------------------------
            # FASE 3: MANUTENÇÃO DE SESSÃO
            # -------------------------------------------------------------
            if (time.time() - ultimo_keep_alive) > TEMPO_INATIVIDADE_MAXIMO:
                if not manter_sessao_viva(driver):
                    print("[ERRO] Sessão perdida. Forçando Relogin.")
                    fazer_login_automatico(driver)
                ultimo_keep_alive = time.time()

            time.sleep(1)

        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"Erro Loop Geral: {e}")
            time.sleep(1)


if __name__ == "__main__":
    loop_servico()
