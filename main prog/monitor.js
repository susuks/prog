const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fs = require('fs');
const csv = require('csv-parser');

// --- CONFIGURAÇÕES ---
const NOME_GRUPO_ALVO = 'VENDAS'; 
const ARQUIVO_VENDEDORES = 'vendedores.csv';
const ARQUIVO_FILA = 'fila_vendas.csv'; 
// O Monitor agora LÊ este arquivo, mas quem escreve é o Python
const ARQUIVO_HISTORICO_SUCESSO = 'historico_concluidos.csv'; 

let mapaVendedores = {};
let sistemaIniciado = false; // Trava para evitar duplo start

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: { 
        headless: false,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-zygote',
            '--disable-gpu'
        ]
    }
});

// --- FUNÇÕES AUXILIARES ---

function carregarVendedores() {
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
                // Só inicia a recuperação depois de carregar os nomes
                recuperarMensagensAntigas();
            });
    } else {
        console.log('[AVISO] Arquivo vendedores.csv não encontrado.');
    }
}

function contratoJaProcessado(contrato) {
    // 1. Verifica se já foi concluído pelo Python (Histórico Definitivo)
    if (fs.existsSync(ARQUIVO_HISTORICO_SUCESSO)) {
        const historico = fs.readFileSync(ARQUIVO_HISTORICO_SUCESSO, 'utf-8');
        if (historico.includes(contrato)) return true;
    }
    
    // 2. Verifica se já está na fila esperando (para não duplicar na fila)
    if (fs.existsSync(ARQUIVO_FILA)) {
        const fila = fs.readFileSync(ARQUIVO_FILA, 'utf-8');
        if (fila.includes(contrato)) return true;
    }

    return false;
}

function salvarNaFila(dados) {
    // Garante cabeçalho correto para o Python
    if (!fs.existsSync(ARQUIVO_FILA)) {
        fs.writeFileSync(ARQUIVO_FILA, "contrato,origem,vendedor,lance livre\n");
    }
    const linha = `${dados.contrato},${dados.origem},${dados.vendedor},${dados.lance}\n`;
    fs.appendFileSync(ARQUIVO_FILA, linha);
    console.log(`[FILA] 📥 Contrato ${dados.contrato} enviado para processamento.`);
}

function extrairDados(texto) {
    const regex = /(\d{5,})\s*,\s*([^,]+)(?:\s*,\s*([\d\.]+))?/;
    const match = texto.match(regex);
    if (match) return { contrato: match[1].trim(), origem: match[2].trim(), lance: match[3] ? match[3].trim() : "0" };
    return null;
}

// --- LÓGICA DE PROCESSAMENTO CENTRALIZADA ---
async function processarMensagem(msg) {
    try {
        if (msg.from === 'status@broadcast' || msg.from.includes('@lid')) return;
        
        const corpoMsg = msg.body;
        if (!corpoMsg || corpoMsg.length < 5) return;

        let isGrupoAlvo = false;
        let isPrivado = !msg.from.includes('@g.us');

        // Identificação rápida de grupo
        if (msg.from.includes('@g.us')) {
            const chat = await msg.getChat();
            if (chat.name && chat.name.toUpperCase() === NOME_GRUPO_ALVO.toUpperCase()) {
                isGrupoAlvo = true;
            }
        }

        if (isGrupoAlvo || isPrivado) {
            const dados = extrairDados(corpoMsg);
            if (dados) {
                let idAutor = (msg.author || msg.from).replace(/\D/g, '');
                let nomeVendedor = "Desconhecido";

                for (let tel in mapaVendedores) {
                    if (idAutor.includes(tel)) {
                        nomeVendedor = mapaVendedores[tel];
                        break;
                    }
                }

                if (nomeVendedor !== "Desconhecido") {
                    dados.vendedor = nomeVendedor;
                    
                    if (!contratoJaProcessado(dados.contrato)) {
                        console.log(`[NOVO] Vendedor: ${nomeVendedor} | Contrato: ${dados.contrato}`);
                        salvarNaFila(dados);
                    } else {
                        // Silencioso para não poluir o log na recuperação
                        // console.log(`[DUPLICADO] ${dados.contrato} ignorado.`);
                    }
                }
            }
        }
    } catch (e) {
        console.error(`[ERRO MSG]: ${e.message}`);
    }
}

// --- RECUPERAÇÃO DE MENSAGENS ANTIGAS ---
async function recuperarMensagensAntigas() {
    console.log('\n>>> INICIANDO ROTINA DE RECUPERAÇÃO <<<');
    console.log('Lendo as últimas 10 mensagens de cada vendedor cadastrado...');
    
    const atraso = ms => new Promise(resolve => setTimeout(resolve, ms));

    for (const [telefone, nome] of Object.entries(mapaVendedores)) {
        try {
            // Monta o ID do contato (ex: 55679...@c.us)
            const chatId = `${telefone}@c.us`;
            const chat = await client.getChatById(chatId);
            
            // Busca as últimas 10 mensagens
            const mensagens = await chat.fetchMessages({ limit: 10 });
            
            console.log(`   > Verificando ${nome} (${mensagens.length} msgs)...`);
            
            for (const msg of mensagens) {
                await processarMensagem(msg);
            }
            
            // Pequena pausa para não bloquear o WhatsApp
            await atraso(500); 

        } catch (erro) {
            // Ignora erro se o chat não existir ou estiver vazio
            // console.log(`   [INFO] Sem histórico acessível para ${nome}`);
        }
    }
    console.log('>>> RECUPERAÇÃO CONCLUÍDA. MODO TEMPO REAL ATIVO. <<<\n');
}

// --- EVENTOS ---

client.on('qr', (qr) => {
    console.log('[SISTEMA] QR Code gerado!');
    qrcode.generate(qr, { small: true });
});

client.on('ready', () => {
    if (sistemaIniciado) return;
    sistemaIniciado = true;

    console.log('\n>>> MONITOR V7.0 (COM RECUPERAÇÃO) INICIADO <<<');
    carregarVendedores();
});

client.on('message_create', async (msg) => {
    if (!sistemaIniciado) return;
    await processarMensagem(msg);
});

client.initialize();