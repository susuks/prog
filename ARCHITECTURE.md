# Arquitetura do Sistema Enterprise V6 & CDA V3.0

## Visão Geral do Sistema

O sistema evoluiu para uma arquitetura de microsserviços puramente HTTP, concebida para contornar as limitações estruturais e o estrangulamento de sessões do portal legado (ASP) do Consórcio Tradição. Eliminaram-se as varreduras visuais contínuas via Selenium, adotando-se uma separação clara entre a API de resposta rápida ao cliente (`site_main.py`) e a ingestão massiva de dados em background (`api_cda_main.py`).

## Módulos e Componentes Principais

### 1. Motor de Vendas e Resposta Rápida (`site_main.py`)

* **Infraestrutura:** Servidor WSGI de produção (`Waitress`) a correr uma API REST (`Flask`) na porta 5050.
* **Função:** Recebe os pedidos de validação de novos contratos, grava os dados fundamentais e devolve uma resposta imediata ao frontend.
* **Estratégia de Sessão (HTTP Puro):** O Selenium é instanciado de forma efémera, atuando apenas durante os primeiros segundos para invocar a Inteligência Artificial, forjar o login e transbordar os *cookies* para uma sessão persistente e ultraleve da biblioteca `requests`.
* **Filas e Bloqueios (Concorrência):** Devido à limitação do servidor ASP (que bloqueia "múltiplas abas" numa mesma sessão), a API utiliza um cadeado global (`lock_api`) para garantir navegação estritamente sequencial. Em caso de colisões de acessos simultâneos, o sistema devolve o código HTTP 429 (Too Many Requests), protegendo a estabilidade da intranet.
* **Processamento Assíncrono:** A extração pesada do PDF do boleto (QR Code e Linha Digitável) é relegada para uma *Thread* de background, que avisa o site posteriormente via Webhook, não retendo o cliente num ecrã de carregamento.
* **Hibernação Noturna:** Bloqueia interações entre as 22:00 e as 07:00 (UTC-4) para abater a sessão e limpar a memória. O sistema possui um motor proativo que deteta inatividade prolongada e restabelece os cookies silenciosamente antes que o primeiro vendedor do dia submeta um contrato.

### 2. Motor de Ingestão de Dados Massiva (`api_cda_main.py`)

* **Função:** Sincronização em massa da base de dados corporativa diretamente com o servidor web da Autocred, eliminando a dependência do Google Sheets para o histórico financeiro retroativo.
* **Padrão de Polling por Delta:** O script consulta exclusivamente a rota `ultima_atualizacao` a cada hora. O download pesado dos 5 endpoints essenciais (cotas, comissões, desistentes, atualização e parcelas) só é acionado se a *hash* desta rota sofrer alterações, reduzindo a zero o desperdício de tráfego de rede.
* **Streaming e Compressão (On-The-Fly):** Para evitar picos explosivos no uso de memória RAM (OOM Killer), os ficheiros JSON volumosos são descarregados em blocos microscópicos (chunks de 8192 bytes), comprimidos em formato GZIP (`.gz`) instantaneamente no disco e transferidos em fluxo contínuo direto (`application/gzip`) para o endpoint de destino.
* **Janela de Manutenção:** O módulo possui uma blindagem horária (UTC-3), saltando as extrações de dados entre as 08:00–08:59 e 19:00–19:59 para impedir a receção de pacotes corrompidos durante a manutenção interna da equipa de TI da Tradição.
* **Força de Sockets IPv4:** Modifica dinamicamente as permissões internas de rede do Python (`urllib3`) para impor conexões estritamente via IPv4, evadindo com sucesso os bloqueios impostos pelo firewall Cloudflare às placas de rede IPv6 da infraestrutura Hostinger.

## Tabela de Respostas HTTP (Interface de Integração)

O sistema central adota os padrões rigorosos de códigos de estado REST para orientar as respostas dinâmicas no painel frontend:

* **200 OK:** Sucesso absoluto. Contrato lido, dados extraídos e validados.
* **400 Bad Request:** Payload da requisição inexistente, malformado ou campos obrigatórios em falta.
* **403 Forbidden:** Token Bearer de autenticação ausente ou incorreto.
* **404 Not Found:** O contrato não existe no painel da Tradição (pode indicar contrato falso, cancelado ou ainda não averbado).
* **429 Too Many Requests:** Colisão (Deadlock Prevention). O motor robótico está ocupado a extrair dados pesados de um contrato anterior. O frontend deve apresentar uma contagem decrescente (ex: 20 segundos) e tentar novamente.
* **502 Bad Gateway:** Queda de rede na origem. O servidor ASP da Tradição está lento, engasgou ou a sessão de *cookies* foi derrubada subitamente no meio do percurso.
* **503 Service Unavailable:** Manutenção programada. O sistema encontra-se na sua janela de hibernação noturna (22h às 07h).

## Armazenamento e Logs

* **`config.txt`:** Ficheiro seguro e isolado para gestão centralizada do Token da API e das chaves de autorização transversais (`KEY` e `TOKEN_SITE`).
* **Logs Segregados Rotativos:** A aplicação mantém saídas de diagnóstico estruturadas e independentes (`site_sistema.log`). Utiliza o módulo nativo `RotatingFileHandler` com limites de tamanho rígidos, garantindo o registo dos eventos em background sem risco de corrupção ou enchimento do disco SSD a longo prazo.
