const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fs = require('fs');
const csv = require('csv-parser');
const axios = require('axios');
const winston = require('winston');

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
    "10": "Ctt.L + RMKT pessoal", "11": "Site", "12": "Contato Emanuel"
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
    puppeteer: { headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'] }
});

function normalizarLid(rawId) {
    if (!rawId) return '';
    try {
        const partes = rawId.split('@');
        return partes.length < 2 ? rawId : `${partes[0].split(':')[0]}@${partes[1]}`;
    } catch(e) { return rawId; }
}

function carregarMemorias() {
    if (fs.existsSync(ARQUIVO_CONFIG)) {
        const linhas = fs.readFileSync(ARQUIVO_CONFIG, 'utf8').split('\n');
        for (let linha of linhas) {
            if (linha.includes('=')) {
                const partes = linha.split('=');
                if (partes[0].trim() === 'NUMERO_ADMIN') NUMERO_ADMIN_CONFIG = partes[1].replace(/\D/g, '');
            }
        }
    }
    if (fs.existsSync(ARQUIVO_ADMIN)) {
        try { adminLid = JSON.parse(fs.readFileSync(ARQUIVO_ADMIN, 'utf8')).admin_lid; } catch (e) {}
    }
    mapaVendedores = {};
    if (fs.existsSync(ARQUIVO_VENDEDORES)) {
        fs.createReadStream(ARQUIVO_VENDEDORES).pipe(csv()).on('data', (row) => {
            const tel = (row.telefone || row.Telefone || "").replace(/\D/g, '');
            const nome = row.nome_planilha || row.nome || row.Nome;
            if (tel && nome) mapaVendedores[tel] = nome;
        });
    }
    if (fs.existsSync(ARQUIVO_LIDS)) { try { mapaLids = JSON.parse(fs.readFileSync(ARQUIVO_LIDS, 'utf8')); } catch (e) {} }
}

