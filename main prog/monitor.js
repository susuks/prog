const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fs = require('fs');
const csv = require('csv-parser');
const axios = require('axios'); // NOVA BIBLIOTECA PARA API

// --- CONFIGURAÇÕES ---
const NOME_GRUPO_ALVO = 'VENDAS'; 
const ARQUIVO_VENDEDORES = 'vendedores.csv';
const ARQUIVO_LIDS = 'mapeamento_lids.json'; // NOVO: Cérebro de IDs
const URL_API_PYTHON = 'http://127.0.0.1:5000/processar_venda'; // Endereço do futuro motor Python

const MAPA_ORIGENS = {
    "1": "DuoTalk", "2": "Tráfego", "3": "Remarketing", "4": "Contato Lucas",
    "5": "Outro", "6": "Indicação", "7": "RMKT + RMKT pessoal",
    "8": "TRFG + RMKT pessoal", "9": "DT + RMKT pessoal",
    "10": "Ctt.L + RMKT pessoal", "11": "Site"
};

let mapaVendedores = {}; // { '67999999999': 'Nome Vendedor' }
let mapaLids = {};       // { '123456@lid': '67999999999' }
let sistemaIniciado = false;

const client = new Client({
    authStrategy: new LocalAuth(),
    authTimeoutMs: 120000, 
    puppeteer: { 
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
    }
});

// --- FUNÇÕES DE MEMÓRIA E ONBOARDING ---

function carregarMemorias() {
    // 1. Carrega a lista Mestra de Vendedores (CSV)
    mapaVendedores = {};
    if (fs.existsSync(ARQUIVO_VENDEDORES)) {
        fs.createReadStream(ARQUIVO_VENDEDORES)
            .pipe(csv({ mapHeaders: ({ header }) => header.trim().replace(/^[\uFEFF\xEF\xBB\xBF]+/, '') }))
            .on('data', (row) => {
                const tel = (row.telefone || row.Telefone) ? (row.telefone || row.Telefone).replace(/\D/g, '') : null;
                const nome = row.nome_planilha || row.nome || row.Nome;
                if (tel && nome) mapaVendedores[tel] = nome;
            })
            .on('end', () => console.log(`[SISTEMA] ${Object.keys(mapaVendedores).length} vendedores mestres carregados.`));
    }

    // 2. Carrega as associações de IDs esquisitos (JSON)
    if (fs.existsSync(ARQUIVO_LIDS)) {
        try {
            mapaLids = JSON.parse(fs.readFileSync(ARQUIVO_LIDS, 'utf8'));
            console.log(`[SISTEMA] ${Object.keys(mapaLids).length} IDs (@lid/@c.us) já reconhecidos na memória.`);
        } catch (e) { mapaLids = {}; }
    }
}

function salvarMapaLids() {
    fs.writeFileSync(ARQUIVO_LIDS, JSON.stringify(mapaLids, null, 4));
}

// --- FUNÇÕES DE DADOS ---

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

// --- NÚCLEO DE PROCESSAMENTO ---

