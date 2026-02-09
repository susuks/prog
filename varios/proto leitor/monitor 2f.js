const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fs = require('fs');
const { spawn } = require('child_process');
const createCsvWriter = require('csv-writer').createObjectCsvWriter;

// --- CONFIGURAÇÕES ---
const TEMPO_CICLO_MINUTOS = 3;
const ARQUIVO_HISTORICO = 'historico_contratos.txt';
const NOME_SCRIPT_PYTHON = 'coleta_dados.py';

// Inicializa o Cliente com persistência (não pede QR Code toda vez)
const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
        headless: true, // Roda sem abrir janela (mais leve)
        args: ['--no-sandbox']
    }
});

let mensagensCiclo = [];
let cicloInterval;

// --- FUNÇÕES AUXILIARES ---

// Carrega histórico para evitar duplicidade
function contratoJaProcessado(contrato) {
    if (!fs.existsSync(ARQUIVO_HISTORICO)) return false;
    const dados = fs.readFileSync(ARQUIVO_HISTORICO, 'utf-8');
    return dados.includes(contrato);
}

function salvarNoHistorico(contrato) {
    fs.appendFileSync(ARQUIVO_HISTORICO, contrato + '\n');
}

// Extrai dados da mensagem (Regex)
function extrairDados(texto) {
    // Padrão: Numeros, Texto, Texto, Numeros (com ou sem espaços)
    const regex = /(\d{5,})\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([\d\.]+)/;
    const match = texto.match(regex);

    if (match) {
        return {
            contrato: match[1].trim(),
            origem: match[2].trim(),
            vendedor: match[3].trim(),
            lance: match[4].trim()
        };
    }
    return null;
}

// --- EVENTOS DO WHATSAPP ---

client.on('qr', (qr) => {
    console.log('\n================================================');
    console.log('ESCANEIE ESTE QR CODE NO SEU WHATSAPP:');
    qrcode.generate(qr, { small: true });
    console.log('================================================\n');
});

client.on('ready', () => {
    console.log('\n>>> WHATSAPP CONECTADO COM SUCESSO! <<<');
    console.log(`Monitorando mensagens... Ciclo de ${TEMPO_CICLO_MINUTOS} minutos.\n`);

    // Inicia o timer do ciclo
    cicloInterval = setInterval(processarCiclo, TEMPO_CICLO_MINUTOS * 60 * 1000);
});

client.on('message_create', async (msg) => {
    // message_create lê mensagens enviadas por você e recebidas
    const dados = extrairDados(msg.body);

    if (dados) {
        if (!contratoJaProcessado(dados.contrato)) {
            // Verifica se já pegamos neste ciclo atual
            const jaNaLista = mensagensCiclo.some(m => m.contrato === dados.contrato);

            if (!jaNaLista) {
                console.log(`[NOVO] Contrato Capturado: ${dados.contrato} - ${dados.vendedor}`);
                mensagensCiclo.push(dados);
                salvarNoHistorico(dados.contrato);
            }
        } else {
            console.log(`[IGNORADO] Contrato ${dados.contrato} já foi processado antes.`);
        }
    }
});

// --- PROCESSAMENTO DO CICLO ---

async function processarCiclo() {
    if (mensagensCiclo.length === 0) {
        console.log(`\n[Ciclo ${new Date().toLocaleTimeString()}] Nenhuma mensagem nova.`);
        return;
    }

    console.log(`\n>>> CICLO FINALIZADO. PROCESSANDO ${mensagensCiclo.length} CONTRATOS...`);

    // 1. Cria o nome do arquivo com Data/Hora
    const agora = new Date();
    const timestamp = agora.toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const nomeArquivo = `contratos_${timestamp}.csv`;

    // 2. Escreve o CSV
    const csvWriter = createCsvWriter({
        path: nomeArquivo,
        header: [
            { id: 'contrato', title: 'contrato' },
            { id: 'origem', title: 'origem' },
            { id: 'vendedor', title: 'vendedor' },
            { id: 'lance', title: 'lance livre' }
        ]
    });

    await csvWriter.writeRecords(mensagensCiclo);
    console.log(`Arquivo gerado: ${nomeArquivo}`);

    // 3. Chama o Python
    console.log('Iniciando Robô de Coleta (Python)...');

    const pythonProcess = spawn('python', [NOME_SCRIPT_PYTHON, nomeArquivo]);

    // Mostra o que o Python está imprimindo (Logs)
    pythonProcess.stdout.on('data', (data) => {
        console.log(`[Python]: ${data.toString().trim()}`);
    });

    pythonProcess.stderr.on('data', (data) => {
        console.error(`[Python Erro]: ${data.toString().trim()}`);
    });

    pythonProcess.on('close', (code) => {
        console.log(`Robô Python finalizado (Código ${code}).`);
        console.log('Aguardando próximas mensagens...\n');
    });

    // Limpa a lista para o próximo ciclo
    mensagensCiclo = [];
}

// Inicia o cliente
client.initialize();