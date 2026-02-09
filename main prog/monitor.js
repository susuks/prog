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
let contratosAvisados = new Set(); // Memória RAM para não repetir aviso
let sistemaIniciado = false;

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: { 
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
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
            .on('end', () => console.log(`[SISTEMA] ${Object.keys(mapaVendedores).length} vendedores carregados.`));
    }
}

function contratoJaProcessado(contrato) {
    if (fs.existsSync(ARQUIVO_FILA)) {
        const fila = fs.readFileSync(ARQUIVO_FILA, 'utf-8');
        if (fila.includes(contrato)) return true;
    }
    // Verifica se já está no histórico final (evita reprocessar antigos)
    if (fs.existsSync(ARQUIVO_HISTORICO_SUCESSO)) {
        const historico = fs.readFileSync(ARQUIVO_HISTORICO_SUCESSO, 'utf-8');
        if (historico.includes(contrato)) return true;
    }
    return false;
}

function salvarNaFila(dados) {
    // REQUISITO 3: Adicionei coluna 'telefone'
    if (!fs.existsSync(ARQUIVO_FILA)) {
        fs.writeFileSync(ARQUIVO_FILA, "contrato,origem,vendedor,lance livre,telefone\n");
    }
    const linha = `${dados.contrato},${dados.origem},${dados.vendedor},${dados.lance},${dados.telefone}\n`;
    fs.appendFileSync(ARQUIVO_FILA, linha);
    console.log(`[FILA] 📥 Contrato ${dados.contrato} enviado para o Python.`);
}

// REQUISITO 3: Feedback Responsivo
function verificarConcluidosEConfirmar() {
    if (!fs.existsSync(ARQUIVO_HISTORICO_SUCESSO)) return;

    // Lê o arquivo de concluídos
    const stream = fs.createReadStream(ARQUIVO_HISTORICO_SUCESSO).pipe(csv());
    
    stream.on('data', async (row) => {
        // Formato esperado do Python: contrato, planilha, data, vendedor_nome, vendedor_tel, status_pagamento
        const contrato = row.contrato;
        const telefone = row.vendedor_tel; 
        const status = row.status_pagamento;

        if (contrato && telefone && !contratosAvisados.has(contrato)) {
            contratosAvisados.add(contrato); // Marca como avisado na memória

            // Evita avisar contratos muito velhos (carregados na inicialização)
            // Se quiser avisar só os de "agora", pode usar timestamp, mas por enquanto vamos avisar todos que aparecerem novos no arquivo
            
            try {
                const chatId = `${telefone}@c.us`;
                let msg = `✅ *Cadastro Confirmado!*\n\n📄 Contrato: ${contrato}\n📊 Planilha: Atualizada\n💰 Status: ${status}`;
                
                await client.sendMessage(chatId, msg);
                console.log(`[FEEDBACK] Mensagem de confirmação enviada para ${telefone} sobre contrato ${contrato}`);
            } catch (e) {
                console.error(`[ERRO FEEDBACK] Não consegui enviar msg para ${telefone}: ${e.message}`);
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

// --- ROTINA PRINCIPAL ---

client.on('qr', (qr) => qrcode.generate(qr, { small: true }));

client.on('ready', () => {
    if (sistemaIniciado) return;
    sistemaIniciado = true;
    console.log('\n>>> MONITOR V7.1 (RESPONSIVO) INICIADO <<<');
    carregarVendedores();
    
    // Carrega o histórico atual na memória para não mandar msg de coisas velhas
    if (fs.existsSync(ARQUIVO_HISTORICO_SUCESSO)) {
         fs.createReadStream(ARQUIVO_HISTORICO_SUCESSO)
            .pipe(csv())
            .on('data', (row) => { if(row.contrato) contratosAvisados.add(row.contrato); })
            .on('end', () => {
                console.log(`[SISTEMA] Histórico sincronizado. Iniciando verificação de novos...`);
                // Inicia o loop de verificação de feedback a cada 10 segundos
                setInterval(verificarConcluidosEConfirmar, 10000); 
            });
    } else {
        setInterval(verificarConcluidosEConfirmar, 10000);
    }
});

client.on('message_create', async (msg) => {
    if (!sistemaIniciado) return;
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
                    dados.telefone = idAutor; // REQUISITO 3: Passando telefone

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
});

client.initialize();