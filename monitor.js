const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fs = require('fs');
const csv = require('csv-parser');
const axios = require('axios');
const winston = require('winston');

// --- CONFIGURAÇÕES ENTERPRISE ---
const NOME_GRUPO_ALVO = 'VENDAS'; 
const ARQUIVO_VENDEDORES = 'files/vendedores.csv';
const ARQUIVO_LIDS = 'files/mapeamento_lids.json';
const URL_API_PYTHON = 'http://127.0.0.1:5000/processar_venda';
const TOKEN_API = 'CHAVE_SECRETA_ENTERPRISE_V6'; // Segurança interna

// SUBSTITUA PELO SEU NÚMERO (Ex: 556799999999@c.us)
const NUMERO_ADMIN = '556799999999@c.us'; 

const MAPA_ORIGENS = {
    "1": "DuoTalk", "2": "Tráfego", "3": "Remarketing", "4": "Contato Lucas",
    "5": "Outro", "6": "Indicação", "7": "RMKT + RMKT pessoal",
    "8": "TRFG + RMKT pessoal", "9": "DT + RMKT pessoal",
    "10": "Ctt.L + RMKT pessoal", "11": "Site"
};

let mapaVendedores = {};
let mapaLids = {};
let pendentesAprovacao = {}; // Fila de aprovação de 10 min
let filaRetentativas = [];   // Fila de resiliência
let sistemaIniciado = false;

// --- CONFIGURAÇÃO DE LOGGING (WINSTON) ---
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

// --- FUNÇÕES DE MEMÓRIA E NORMALIZAÇÃO ---
function normalizarId(idBruto) {
    if (!idBruto) return '';
    return idBruto.replace(/:.*?@/, '@');
}

