const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fs = require('fs');
const csv = require('csv-parser');

const NOME_GRUPO_ALVO = 'VENDAS'; 
const NUMERO_DESTINO_BRUTO = '556792656340'; 
const ARQUIVO_VENDEDORES = 'vendedores enc.csv'; // Corrigido para arquivo padrão
const ARQUIVO_STICKER_ID = 'sticker_id.txt';

let MENSAGEM_STICKER_PADRAO = null;
let numerosPermitidos = [];
let sistemaIniciado = false;

// REQUISITO 2: Fila de Processamento para evitar mistura
class PromiseQueue {
    constructor() {
        this.queue = Promise.resolve();
    }
    add(operation) {
        this.queue = this.queue.then(operation).catch(err => {
            console.error('[ERRO FILA]', err.message);
        });
    }
}
const filaEnvios = new PromiseQueue();

const client = new Client({
    authStrategy: new LocalAuth({ clientId: 'sessao-media' }),
    puppeteer: { 
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
    }
});

const delay = ms => new Promise(res => setTimeout(res, ms));

function carregarPermissoes() {
    numerosPermitidos = [];
    if (fs.existsSync(ARQUIVO_VENDEDORES)) {
        fs.createReadStream(ARQUIVO_VENDEDORES)
            .pipe(csv())
            .on('data', (row) => {
                try {
                    const limpo = (row.telefone || "").replace(/\D/g, '');
                    if (limpo) numerosPermitidos.push(limpo);
                } catch (err) {}
            })
            .on('end', () => console.log(`[SISTEMA] ${numerosPermitidos.length} vendedores autorizados.`));
    }
}

function salvarIdSticker(id) {
    fs.writeFileSync(ARQUIVO_STICKER_ID, id);
    console.log(`[MEMÓRIA] ID do sticker salvo em ${ARQUIVO_STICKER_ID}`);
}

async function tentarRecuperarSticker() {
    if (fs.existsSync(ARQUIVO_STICKER_ID)) {
        const idSalvo = fs.readFileSync(ARQUIVO_STICKER_ID, 'utf-8').trim();
        if (idSalvo) {
            try {
                const msgRecuperada = await client.getMessageById(idSalvo);
                if (msgRecuperada) {
                    MENSAGEM_STICKER_PADRAO = msgRecuperada;
                    console.log(`[SUCESSO] ✅ Sticker restaurado!`);
                    return true;
                }
            } catch (e) {}
        }
    }
    return false;
}

client.on('qr', (qr) => qrcode.generate(qr, { small: true }));

client.on('ready', async () => {
    if (sistemaIniciado) return;
    sistemaIniciado = true;
    console.log('\n>>> ROBÔ DE MÍDIA V20 (COM FILA ORDENADA) <<<');
    carregarPermissoes();
    await tentarRecuperarSticker();
    if (!MENSAGEM_STICKER_PADRAO) console.log(`[AVISO] Mande o sticker no grupo para calibrar.`);
});

client.on('message_create', async (msg) => {
    if (!sistemaIniciado) return;

    // Coloca todo o processamento dentro da fila
    filaEnvios.add(async () => {
        try {
            const chatOrigem = await msg.getChat();
            
            // Calibragem (Não precisa de fila complexa, é rápido)
            if (chatOrigem.isGroup && chatOrigem.name.toUpperCase() === NOME_GRUPO_ALVO.toUpperCase()) {
                if (msg.type === 'sticker') {
                    MENSAGEM_STICKER_PADRAO = msg;
                    salvarIdSticker(msg.id._serialized);
                    console.log(`\n[CALIBRAGEM] ✅ Novo sticker capturado!\n`);
                    return; 
                }
            }

            // Verificação
            let autorizado = false;
            if (chatOrigem.isGroup && chatOrigem.name.toUpperCase() === NOME_GRUPO_ALVO.toUpperCase()) {
                autorizado = true;
            } else if (!chatOrigem.isGroup) {
                const numeroRemetente = (msg.author || msg.from).replace(/\D/g, '');
                if (numerosPermitidos.some(perm => numeroRemetente.includes(perm))) autorizado = true;
            }

            // Envio (Esta é a parte crítica que precisa de ordem)
            if (autorizado && (msg.hasMedia || ['image', 'document'].includes(msg.type))) {
                if (msg === MENSAGEM_STICKER_PADRAO) return;

                console.log(`[PROCESSANDO] Mídia recebida... iniciando sequência.`);
                
                const idValidado = await client.getNumberId(NUMERO_DESTINO_BRUTO);
                if (!idValidado) return;
                const idFinal = idValidado._serialized;

                let textoMencao = "";
                let arrayMencoes = [];
                try {
                    const contato = await msg.getContact();
                    textoMencao = `DO @${contato.id.user}`;
                    arrayMencoes = [contato.id._serialized];
                } catch (e) {
                    const numero = (msg.author || msg.from).replace(/\D/g, '');
                    textoMencao = `DO @${numero}`;
                }

                // A) Envia Mídia
                const mediaOriginal = await msg.downloadMedia();
                await client.sendMessage(idFinal, mediaOriginal);
                await delay(1200); // Delay aumentado para garantir ordem no servidor do Whats

                // B) Envia Menção
                await client.sendMessage(idFinal, textoMencao, { mentions: arrayMencoes });
                await delay(1000);

                // C) Envia Sticker
                if (MENSAGEM_STICKER_PADRAO) {
                    await MENSAGEM_STICKER_PADRAO.forward(idFinal);
                }
                
                console.log(`[CONCLUÍDO] Sequência de mídia finalizada.`);
                await delay(500); // Pausa extra entre diferentes clientes
            }
        } catch (e) {
            console.error("Erro no processamento da mensagem:", e.message);
        }
    });
});

client.initialize();