async function processarMensagem(msg) {
    try {
        if (msg.from === 'status@broadcast') return;
        const corpoMsg = msg.body;
        if (!corpoMsg) return;

        const idRemetente = msg.author || msg.from; // Pode ser @lid ou @c.us
        const chat = await msg.getChat();
        const isGrupoAlvo = chat.isGroup && chat.name && chat.name.toUpperCase() === NOME_GRUPO_ALVO.toUpperCase();
        const isPrivado = !chat.isGroup;

        // 1. FLUXO DE ONBOARDING (CADASTRO)
        if (corpoMsg.startsWith('#sou ')) {
            const numeroFornecido = corpoMsg.split('#sou ')[1].replace(/\D/g, '');
            
            // Verifica se o número existe na lista mestre de vendedores
            let nomeEncontrado = null;
            for (let tel in mapaVendedores) {
                if (tel.includes(numeroFornecido) || numeroFornecido.includes(tel)) {
                    nomeEncontrado = mapaVendedores[tel];
                    break;
                }
            }

            if (nomeEncontrado) {
                mapaLids[idRemetente] = numeroFornecido; // Associa o ID esquisito ao número real
                salvarMapaLids();
                msg.reply(`✅ *Identidade Confirmada!*\nBem-vindo(a), ${nomeEncontrado}.\nO sistema já o reconhece. Pode enviar as suas vendas no formato habitual.`);
                console.log(`[ONBOARDING] Novo ID associado ao vendedor: ${nomeEncontrado}`);
            } else {
                msg.reply(`❌ Número não encontrado na base de dados de vendedores autorizados. Verifique se digitou o DDD corretamente.`);
            }
            return;
        }

        // 2. FLUXO DE VENDA (API REST)
        if (isGrupoAlvo || isPrivado) {
            const dadosVenda = extrairDados(corpoMsg);
            
            if (dadosVenda) {
                // Tenta descobrir quem é com base na memória de IDs
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

                    // AVISO DE CARREGAMENTO
                    const loadingMsg = await msg.reply(`⏳ A processar o contrato *${dadosVenda.contrato}* no sistema da Tradição...`);

                    console.log(`[API SEND] Enviando Contrato: ${dadosVenda.contrato} | Vendedor: ${nomeVendedor}`);

                    // ENVIA PARA A NOSSA API PYTHON E ESPERA RESPOSTA
                    try {
                        const respostaPython = await axios.post(URL_API_PYTHON, dadosVenda, { timeout: 60000 }); // Aguarda até 60s
                        
                        // O Python respondeu com sucesso
                        const status = respostaPython.data.status_pagamento;
                        const planilha = respostaPython.data.planilha;
                        
                        loadingMsg.edit(`✅ *Contrato Registado!*\n\n📄 Contrato: ${dadosVenda.contrato}\n👤 Vendedor: ${nomeVendedor}\n📊 Planilha: ${planilha}\n📌 Status: ${status}`);

                    } catch (erroApi) {
                        // Ocorreu um erro (contrato não achado, falha no login, etc)
                        let motivo = "Falha de comunicação com o servidor central.";
                        if (erroApi.response && erroApi.response.data && erroApi.response.data.erro) {
                            motivo = erroApi.response.data.erro;
                        }
                        loadingMsg.edit(`❌ *Erro ao processar contrato!*\n\n📄 Contrato: ${dadosVenda.contrato}\n⚠️ Motivo: ${motivo}`);
                        console.log(`[API ERROR] Falha no contrato ${dadosVenda.contrato}: ${motivo}`);
                    }

                } else {
                    // SE O SISTEMA NÃO RECONHECER O UTILIZADOR, MANDA FAZER ONBOARDING
                    msg.reply(`⚠️ Olá! O sistema detetou um contrato válido, mas *não reconheceu o seu utilizador* devido a restrições de privacidade do WhatsApp.\n\nPor favor, responda a esta mensagem com o comando:\n*#sou SEU_NUMERO*\n_(Exemplo: #sou 67999999999)_\n\nSó precisa de fazer isto uma vez!`);
                }
            }
        }
    } catch (e) {
        console.error(`[ERRO GERAL]: ${e.message}`);
    }
}

// --- EVENTOS ---

client.on('qr', (qr) => qrcode.generate(qr, { small: true }));

client.on('ready', async () => {
    if (sistemaIniciado) return;
    sistemaIniciado = true;
    console.log('\n>>> MONITOR V9.0 (API REST & ONBOARDING) INICIADO <<<');
    
    carregarMemorias();
    // A função de recuperar antigas foi removida temporariamente para não inundar o servidor Python numa reinicialização
    console.log('>>> A aguardar novas mensagens em tempo real. <<<\n');
});

client.on('message_create', async (msg) => {
    if (!sistemaIniciado) return;
    await processarMensagem(msg);
});

client.initialize();