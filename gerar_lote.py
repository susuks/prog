# Nome dos arquivos
arquivo_entrada = "codigos.txt"
arquivo_saida = "fila_vendas.csv"

# Seus dados fixos
origem = "1"
vendedor = "1"
lance_livre = "0"
telefone = "556781494851"

try:
    with open(arquivo_entrada, "r", encoding="utf-8") as f_in:
        # Lê todos os códigos removendo espaços em branco extras
        contratos = [linha.strip() for linha in f_in if linha.strip()]

    with open(arquivo_saida, "w", encoding="utf-8") as f_out:
        # Escreve o cabeçalho obrigatório
        f_out.write("contrato,origem,vendedor,lance livre,telefone\n")

        # Escreve cada linha de contrato
        for contrato in contratos:
            f_out.write(f"{contrato},{origem},{vendedor},{lance_livre},{telefone}\n")

    print(
        f"SUCESSO! {len(contratos)} contratos foram formatados e salvos em '{arquivo_saida}'."
    )

except FileNotFoundError:
    print(f"Erro: O arquivo '{arquivo_entrada}' não foi encontrado.")
