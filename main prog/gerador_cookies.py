"""
Gerador de Sessão (Cookies) Local com Preenchimento Automático.
"""

import os
import json
import subprocess
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

def carregar_credenciais() -> dict:
    """Lê o arquivo config.txt localmente e extrai login e senha."""
    config = {"MATRICULA": "", "SENHA": ""}
    if os.path.exists("config.txt"):
        with open("config.txt", "r", encoding="utf-8") as f:
            for linha in f:
                if "=" in linha:
                    chave, valor = linha.split("=", 1)
                    config[chave.strip()] = valor.strip()
    return config

def clonar_e_enviar():
    """Abre o navegador, preenche login, extrai cookies e envia via SCP."""
    print("=== GERADOR DE COOKIES (BYPASS CAPTCHA) ===")
    credenciais = carregar_credenciais()
    usuario = credenciais.get("MATRICULA", "")
    senha = credenciais.get("SENHA", "")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install())
    )
    driver.get("https://intranet.consorciotradicao.com.br/autocred/")

    # Tentativa de auto-preenchimento
    if usuario and senha:
        try:
            # Lida com o frame inicial se existir
            WebDriverWait(driver, 3).until(
                EC.frame_to_be_available_and_switch_to_it((By.NAME, "mainFrame"))
            )
            driver.switch_to.default_content()
        except TimeoutException:
            pass

        try:
            campo_usuario = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "j_username"))
            )
            campo_usuario.send_keys(usuario)
            driver.find_element(By.ID, "j_password").send_keys(senha)
            print("\n[OK] Dados preenchidos com base no 'config.txt'!")
        except (TimeoutException, WebDriverException) as e:
            print(f"\n[AVISO] Falha ao preencher automaticamente: {e}")

    print("\n1. Resolva o Captcha das imagens.")
    print("2. Clique no botão de entrar.")
    print("3. QUANDO ESTIVER LOGADO (na tela inicial), volte aqui.")

    input("\n>>> Aperte [ENTER] para capturar o crachá e enviar... <<<")

    cookies = driver.get_cookies()
    with open("cookies.json", "w", encoding="utf-8") as f:
        json.dump(cookies, f)

    print("\n[OK] Crachá clonado e salvo no arquivo 'cookies.json'!")
    driver.quit()

    print("\nEnviando para o servidor na Hostinger...")
    comando_scp = 'scp cookies.json root@187.127.253.108:"/root/prog/main prog/"'

    try:
        subprocess.run(comando_scp, shell=True, check=True)
        print("\n[SUCESSO] Arquivo enviado para a nuvem!")
        print("O robô do servidor perceberá o arquivo novo e voltará a trabalhar.")
    except subprocess.CalledProcessError as e:
        print(f"\n[ERRO] O comando SCP falhou: {e}")

if __name__ == '__main__':
    clonar_e_enviar()
