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
// Mantido apontamento original para o main.py na porta 5000
const URL_API_PYTHON = 'http://127.0.0.1:5000/processar_venda';

const MAPA_ORIGENS = {
    "1": "DuoTalk", "2": "Tráfego", "3": "Remarketing", "4": "Contato Lucas",
    "5": "Outro", "6": "Indicação", "7": "RMKT + RMKT pessoal",
    "8": "TRFG + RMKT pessoal", "9": "DT + RMKT pessoal",
    "10": "Ctt.L + RMKT pessoal", "11": "Site", "12": "Contato Emanuel"
};

let NUMERO_ADMIN_CONFIG = '';
let TOKEN_API_CONFIG = ''; // [NOVO] Token agora é carregado dinamicamente do config.txt
let adminLid = null;
let mapaVendedores = {};
let mapaLids = {};
let pendentesAprovacao = {}; 
let filaRetentativas = [];   
let sistemaIniciado = false;

// [NOVA TRAVA] - Bloqueia contratos duplicados na memória instantaneamente
let contratosEmProcessamento = new Set(); 

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
        args: [
            '--no-sandbox', 
            '--disable-setuid-sandbox', 
            '--disable-gpu',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-zygote',
            '--disable-extensions',
            '--disable-background-networking',
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding'
        ] 
    }
});

