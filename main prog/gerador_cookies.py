"""_summary_
"""
import json
import subprocess
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

def clonar_e_enviar():
    """_summary_
    """
    print("=== GERADOR DE COOKIES (BYPASS CAPTCHA) ===")

    # Inicia o Chrome visível
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()))
    driver.get("https://intranet.consorciotradicao.com.br/autocred/")

    print("\n1. O navegador foi aberto na sua tela.")
    print("2. Faça o login normalmente e resolva o Captcha das imagens.")
    print("3. QUANDO ESTIVER LOGADO (na tela inicial do sistema), volte aqui.")

    input("\n>>> Aperte [ENTER] para capturar o crachá e enviar para o Servidor... <<<")

    # Captura os cookies da sessão atual
    cookies = driver.get_cookies()
    with open("cookies.json", "w", encoding="utf-8") as f:
        json.dump(cookies, f)

    print("\n[OK] Crachá clonado e salvo no arquivo 'cookies.json'!")
    driver.quit()

    # Prepara o comando de envio SCP do Windows para a Hostinger
    print("\nEnviando para o servidor na Hostinger...")

    # Atenção: As aspas duplas protegem o espaço na pasta "main prog"
    comando_scp = 'scp cookies.json root@187.127.253.108:"/root/prog/main prog/"'

    try:
        # Executa o comando invisível no PowerShell
        subprocess.run(comando_scp, shell=True, check=True)
        print("\n[SUCESSO ABSOLUTO] Arquivo enviado para a nuvem!")
        print("O robô do servidor perceberá o arquivo em um minuto e voltará a trabalhar.")
    except Exception as e: # pylint: disable=broad-exception-caught
        print(f"\n[ERRO] O comando SCP falhou: {e}")
        print("Verifique se o Windows pediu a senha do servidor e você não viu.")

if __name__ == '__main__':
    clonar_e_enviar()
