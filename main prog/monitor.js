const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fs = require('fs');
const csv = require('csv-parser');

// --- CONFIGURAÇÕES ---
const NOME_GRUPO_ALVO = 'VENDAS'; 
const ARQUIVO_VENDEDORES = 'vendedores.csv';
const ARQUIVO_FILA = 'fila_vendas.csv'; 
const ARQUIVO_HISTORICO_SUCESSO = 'historico_concluidos.csv'; 

let mapaVendedores = {};
let contratosAvisados = new Set(); 
let sistemaIniciado = false;

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: { 
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
    }
});

// --- FUNÇÕES AUXILIARES ---

// NOVA FUNÇÃO: Remove sufixos como :83 ou :2 que estragam o número
function limparIdUsuario(rawId) {
    if (!rawId) return "";
    // 1. Pega apenas o que vem antes do @ (ex: 556799998888:83@c.us -> 556799998888:83)
    let userPart = rawId.split('@')[0];
    // 2. Pega apenas o que vem antes de : (ex: 556799998888:83 -> 556799998888)
    let cleanNumber = userPart.split(':')[0];
    // 3. Garante que só tem números
    return cleanNumber.replace(/\D/g, '');
}

function carregarVendedores() {
    return new Promise((resolve, reject) => {
        mapaVendedores = {};
        if (fs.existsSync(ARQUIVO_VENDEDORES)) {
            fs.createReadStream(ARQUIVO_VENDEDORES)
                .pipe(csv())
                .on('data', (row) => {
                    try {
                        const tel = (row.telefone || row.Telefone) ? (row.telefone || row.Telefone).replace(/\D/g, '') : null;
                        const nome = row.nome_planilha || row.nome || row.Nome;
                        if (tel && nome) mapaVendedores[tel] = nome;
                    } catch (e) {}
                })
                .on('end', () => {
                    console.log(`[SISTEMA] ${Object.keys(mapaVendedores).length} vendedores carregados.`);
                    resolve(true); 
                })
                .on('error', (err) => {
                    console.log('[ERRO] Falha ao ler CSV vendedores.');
                    resolve(false); 
                });
        } else {
            console.log('[AVISO] Arquivo vendedores.csv não encontrado.');
            resolve(false);
        }
    });
}

function contratoJaProcessado(contrato) {
    if (fs.existsSync(ARQUIVO_FILA)) {
        const fila = fs.readFileSync(ARQUIVO_FILA, 'utf-8');
        if (fila.includes(contrato)) return true;
    }
    if (fs.existsSync(ARQUIVO_HISTORICO_SUCESSO)) {
        const historico = fs.readFileSync(ARQUIVO_HISTORICO_SUCESSO, 'utf-8');
        if (historico.includes(contrato)) return true;
    }
    return false;
}

function salvarNaFila(dados) {
    if (!fs.existsSync(ARQUIVO_FILA)) {
        fs.writeFileSync(ARQUIVO_FILA, "contrato,origem,vendedor,lance livre,telefone\n");
    }
    const linha = `${dados.contrato},${dados.origem},${dados.vendedor},${dados.lance},${dados.telefone}\n`;
    fs.appendFileSync(ARQUIVO_FILA, linha);
    console.log(`[FILA] 📥 Contrato ${dados.contrato} enviado para o Python.`);
}

function verificarConcluidosEConfirmar() {
    if (!fs.existsSync(ARQUIVO_HISTORICO_SUCESSO)) return;

    const stream = fs.createReadStream(ARQUIVO_HISTORICO_SUCESSO).pipe(csv());
    
    stream.on('data', async (row) => {
        const contrato = row.contrato;
        let telefoneRaw = row.vendedor_tel || row['número'] || row.numero; 
        const status = row.status_pagamento || row['1º paga'];

        if (contrato && !contratosAvisados.has(contrato)) {
            if (telefoneRaw) {
                contratosAvisados.add(contrato);
                try {
                    // Limpeza extra também no feedback, por segurança
                    let numeroLimpo = limparIdUsuario(telefoneRaw + "@c.us"); 

                    // Adiciona 55 se parecer número BR sem DDI (10 ou 11 dígitos)
                    if (numeroLimpo.length >= 10 && numeroLimpo.length <= 11) numeroLimpo = '55' + numeroLimpo; 

                    const idValidado = await client.getNumberId(numeroLimpo);
                    
                    if (idValidado) {
                        const chatId = idValidado._serialized;
                        let msg = `✅ *Cadastro Confirmado!*\n\n📄 Contrato: ${contrato}\n📊 Planilha: Atualizada\n💰 Status: ${status}`;
                        await client.sendMessage(chatId, msg);
                        console.log(`[FEEDBACK] ✅ Mensagem enviada para ${numeroLimpo}`);
                    } else {
                        console.log(`[ERRO FEEDBACK] WhatsApp não reconhece o número: ${numeroLimpo} (Original: ${telefoneRaw})`);
                    }
                } catch (e) {
                    console.error(`[ERRO FEEDBACK] Falha técnica: ${e.message}`);
                }
            } 
        }
    });
}