function notificarAdmin(mensagem) {
    if (adminLid && client) {
        try {
            client.sendMessage(adminLid, mensagem);
        } catch (e) {
            logger.error("Falha ao tentar notificar o administrador.");
        }
    }
}

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
                const chave = partes[0].trim();
                const valor = partes[1].trim(); // Pega o resto e limpa espaços

                if (chave === 'NUMERO_ADMIN') {
                    NUMERO_ADMIN_CONFIG = valor.replace(/\D/g, '');
                } else if (chave === 'TOKEN_API') {
                    TOKEN_API_CONFIG = valor.replace(/["']/g, ""); // Puxa o Token e limpa possíveis aspas
                }
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
        
        // Tranca a memória novamente ao reprocessar para evitar colisões externas
        contratosEmProcessamento.add(item.dadosVenda.contrato);
        
        const msgProcessandoRetry = `[PROCESSANDO RETENTATIVA]\nO contrato ${item.dadosVenda.contrato} do vendedor ${item.dadosVenda.vendedor} está sendo reanalisado...`;
        logger.info(msgProcessandoRetry);
        notificarAdmin(msgProcessandoRetry);
        
        try {
            // [NOVO] Injetando a variável TOKEN_API_CONFIG carregada do config.txt
            const resposta = await axios.post(URL_API_PYTHON, item.dadosVenda, { 
                headers: { 'Authorization': `Bearer ${TOKEN_API_CONFIG}` },
                timeout: 180000 
            });
            const status = resposta.data.status_pagamento;
            const planilha = resposta.data.planilha;
            const nomeCliente = resposta.data.nome_cliente || "Não informado";
            
            const msgSucessoRetry = `[REGISTRADO] Contrato validado com sucesso (após retentativa).\n\nContrato: ${item.dadosVenda.contrato}\nCliente: ${nomeCliente}\nVendedor: ${item.dadosVenda.vendedor}\nPlanilha: ${planilha}\nStatus: ${status}`;
            
            if (item.idSessaoRemetente) client.sendMessage(item.idSessaoRemetente, msgSucessoRetry);
            notificarAdmin(msgSucessoRetry);
            
        } catch (erroApi) {
            let msgTratada = "Falha de comunicação ou erro interno.";

            if (erroApi.response && erroApi.response.data) {
                const tipoErro = erroApi.response.data.erro;
                msgTratada = erroApi.response.data.mensagem || msgTratada;

                if (tipoErro === 'duplicidade') {
                    const msgDupRetry = `[REGISTRADO] ${msgTratada}\n\nVendedor: ${item.dadosVenda.vendedor}`;
                    if (item.idSessaoRemetente) client.sendMessage(item.idSessaoRemetente, msgDupRetry);
                    notificarAdmin(msgDupRetry);
                    logger.info(`Retentativa cancelada: Contrato ${item.dadosVenda.contrato} já se encontrava registrado.`);
                    contratosEmProcessamento.delete(item.dadosVenda.contrato);
                    return; 
                } else if (tipoErro === 'planilha_ausente') {
                    const msgPlanRetry = `[ERRO DE SISTEMA] Falha definitiva na retentativa.\n\nVendedor: ${item.dadosVenda.vendedor}\nDetalhe: ${msgTratada}\n\nO bot não tentará novamente até que as planilhas sejam criadas.`;
                    if (item.idSessaoRemetente) client.sendMessage(item.idSessaoRemetente, msgPlanRetry);
                    notificarAdmin(msgPlanRetry);
                    logger.error(`Retentativa abortada por falha de infraestrutura.`);
                    contratosEmProcessamento.delete(item.dadosVenda.contrato);
                    return; 
                }
            }

            item.tentativas += 1;
            if (item.tentativas < 2) {
                filaRetentativas.push(item);
                logger.warn(`Falha na retentativa ${item.dadosVenda.contrato}. Devolvido para a fila. Detalhe: ${msgTratada}`);
            } else {
                const msgFalhaRetry = `[ERRO] Falha definitiva ao processar contrato após 2 tentativas em background.\n\nContrato: ${item.dadosVenda.contrato}\nVendedor: ${item.dadosVenda.vendedor}\nÚltimo Erro: ${msgTratada}`;
                if (item.idSessaoRemetente) client.sendMessage(item.idSessaoRemetente, msgFalhaRetry);
                notificarAdmin(msgFalhaRetry);
                logger.error(`Abandono de retentativa para o contrato ${item.dadosVenda.contrato}.`);
            }
        } finally {
            // Destranca a memória após a retentativa finalizar (sucesso ou falha)
            contratosEmProcessamento.delete(item.dadosVenda.contrato);
        }
    }
}, 15000); 

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

            // [NOVO] TRAVA DE CORRIDA (RACE CONDITION)
            if (contratosEmProcessamento.has(dadosVenda.contrato)) {
                logger.warn(`Contrato ${dadosVenda.contrato} descartado sumariamente. Já está em processamento concorrente.`);
                return; 
            }
            // Tranca a porta para este contrato
            contratosEmProcessamento.add(dadosVenda.contrato);

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

                const msgProcessando = `[PROCESSANDO] O contrato ${dadosVenda.contrato} do vendedor ${nomeVendedor} está sendo analisado...`;
                const loadingMsg = await msg.reply(msgProcessando);
                logger.info(`Enviando Contrato: ${dadosVenda.contrato} | Vendedor: ${nomeVendedor}`);
                notificarAdmin(msgProcessando);

                try {
                    // [NOVO] Injetando a variável TOKEN_API_CONFIG carregada do config.txt
                    const respostaApp = await axios.post(URL_API_PYTHON, dadosVenda, { 
                        headers: { 'Authorization': `Bearer ${TOKEN_API_CONFIG}` },
                        timeout: 180000
                    });
                    const status = respostaApp.data.status_pagamento;
                    const planilha = respostaApp.data.planilha;
                    const nomeCliente = respostaApp.data.nome_cliente || "Não informado";
                    
                    const msgSucesso = `[REGISTRADO] Contrato validado com sucesso.\n\nContrato: ${dadosVenda.contrato}\nCliente: ${nomeCliente}\nVendedor: ${nomeVendedor}\nPlanilha: ${planilha}\nStatus: ${status}`;
                    
                    client.sendMessage(idSessaoBruto, msgSucesso);
                    notificarAdmin(msgSucesso);

                } catch (erroApi) {
                    if (erroApi.response && erroApi.response.data) {
                        const tipoErro = erroApi.response.data.erro;
                        const msgErro = erroApi.response.data.mensagem || 'Erro desconhecido retornado pela API.';

                        if (tipoErro === 'duplicidade') {
                            const msgDup = `[REGISTRADO] ${msgErro}\n\nVendedor: ${nomeVendedor}`;
                            client.sendMessage(idSessaoBruto, msgDup); 
                            notificarAdmin(msgDup);
                            logger.info(`Contrato ${dadosVenda.contrato} ignorado na fila. Motivo: Duplicidade (409).`);
                        
                        } else if (tipoErro === 'planilha_ausente') {
                            const msgPlan = `[ERRO DE SISTEMA] ${msgErro}\n\nVendedor: ${nomeVendedor}\n\nCrie as planilhas ou abas ausentes e reenvie a mensagem para tentar novamente.`;
                            client.sendMessage(idSessaoBruto, msgPlan); 
                            notificarAdmin(msgPlan);
                            logger.error(`Falha de infraestrutura no contrato ${dadosVenda.contrato}.`);
                        
                        } else if (tipoErro === 'contrato_nao_encontrado') {
                            logger.warn(`Contrato ${dadosVenda.contrato} não encontrado no portal. Transferindo para fila de resiliência.`);
                            // [NOVO] Guardando idSessaoRemetente para avisar o vendedor se a retentativa falhar depois
                            filaRetentativas.push({ dadosVenda, loadingMsg, tentativas: 0, idSessaoRemetente: idSessaoBruto }); 
                            const msgNaoEnc = `[AVISO] ${msgErro}\n\nVendedor: ${nomeVendedor}\n\nO bot tentará encontrar o contrato novamente em background (esperando o portal atualizar).`;
                            client.sendMessage(idSessaoBruto, msgNaoEnc); 
                            notificarAdmin(msgNaoEnc);
                        
                        } else {
                            logger.error(`Falha no contrato ${dadosVenda.contrato} (${tipoErro}). Transferindo para resiliência.`);
                            filaRetentativas.push({ dadosVenda, loadingMsg, tentativas: 0, idSessaoRemetente: idSessaoBruto }); 
                            const msgFalha = `[AVISO] Lentidão ou falha de gravação detectada.\nDetalhe: ${msgErro}\n\nVendedor: ${nomeVendedor}\n\nO bot transferiu o contrato para a fila de retentativas.`;
                            client.sendMessage(idSessaoBruto, msgFalha); 
                            notificarAdmin(msgFalha);
                        }
                    } else {
                        logger.error(`Falha de comunicação offline no contrato ${dadosVenda.contrato}. Transferindo para fila de resiliência.`);
                        filaRetentativas.push({ dadosVenda, loadingMsg, tentativas: 0, idSessaoRemetente: idSessaoBruto }); 
                        const msgOffline = `[AVISO] Falha de comunicação com o motor Python. O bot tentará registrar o contrato ${dadosVenda.contrato} novamente em background.\n\nVendedor: ${nomeVendedor}`;
                        client.sendMessage(idSessaoBruto, msgOffline); 
                        notificarAdmin(msgOffline);
                    }
                } finally {
                    // DESTRANCA A MEMÓRIA independente do desfecho
                    contratosEmProcessamento.delete(dadosVenda.contrato);
                }

            } else {
                contratosEmProcessamento.delete(dadosVenda.contrato);
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