function salvarAdmin(lid) {
    adminLid = lid;
    fs.writeFileSync(ARQUIVO_ADMIN, JSON.stringify({ admin_lid: lid }, null, 4));
    logger.info(`[SEGURANÇA] Novo Administrador registrado e blindado no sistema: ${lid}`);
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
        logger.info(`Processando retentativa do contrato ${item.dadosVenda.contrato}...`);
        
        try {
            const resposta = await axios.post(URL_API_PYTHON, item.dadosVenda, { 
                headers: { 'Authorization': `Bearer ${TOKEN_API}` },
                timeout: 180000 
            });
            const status = resposta.data.status_pagamento;
            const planilha = resposta.data.planilha;
            item.loadingMsg.edit(`[REGISTRADO] Contrato validado com sucesso (após retentativa).\n\nContrato: ${item.dadosVenda.contrato}\nVendedor: ${item.dadosVenda.vendedor}\nPlanilha: ${planilha}\nStatus: ${status}`);
        } catch (erroApi) {
            let msgTratada = "Falha de comunicação ou erro interno.";

            if (erroApi.response && erroApi.response.data) {
                const tipoErro = erroApi.response.data.erro;
                msgTratada = erroApi.response.data.mensagem || msgTratada;

                if (tipoErro === 'duplicidade') {
                    item.loadingMsg.edit(`[REGISTRADO] ${msgTratada}`);
                    logger.info(`Retentativa cancelada: Contrato ${item.dadosVenda.contrato} já se encontrava registrado.`);
                    return; 
                } else if (tipoErro === 'planilha_ausente') {
                    item.loadingMsg.edit(`[ERRO DE SISTEMA] Falha definitiva na retentativa.\n\n${msgTratada}\n\nO bot não tentará novamente até que as planilhas sejam criadas.`);
                    logger.error(`Retentativa abortada por falha de infraestrutura.`);
                    return; 
                }
            }

            item.tentativas += 1;
            if (item.tentativas < 3) {
                filaRetentativas.push(item);
                logger.warn(`Falha na retentativa ${item.dadosVenda.contrato}. Devolvido para a fila. Detalhe: ${msgTratada}`);
            } else {
                item.loadingMsg.edit(`[ERRO] Falha definitiva ao processar contrato após 3 tentativas em background.\n\nContrato: ${item.dadosVenda.contrato}\nÚltimo Erro: ${msgTratada}`);
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
        
        if (chat.isGroup) return;
        
        const isPrivado = true;
        const idSessaoBruto = normalizarLid(msg.author || msg.from); 

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

        if (!adminLid) {
            if (comandoCadastro) {
                if (comandoCadastro === NUMERO_ADMIN_CONFIG) {
                    salvarAdmin(idSessaoBruto);
                    msg.reply(`[SISTEMA] Autoridade máxima reconhecida. O sistema está agora destrancado.`);
                } else {
                    msg.reply(`[ERRO DE SEGURANÇA] O número fornecido não coincide com a chave mestra configurada no sistema.`);
                }
            } else if (isPrivado && !msg.fromMe) {
                msg.reply(`[SISTEMA TRANCADO] O Administrador do sistema ainda não efetuou o login inicial.\n\nSe você é o administrador, envie o seu número cadastrado no arquivo config.txt no seguinte formato:\n*-SEUNUMERO-*`);
            }
            return; 
        }

        const isAdmin = (idSessaoBruto === adminLid);

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
                logger.info(`Enviando Contrato: ${dadosVenda.contrato} | Vendedor: ${nomeVendedor}`);

                try {
                    const respostaApp = await axios.post(URL_API_PYTHON, dadosVenda, { 
                        headers: { 'Authorization': `Bearer ${TOKEN_API}` },
                        timeout: 180000
                    });
                    const status = respostaApp.data.status_pagamento;
                    const planilha = respostaApp.data.planilha;
                    
                    loadingMsg.edit(`[REGISTRADO] Contrato validado com sucesso.\n\nContrato: ${dadosVenda.contrato}\nVendedor: ${nomeVendedor}\nPlanilha: ${planilha}\nStatus: ${status}`);

                } catch (erroApi) {
                    // Verificação estrita da resposta da API
                    if (erroApi.response && erroApi.response.data) {
                        const tipoErro = erroApi.response.data.erro;
                        const msgErro = erroApi.response.data.mensagem || 'Erro desconhecido retornado pela API.';

                        if (tipoErro === 'duplicidade') {
                            loadingMsg.edit(`[REGISTRADO] ${msgErro}`);
                            logger.info(`Contrato ${dadosVenda.contrato} ignorado na fila. Motivo: Duplicidade (409).`);
                        
                        } else if (tipoErro === 'planilha_ausente') {
                            loadingMsg.edit(`[ERRO DE SISTEMA] ${msgErro}\n\nCrie as planilhas ou abas ausentes e reenvie a mensagem para tentar novamente.`);
                            logger.error(`Falha de infraestrutura no contrato ${dadosVenda.contrato}.`);
                        
                        } else if (tipoErro === 'contrato_nao_encontrado') {
                            logger.warn(`Contrato ${dadosVenda.contrato} não encontrado no portal. Transferindo para fila de resiliência.`);
                            filaRetentativas.push({ dadosVenda, loadingMsg, tentativas: 0 });
                            loadingMsg.edit(`[AVISO] ${msgErro}\n\nO bot tentará encontrar o contrato novamente em background (esperando o portal atualizar).`);
                        
                        } else {
                            // Cobre 'falha_gravacao' ou 'erro_interno'
                            logger.error(`Falha no contrato ${dadosVenda.contrato} (${tipoErro}). Transferindo para resiliência.`);
                            filaRetentativas.push({ dadosVenda, loadingMsg, tentativas: 0 });
                            loadingMsg.edit(`[AVISO] Lentidão ou falha de gravação detectada.\nDetalhe: ${msgErro}\n\nO bot transferiu o contrato para a fila de retentativas.`);
                        }
                    } else {
                        // Trata quedas de rede onde o Node sequer consegue falar com o Python
                        logger.error(`Falha de comunicação offline no contrato ${dadosVenda.contrato}. Transferindo para fila de resiliência.`);
                        filaRetentativas.push({ dadosVenda, loadingMsg, tentativas: 0 });
                        loadingMsg.edit(`[AVISO] Falha de comunicação com o motor Python. O bot tentará registrar o contrato ${dadosVenda.contrato} novamente em background.`);
                    }
                }

            } else {
                msg.reply(`[AVISO] Contrato detectado, mas o usuário não possui permissão.\nPor favor, envie o seu número entre hífens para solicitar acesso ao administrador:\n*-556799999999-*\n\nNota: Não inclua o 9 adicional do WhatsApp no número.`);
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
    logger.info('>>> MONITOR V14.0 (MAPEAMENTO DE ERROS EXPLÍCITO) INICIADO <<<');
    carregarMemorias();
});

client.on('message_create', async (msg) => {
    if (!sistemaIniciado) return;
    await processarMensagem(msg);
});

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