function extrairDados(texto) {
    const regex = /(\d{5,})\s*,\s*([^,]+)(?:\s*,\s*([\d\.]+))?/;
    const match = texto.match(regex);
    if (match) return { contrato: match[1].trim(), origem: match[2].trim(), lance: match[3] ? match[3].trim() : "0" };
    return null;
}

async function processarMensagem(msg) {
    try {
        if (msg.from === 'status@broadcast' || msg.from.includes('@lid')) return;
        
        const corpoMsg = msg.body;
        if (!corpoMsg || corpoMsg.length < 5) return;

        let isGrupoAlvo = false;
        let isPrivado = !msg.from.includes('@g.us');

        if (msg.from.includes('@g.us')) {
            const chat = await msg.getChat();
            if (chat.name && chat.name.toUpperCase() === NOME_GRUPO_ALVO.toUpperCase()) isGrupoAlvo = true;
        }

        if (isGrupoAlvo || isPrivado) {
            const dados = extrairDados(corpoMsg);
            if (dados) {
                // CORREÇÃO: Usando a nova função de limpeza cirúrgica
                let idAutor = limparIdUsuario(msg.author || msg.from);
                
                let nomeVendedor = "Desconhecido";

                for (let tel in mapaVendedores) {
                    if (idAutor.includes(tel)) {
                        nomeVendedor = mapaVendedores[tel];
                        break;
                    }
                }

                if (nomeVendedor !== "Desconhecido") {
                    dados.vendedor = nomeVendedor;
                    dados.telefone = idAutor; 

                    if (!contratoJaProcessado(dados.contrato)) {
                        console.log(`[NOVO] Vendedor: ${nomeVendedor} | Contrato: ${dados.contrato}`);
                        salvarNaFila(dados);
                    }
                }
            }
        }
    } catch (e) {
        console.error(`[ERRO MSG]: ${e.message}`);
    }
}

async function recuperarMensagensAntigas() {
    console.log('\n>>> INICIANDO ROTINA DE RECUPERAÇÃO <<<');
    console.log('Lendo as últimas 10 mensagens de cada vendedor cadastrado...');
    
    const atraso = ms => new Promise(resolve => setTimeout(resolve, ms));

    for (const [telefone, nome] of Object.entries(mapaVendedores)) {
        try {
            const chatId = `${telefone}@c.us`;
            const chat = await client.getChatById(chatId);
            const mensagens = await chat.fetchMessages({ limit: 10 });
            
            console.log(`   > Verificando ${nome} (${mensagens.length} msgs)...`);
            
            for (const msg of mensagens) {
                await processarMensagem(msg);
            }
            await atraso(500); 
        } catch (erro) {
            // Ignora chat inexistente
        }
    }
    console.log('>>> RECUPERAÇÃO CONCLUÍDA. MODO TEMPO REAL ATIVO. <<<\n');
}

// --- EVENTOS ---

client.on('qr', (qr) => qrcode.generate(qr, { small: true }));

client.on('ready', async () => {
    if (sistemaIniciado) return;
    sistemaIniciado = true;
    console.log('\n>>> MONITOR V7.4 (CORREÇÃO DE ID) INICIADO <<<');
    
    await carregarVendedores();

    if (fs.existsSync(ARQUIVO_HISTORICO_SUCESSO)) {
         fs.createReadStream(ARQUIVO_HISTORICO_SUCESSO)
            .pipe(csv())
            .on('data', (row) => { if(row.contrato) contratosAvisados.add(row.contrato); })
            .on('end', async () => {
                console.log(`[SISTEMA] Histórico sincronizado.`);
                await recuperarMensagensAntigas();
                setInterval(verificarConcluidosEConfirmar, 10000); 
            });
    } else {
        await recuperarMensagensAntigas();
        setInterval(verificarConcluidosEConfirmar, 10000);
    }
});

client.on('message_create', async (msg) => {
    if (!sistemaIniciado) return;
    await processarMensagem(msg);
});

client.initialize();