const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fs = require('fs');
const csv = require('csv-parser');
const axios = require('axios');
const winston = require('winston');

const NOME_GRUPO_ALVO = 'VENDAS'; 
const ARQUIVO_VENDEDORES = 'files/vendedores.csv';
const ARQUIVO_LIDS = 'files/mapeamento_lids.json';
const ARQUIVO_ADMIN = 'files/admin_data.json';
const ARQUIVO_CONFIG = 'files/config.txt';
const URL_API_PYTHON = 'http://127.0.0.1:5000/processar_venda';
const TOKEN_API = 'CHAVE_SECRETA_ENTERPRISE_V6'; 

const MAPA_ORIGENS = {
    "1": "DuoTalk", "2": "Tráfego", "3": "Remarketing", "4": "Contato Lucas",
    "5": "Outro", "6": "Indicação", "7": "RMKT + RMKT pessoal",
    "8": "TRFG + RMKT pessoal", "9": "DT + RMKT pessoal",
    "10": "Ctt.L + RMKT pessoal", "11": "Site"
};

let NUMERO_ADMIN_CONFIG = '';
let adminLid = null;
let mapaVendedores = {};
let mapaLids = {};
let pendentesAprovacao = {}; 
let filaRetentativas = [];   
let sistemaIniciado = false;

const logger = winston.createLogger({
    level: 'info',
    format: winston.format.combine(
        winston.format.timestamp({ format: 'YYYY-MM-DD HH:mm:ss' }),
        winston.format.printf(info => `[${info.timestamp}] ${info.level.toUpperCase()}: ${info.message}`)
    ),
    transports: [
        new winston.transports.Console(),
        new winston.transports.File({ filename: 'files/nodejs_error.log', level: 'error' }),
        new winston.transports.File({ filename: 'files/nodejs_sistema.log' })
    ]
});

const client = new Client({
    authStrategy: new LocalAuth(),
    authTimeoutMs: 120000, 
    puppeteer: { 
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
    }
});

// NOVA FUNÇÃO: Remove o sufixo de dispositivo (ex: :13) do LID
function normalizarLid(rawId) {
    if (!rawId) return '';
    return rawId.replace(/:.*?@/, '@');
}

function carregarMemorias() {
    // 1. Carrega o Número do Admin do config.txt
    if (fs.existsSync(ARQUIVO_CONFIG)) {
        const linhas = fs.readFileSync(ARQUIVO_CONFIG, 'utf8').split('\n');
        for (let linha of linhas) {
            if (linha.includes('=')) {
                const partes = linha.split('=');
                if (partes[0].trim() === 'NUMERO_ADMIN') {
                    NUMERO_ADMIN_CONFIG = partes[1].replace(/\D/g, '');
                }
            }
        }
    }
    if (!NUMERO_ADMIN_CONFIG) {
        logger.error("[CRÍTICO] NUMERO_ADMIN não foi encontrado no ficheiro config.txt!");
    }

    // 2. Carrega o LID (Identificador da Meta) do Administrador
    if (fs.existsSync(ARQUIVO_ADMIN)) {
        try {
            const dados = JSON.parse(fs.readFileSync(ARQUIVO_ADMIN, 'utf8'));
            if (dados.admin_lid) {
                adminLid = dados.admin_lid;
                logger.info(`Administrador reconhecido na memória (LID: ${adminLid}).`);
            }
        } catch (e) {
            logger.error("Falha ao ler o ficheiro admin_data.json.");
        }
    } else {
        logger.warn("[SISTEMA] Modo de Setup: Aguardando primeiro cadastro do Administrador via WhatsApp.");
    }

    // 3. Carrega os Vendedores e LIDs
    mapaVendedores = {};
    if (fs.existsSync(ARQUIVO_VENDEDORES)) {
        fs.createReadStream(ARQUIVO_VENDEDORES)
            .pipe(csv({ mapHeaders: ({ header }) => header.trim().replace(/^[\uFEFF\xEF\xBB\xBF]+/, '') }))
            .on('data', (row) => {
                try {
                    const telBruto = row.telefone || row.Telefone || "";
                    const tel = telBruto ? String(telBruto).replace(/\D/g, '') : null;
                    const nome = row.nome_planilha || row.nome || row.Nome;
                    if (tel && nome) mapaVendedores[tel] = nome;
                } catch (e) {}
            })
            .on('end', () => logger.info(`${Object.keys(mapaVendedores).length} vendedores mestres carregados.`));
    }

    if (fs.existsSync(ARQUIVO_LIDS)) {
        try {
            mapaLids = JSON.parse(fs.readFileSync(ARQUIVO_LIDS, 'utf8'));
            logger.info(`${Object.keys(mapaLids).length} IDs reconhecidos na memória.`);
        } catch (e) { mapaLids = {}; }
    }
}

