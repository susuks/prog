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
let contratosAvisados = new Set(); // Guarda apenas o número do contrato (Avisa só no registro)
let sistemaIniciado = false;

const client = new Client({
    authStrategy: new LocalAuth(),
    authTimeoutMs: 120000, 
    puppeteer: { 
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
    }
});

// --- FUNÇÕES AUXILIARES ---

function limparIdUsuario(rawId) {
    if (!rawId) return "";
    let userPart = rawId.split('@')[0];
    let cleanNumber = userPart.split(':')[0];
    return cleanNumber.replace(/\D/g, '');
}

// Configuração segura para ler CSV ignorando caracteres invisíveis (BOM) do Excel
const csvConfig = { mapHeaders: ({ header }) => header.trim().replace(/^[\uFEFF\xEF\xBB\xBF]+/, '') };

function carregarVendedores() {
    return new Promise((resolve, reject) => {
        mapaVendedores = {};
        if (fs.existsSync(ARQUIVO_VENDEDORES)) {
            fs.createReadStream(ARQUIVO_VENDEDORES)
                .pipe(csv(csvConfig))
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

    const stream = fs.createReadStream(ARQUIVO_HISTORICO_SUCESSO).pipe(csv(csvConfig));
    
    stream.on('data', async (row) => {
        const contrato = row.contrato;
        let telefoneRaw = row.vendedor_tel || row['número'] || row.numero || row.telefone || row.vendedor; 
        const status = row.status_pagamento || row['1º paga'] || "Registrado";

        // Verifica SÓ o número do contrato na memória
        if (contrato && !contratosAvisados.has(contrato)) {
            if (telefoneRaw) {
                contratosAvisados.add(contrato); // Salva para nunca mais avisar
                
                try {
                    let numeroLimpo = limparIdUsuario(telefoneRaw + "@c.us"); 
                    if (numeroLimpo.length >= 10 && numeroLimpo.length <= 11) numeroLimpo = '55' + numeroLimpo; 

                    const idValidado = await client.getNumberId(numeroLimpo);
                    
                    if (idValidado) {
                        const chatId = idValidado._serialized;
                        
                        // Mensagem focada apenas no Registro
                        let msg = `✅ *Contrato Registrado!*\n\n📄 Contrato: ${contrato}\n📊 Planilha: Atualizada\n📌 Status Inicial: ${status}`;
                        
                        await client.sendMessage(chatId, msg);
                        console.log(`[FEEDBACK] ✅ Mensagem de registro enviada para ${numeroLimpo}`);
                    } else {
                        console.log(`[ERRO FEEDBACK] WhatsApp rejeitou o número: ${numeroLimpo}`);
                    }
                } catch (e) {
                    console.error(`[ERRO FEEDBACK] ${e.message}`);
                }
            } else {
                // LOG DELATOR: Se chegar aqui, o arquivo CSV está quebrado/antigo!
                console.log(`[ALERTA] Vi o contrato ${contrato} concluído, mas a coluna de telefone sumiu! Apague o historico_concluidos.csv e deixe o Python recriar.`);
                contratosAvisados.add(contrato); // Adiciona na memória pra não spammar o erro
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
                    } else {
                        console.log(`[DUPLICADO] Contrato ${dados.contrato} recusado (já processado).`);
                    }
                } else {
                    console.log(`[NEGADO] Mensagem válida, mas número ${idAutor} não está na lista de vendedores.`);
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
        } catch (erro) {}
    }
    console.log('>>> RECUPERAÇÃO CONCLUÍDA. MODO TEMPO REAL ATIVO. <<<\n');
}

// --- EVENTOS ---

client.on('qr', (qr) => qrcode.generate(qr, { small: true }));

client.on('ready', async () => {
    if (sistemaIniciado) return;
    sistemaIniciado = true;
    console.log('\n>>> MONITOR V8.0 (ANTI-FALHA SILENCIOSA) INICIADO <<<');
    
    await carregarVendedores();

    if (fs.existsSync(ARQUIVO_HISTORICO_SUCESSO)) {
         fs.createReadStream(ARQUIVO_HISTORICO_SUCESSO)
            .pipe(csv(csvConfig))
            .on('data', (row) => { 
                // Popula a memória SÓ com o contrato
                if(row.contrato) {
                    contratosAvisados.add(row.contrato);
                }
            })
            .on('end', async () => {
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