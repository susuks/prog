# Arquitetura do Sistema Enterprise V6 & CDA V2.9

## Visão Geral do Sistema

O sistema é uma arquitetura de microsserviços híbrida (Node.js + Python) desenvolvida para automatizar a inserção de vendas, o monitoramento de adimplência e a comunicação de mídia para o Consórcio Tradição (portal Autocred). O sistema opera de forma assíncrona, utilizando filas de processamento, extração de dados via requisições HTTP diretas e atualizações em lote (Batch Update) no Google Sheets.

## Módulos e Componentes Principais

### 1. Módulo de Captação (Node.js)

* **Arquivo:** `monitor.js`
* **Função:** Atua como um *listener* no WhatsApp (via `whatsapp-web.js`). Identifica mensagens de vendas no grupo alvo, extrai os dados via expressões regulares (Regex) e os empacota em um payload JSON.
* **Comunicação:** Dispara uma requisição HTTP POST para a API Python interna. Possui um mecanismo de resiliência (Fila de Retentativas) caso o servidor Python retorne erros de duplicidade, falha de infraestrutura ou queda de rede.

### 2. Motor Alpha: Servidor API e Validação (Python)

* **Arquivo:** `main.py`
* **Infraestrutura:** Servidor WSGI de produção (`Waitress`) rodando uma API REST (`Flask`). Protegido por autenticação via Bearer Token.
* **Função:** Recebe os dados do Node.js e orquestra a primeira inserção. Utiliza o Selenium estritamente para transpor barreiras de segurança, operando em modo headless.
* **Segurança:** Utiliza travas de thread (`threading.Lock`) para evitar colisões entre requisições concorrentes no navegador instanciado (`driver_api`).

### 3. Motor Beta: Controle de Adimplência - CDA (Python)

* **Arquivos:** `cda_main.py` e `cda_modulos.py`
* **Função:** Monitoramento financeiro contínuo de longo prazo dos contratos injetados.
* **Paradigma de Extração (HTTP Puro):** Em vez de utilizar o Selenium para varredura visual (lento e custoso em processamento), o CDA sequestra os *cookies* de sessão validados por Inteligência Artificial (CapSolver) e navega pelo portal Autocred disparando métodos POST forjados. A extração de dados é feita via manipulação do DOM (`BeautifulSoup`).

## Fluxo de Dados e Decisões de Engenharia

### A. Otimização de Busca via Memória RAM O(1)

Para mitigar a latência e as restrições de cota da API do Google Sheets, o sistema não realiza buscas de linhas diretamente nas planilhas a cada contrato.

* **Mecanismo:** A classe `GerenciadorCachePlanilhasCDA` faz o download integral da coluna de IDs de uma aba na primeira requisição, mapeando os contratos para os seus respectivos números de linha em um dicionário local na memória RAM.
* **Vantagem:** A complexidade de busca cai de O(N) para O(1). Consultas subsequentes ocorrem em milissegundos sem acionar os servidores do Google. Em caso de *Cache Miss* (um contrato que não está na RAM), a aba é automaticamente descarregada novamente para revalidação.

### B. Gravação Assíncrona em Lote (Batch Updates)

As requisições para a API do Google Sheets representavam o maior gargalo de tempo da aplicação.

* **Mecanismo:** O CDA analisa os clientes a uma velocidade de 0.8s por contrato (apenas leitura HTTP e comparação com JSON local). As alterações identificadas (Delta Updates) são armazenadas em um buffer. Quando o buffer atinge 20 itens, a carga é enviada para uma thread em segundo plano (`ThreadPoolExecutor`), que utiliza o método `batch_update` para atualizar todas as células modificadas em um único disparo.
* **Vantagem:** O *scanner* de auditoria nunca é interrompido pelas latências de I/O de disco ou rede externa.

### C. Gestão do "Limbo" e Abstenção

Contratos que retornam como "Não Encontrado" no portal podem indicar cancelamento, desistência ou erro temporário de sessão.

* **Mecanismo de Carência:** Tais contratos entram em estado de "Limbo" no arquivo `adimplencia.json`, recebendo um carimbo temporal de 45 dias para averiguações antes da exclusão definitiva.
* **Injeção Assíncrona:** A injeção da palavra "Inacessível" nas planilhas do vendedor e geral é despachada imediatamente para o `ThreadPoolExecutor`, isolando o problema e mantendo a fila principal intacta.

## Armazenamento de Estado Local

* **`config.txt`:** Gerenciamento centralizado de credenciais e parâmetros de intervalo.
* **`adimplencia.json`:** Banco de dados NoSQL de estado da aplicação. Mantém o histórico da última verificação, estado de pagamento, número de parcelas, e dados cadastrais base. É o pilar que permite os "Delta Updates" (só escreve no Sheets se houver alteração de estado no JSON).
* **Logs Segregados:** A aplicação gera saídas independentes (`crm_sistema.log` e `python_sistema.log`) com rotação de tamanho (RotatingFileHandler), garantindo rastreabilidade granular de erros de rede e mudanças de estado sem corromper o armazenamento do disco a longo prazo.