function salvarAdmin(lid) {
    adminLid = lid;
    fs.writeFileSync(ARQUIVO_ADMIN, JSON.stringify({ admin_lid: lid }, null, 4));
    logger.info(`[SEGURANÇA] Novo Administrador registado e blindado no sistema: ${lid}`);
}

function salvarMapaLids() {
    fs.writeFileSync(ARQUIVO_LIDS, JSON.stringify(mapaLids, null, 4));
}

function extrairDados(texto) {
    const regex = /(\d{5,})\s*,\s*([^,]+)(?:\s*,\s*([\d\.]+))?/;
    const match = texto.match(regex);
    if (match) {
        let origemExtraida = match[2].trim();
        let origemFinal = MAPA_ORIGENS[origemExtraida] || origemExtraida;
        return { contrato: match[1].trim(), origem: origemFinal, lance: match[3] ? match[3].trim() : "0" };
    }
    return null;
}
setInterval(async () => {
    if (filaRetentativas.length > 0) {
        const item = filaRetentativas.shift();
        logger.info(`A processar retentativa do contrato ${item.dadosVenda.contrato}...`);
        
        try {
            const resposta = await axios.post(URL_API_PYTHON, item.dadosVenda, { 
                headers: { 'Authorization': `Bearer ${TOKEN_API}` },
                timeout: 60000 
            });
            const status = resposta.data.status_pagamento;
            const planilha = resposta.data.planilha;
            item.loadingMsg.edit(`[REGISTRADO] Contrato validado com sucesso (após retentativa).\n\nContrato: ${item.dadosVenda.contrato}\nVendedor: ${item.dadosVenda.vendedor}\nPlanilha: ${planilha}\nStatus: ${status}`);
        } catch (erroApi) {
            item.tentativas += 1;
            if (item.tentativas < 3) {
                filaRetentativas.push(item);
                logger.warn(`Falha na retentativa ${item.dadosVenda.contrato}. Devolvido para a fila.`);
            } else {
                item.loadingMsg.edit(`[ERRO] Falha definitiva ao processar contrato após múltiplas tentativas.\n\nContrato: ${item.dadosVenda.contrato}`);
                logger.error(`Abandono de retentativa para o contrato ${item.dadosVenda.contrato}.`);
            }
        }
    }
}, 30000); 