function carregarMemorias() {
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

// --- FILA DE RETENTATIVAS (RESILIÊNCIA) ---
setInterval(async () => {
    if (filaRetentativas.length > 0) {
        const item = filaRetentativas.shift();
        logger.info(`Processando retentativa do contrato ${item.dadosVenda.contrato}...`);
        
        try {
            const resposta = await axios.post(URL_API_PYTHON, item.dadosVenda, { 
                headers: { 'Authorization': `Bearer ${TOKEN_API}` },
                timeout: 60000 
            });
            const status = resposta.data.status_pagamento;
            const planilha = resposta.data.planilha;
            item.loadingMsg.edit(`[REGISTRADO] Contrato validado com sucesso (após retentativa).\\n\\nContrato: ${item.dadosVenda.contrato}\\nVendedor: ${item.dadosVenda.vendedor}\\nPlanilha: ${planilha}\\nStatus: ${status}`);
        } catch (erroApi) {
            item.tentativas += 1;
            if (item.tentativas < 3) {
                filaRetentativas.push(item);
                logger.warn(`Falha na retentativa ${item.dadosVenda.contrato}. Devolvido para a fila.`);
            } else {
                item.loadingMsg.edit(`[ERRO] Falha definitiva ao processar contrato após múltiplas tentativas.\\n\\nContrato: ${item.dadosVenda.contrato}`);
                logger.error(`Abandono de retentativa para o contrato ${item.dadosVenda.contrato}.`);
            }
        }
    }
}, 30000); // Processa a fila a cada 30 segundos

// --- NÚCLEO DE PROCESSAMENTO ---
async function processarMensagem(msg) {
    try {
        if (msg.from === 'status@broadcast') return;
        const corpoMsg = msg.body;
        if (!corpoMsg) return;

        const idRemetente = normalizarId(msg.author || msg.from);
        const chat = await msg.getChat();
        const isGrupoAlvo = chat.isGroup && chat.name && chat.name.toUpperCase() === NOME_GRUPO_ALVO.toUpperCase();
        const isPrivado = !chat.isGroup;

        // 1. FLUXO DE APROVAÇÃO ADMINISTRATIVA (Comando #ap#NUMERO#)
        if (idRemetente === NUMERO_ADMIN && corpoMsg.startsWith('#ap#') && corpoMsg.endsWith('#')) {
            const numAprovado = corpoMsg.replace(/\D/g, '');
            if (pendentesAprovacao[numAprovado]) {
                const idVendedor = pendentesAprovacao[numAprovado].idRemetente;
                mapaLids[idVendedor] = numAprovado;
                salvarMapaLids();
                
                delete pendentesAprovacao[numAprovado];
                
                msg.reply(`[ADMINISTRATIVO] Acesso liberado para o número ${numAprovado}.`);
                client.sendMessage(idVendedor, `[SUCESSO] O seu acesso foi aprovado pela administração. Você já pode enviar contratos.`);
                logger.info(`Administrador aprovou o número ${numAprovado}.`);
            } else {
                msg.reply(`[AVISO] O número ${numAprovado} não possui solicitação pendente ou o tempo expirou.`);
            }
            return;
        }

        // 2. FLUXO DE SOLICITAÇÃO DE CADASTRO (#NUMERO#)
        const matchCadastro = corpoMsg.match(/^#(\d+)#$/);
        if (matchCadastro) {
            const numeroFornecido = matchCadastro[1];
            
            let nomeEncontrado = null;
            for (let tel in mapaVendedores) {
                if (tel.includes(numeroFornecido) || numeroFornecido.includes(tel)) {
                    nomeEncontrado = mapaVendedores[tel];
                    break;
                }
            }

            if (nomeEncontrado) {
                pendentesAprovacao[numeroFornecido] = { idRemetente: idRemetente };
                
                msg.reply(`[AGUARDANDO] Identidade reconhecida (${nomeEncontrado}). Solicitação enviada à administração. Aguarde aprovação.`);
                client.sendMessage(NUMERO_ADMIN, `[SOLICITAÇÃO DE ACESSO]\\nVendedor: ${nomeEncontrado}\\nNúmero: ${numeroFornecido}\\n\\nResponda com o comando exato abaixo para aprovar:\\n#ap#${numeroFornecido}#`);
                logger.info(`Nova solicitação de acesso de ${nomeEncontrado} (${numeroFornecido}).`);

                // Timeout de 10 minutos
                setTimeout(() => {
                    if (pendentesAprovacao[numeroFornecido]) {
                        delete pendentesAprovacao[numeroFornecido];
                        client.sendMessage(idRemetente, `[RECUSADO] O tempo para aprovação da sua solicitação expirou (10 minutos). Tente novamente.`);
                        client.sendMessage(NUMERO_ADMIN, `[AVISO] A solicitação do número ${numeroFornecido} expirou.`);
                        logger.info(`Solicitação de ${numeroFornecido} expirou por falta de ação administrativa.`);
                    }
                }, 600000);
            } else {
                msg.reply(`[ERRO] Número não encontrado na base mestra de vendedores autorizados.`);
            }
            return;
        }

        // 3. FLUXO DE VENDA (API REST)
        if (isGrupoAlvo || isPrivado) {
            const dadosVenda = extrairDados(corpoMsg);
            
            if (dadosVenda) {
                const numeroReal = mapaLids[idRemetente] || idRemetente.replace(/\D/g, '');
                
                let nomeVendedor = "Desconhecido";
                for (let tel in mapaVendedores) {
                    if (numeroReal.includes(tel) || tel.includes(numeroReal)) {
                        nomeVendedor = mapaVendedores[tel];
                        break;
                    }
                }

                if (nomeVendedor !== "Desconhecido") {
                    dadosVenda.vendedor = nomeVendedor;
                    dadosVenda.telefone = numeroReal;

                    const loadingMsg = await msg.reply(`[PROCESSANDO] O contrato ${dadosVenda.contrato} está sendo analisado...`);
                    logger.info(`Enviando Contrato: ${dadosVenda.contrato} | Vendedor: ${nomeVendedor}`);

                    try {
                        const respostaPython = await axios.post(URL_API_PYTHON, dadosVenda, { 
                            headers: { 'Authorization': `Bearer ${TOKEN_API}` },
                            timeout: 60000 
                        });
                        const status = respostaPython.data.status_pagamento;
                        const planilha = respostaPython.data.planilha;
                        
                        loadingMsg.edit(`[REGISTRADO] Contrato validado com sucesso.\\n\\nContrato: ${dadosVenda.contrato}\\nVendedor: ${nomeVendedor}\\nPlanilha: ${planilha}\\nStatus: ${status}`);

                    } catch (erroApi) {
                        logger.error(`Falha inicial no contrato ${dadosVenda.contrato}. Transferindo para fila de resiliência.`);
                        filaRetentativas.push({ dadosVenda, loadingMsg, tentativas: 0 });
                        loadingMsg.edit(`[AVISO] Falha de comunicação. O sistema tentará registrar o contrato ${dadosVenda.contrato} novamente em background.`);
                    }

                } else {
                    msg.reply(`[AVISO] Contrato detectado, mas o usuário não possui permissão.\\nPor favor, envie o seu número entre hashtags para solicitar acesso ao administrador:\\n*#556799999999#*`);
                }
            }
        }
    } catch (e) {
        logger.error(`Erro na rotina principal: ${e.message}`);
    }
}

// --- EVENTOS ---
client.on('qr', (qr) => qrcode.generate(qr, { small: true }));

client.on('ready', async () => {
    if (sistemaIniciado) return;
    sistemaIniciado = true;
    logger.info('>>> MONITOR V11.0 (ENTERPRISE: APROVAÇÃO, RESILIÊNCIA E AUTH) INICIADO <<<');
    carregarMemorias();
});

client.on('message_create', async (msg) => {
    if (!sistemaIniciado) return;
    await processarMensagem(msg);
});

client.initialize();