async function processarMensagem(msg) {
    try {
        if (msg.from === 'status@broadcast') return;
        const corpoMsg = msg.body;
        if (!corpoMsg) return;

        const chat = await msg.getChat();
        
        // BLOQUEIO: Ignora completamente qualquer mensagem de grupos
        if (chat.isGroup) return;
        
        const isGrupoAlvo = false; // Mantido apenas para compatibilidade de variáveis da V12
        const isPrivado = true;
        
        // APLICAÇÃO: O identificador de sessão é limpo de qualquer sufixo (:13, :14)
        const idSessaoBruto = normalizarLid(msg.author || msg.from); 

        // =====================================================================
        // EXTRAÇÃO ESTRITA DE COMANDOS (Ignora lixo e formatações ocultas)
        // =====================================================================
        const textoLimpo = corpoMsg.trim();

        let comandoAprovacao = null;
        const matchAp = textoLimpo.match(/^@(\d+)@$/);
        if (matchAp) comandoAprovacao = matchAp[1];

        let comandoCadastro = null;
        const matchCad = textoLimpo.match(/^-(\d+)-$/);
        if (matchCad) comandoCadastro = matchCad[1];

        if (comandoAprovacao || comandoCadastro) {
            logger.info(`[SISTEMA LIDA] ID Sessão: '${idSessaoBruto}' | Aprov: '${comandoAprovacao}' | Cad: '${comandoCadastro}'`);
        }

        // =====================================================================
        // MODO SETUP: AGUARDANDO CONFIGURAÇÃO DO ADMINISTRADOR
        // =====================================================================
        if (!adminLid) {
            if (comandoCadastro) {
                if (comandoCadastro === NUMERO_ADMIN_CONFIG) {
                    salvarAdmin(idSessaoBruto);
                    msg.reply(`[SISTEMA] Autoridade máxima reconhecida. Você foi registado como Administrador com sucesso! O sistema está agora destrancado.`);
                } else {
                    msg.reply(`[ERRO DE SEGURANÇA] O número fornecido não coincide com a chave mestra configurada no sistema.`);
                }
            } else if (isPrivado && !msg.fromMe) {
                msg.reply(`[SISTEMA TRANCADO] O Administrador do sistema ainda não efetuou o login inicial.\n\nSe você é o administrador, envie o seu número cadastrado no config.txt no seguinte formato:\n*-SEUNUMERO-*`);
            }
            return; // Bloqueia todas as outras funções até o Admin existir
        }

        // =====================================================================
        // MODO NORMAL: OPERAÇÃO PADRÃO DO SISTEMA
        // =====================================================================
        const isAdmin = (idSessaoBruto === adminLid);

        // 1. FLUXO DE APROVAÇÃO ADMINISTRATIVA (@NUMERO@)
        if (isAdmin && comandoAprovacao) {
            const numAprovado = comandoAprovacao;
            if (pendentesAprovacao[numAprovado]) {
                const idVendedorRaw = pendentesAprovacao[numAprovado].idSessaoBruto;
                mapaLids[idVendedorRaw] = numAprovado;
                salvarMapaLids();
                
                delete pendentesAprovacao[numAprovado];
                
                msg.reply(`[ADMINISTRATIVO] Acesso liberado para o número ${numAprovado}.`);
                client.sendMessage(idVendedorRaw, `[SUCESSO] O seu acesso foi aprovado pela administração. Você já pode enviar contratos.`);
                logger.info(`Administrador autorizou o ID de sessão: ${idVendedorRaw} -> Número: ${numAprovado}`);
            } else {
                msg.reply(`[AVISO] O número ${numAprovado} não possui solicitação pendente ou o tempo expirou.`);
            }
            return;
        }

        // 2. FLUXO DE SOLICITAÇÃO DO VENDEDOR (-NUMERO-)
        if (comandoCadastro && !isAdmin) {
            const numeroFornecido = comandoCadastro;
            
            let nomeEncontrado = null;
            for (let tel in mapaVendedores) {
                if (tel.includes(numeroFornecido) || numeroFornecido.includes(tel)) {
                    nomeEncontrado = mapaVendedores[tel];
                    break;
                }
            }

            if (nomeEncontrado) {
                pendentesAprovacao[numeroFornecido] = { idSessaoBruto: idSessaoBruto };
                
                msg.reply(`[AGUARDANDO] Identidade reconhecida (${nomeEncontrado}). Solicitação enviada à administração. Aguarde aprovação.`);
                
                // Dispara o pedido de aprovação para o LID do Administrador Supremo
                client.sendMessage(adminLid, `[SOLICITAÇÃO DE ACESSO]\nVendedor: ${nomeEncontrado}\nNúmero: ${numeroFornecido}\n\nResponda com o comando exato abaixo para aprovar:\n@${numeroFornecido}@`);
                logger.info(`Nova solicitação de acesso de ${nomeEncontrado} (${numeroFornecido}).`);

                setTimeout(() => {
                    if (pendentesAprovacao[numeroFornecido]) {
                        delete pendentesAprovacao[numeroFornecido];
                        client.sendMessage(idSessaoBruto, `[RECUSADO] O tempo para aprovação da sua solicitação expirou (10 minutos). Tente novamente.`);
                        client.sendMessage(adminLid, `[AVISO] A solicitação do número ${numeroFornecido} expirou por inatividade.`);
                        logger.info(`Solicitação de ${numeroFornecido} expirou.`);
                    }
                }, 600000);
            } else {
                msg.reply(`[ERRO] Número não encontrado na base mestra de vendedores autorizados.`);
            }
            return;
        }

        // 3. FLUXO DE PROCESSAMENTO DE CONTRATO
        if (isGrupoAlvo || isPrivado) {
            const dadosVenda = extrairDados(corpoMsg);
            
            if (dadosVenda) {
                let numeroIdentificador = mapaLids[idSessaoBruto];
                
                if (isAdmin) {
                    numeroIdentificador = NUMERO_ADMIN_CONFIG;
                }

                let nomeVendedor = "Desconhecido";
                if (numeroIdentificador) {
                    for (let tel in mapaVendedores) {
                        if (numeroIdentificador.includes(tel) || tel.includes(numeroIdentificador)) {
                            nomeVendedor = mapaVendedores[tel];
                            break;
                        }
                    }
                }

                if (nomeVendedor !== "Desconhecido") {
                    dadosVenda.vendedor = nomeVendedor;
                    dadosVenda.telefone = numeroIdentificador;

                    const loadingMsg = await msg.reply(`[PROCESSANDO] O contrato ${dadosVenda.contrato} está sendo analisado...`);
                    logger.info(`A enviar Contrato: ${dadosVenda.contrato} | Vendedor: ${nomeVendedor}`);

                    try {
                        const respostaPython = await axios.post(URL_API_PYTHON, dadosVenda, { 
                            headers: { 'Authorization': `Bearer ${TOKEN_API}` },
                            timeout: 60000 
                        });
                        const status = respostaPython.data.status_pagamento;
                        const planilha = respostaPython.data.planilha;
                        
                        loadingMsg.edit(`[REGISTRADO] Contrato validado com sucesso.\n\nContrato: ${dadosVenda.contrato}\nVendedor: ${nomeVendedor}\nPlanilha: ${planilha}\nStatus: ${status}`);

                    } catch (erroApi) {
                        logger.error(`Falha inicial no contrato ${dadosVenda.contrato}. Transferindo para fila de resiliência.`);
                        filaRetentativas.push({ dadosVenda, loadingMsg, tentativas: 0 });
                        loadingMsg.edit(`[AVISO] Falha de comunicação. O sistema tentará registrar o contrato ${dadosVenda.contrato} novamente em background.`);
                    }

                } else {
                    msg.reply(`[AVISO] Contrato detetado, mas o utilizador não possui permissão.\nPor favor, envie o seu número entre hífens para solicitar acesso ao administrador:\n*-556799999999-*\n\nNota: Não inclua o 9 adicional do WhatsApp no número.`);
                }
            }
        }
    } catch (e) {
        logger.error(`Erro na rotina principal: ${e.message}`);
    }
}

client.on('qr', (qr) => qrcode.generate(qr, { small: true }));

client.on('ready', async () => {
    if (sistemaIniciado) return;
    sistemaIniciado = true;
    logger.info('>>> MONITOR V12.0 (MODIFICADO COM LID E BLOQUEIO DE GRUPOS) INICIADO <<<');
    carregarMemorias();
});

client.on('message_create', async (msg) => {
    if (!sistemaIniciado) return;
    await processarMensagem(msg);
});

// =====================================================================
// ENCERRAMENTO GRACIOSO (Prevenção de Processos Zumbis e File Lock)
// =====================================================================
process.on('SIGINT', async () => {
    logger.info("Sinal de interrupção recebido. Encerrando o navegador com segurança...");
    try {
        if (client) {
            await client.destroy();
            logger.info("Navegador do WhatsApp encerrado com sucesso.");
        }
        process.exit(0);
    } catch (err) {
        logger.error(`Erro ao tentar encerrar os processos: ${err.message}`);
        process.exit(1);
    }
});

client.initialize();