const TelegramBot = require('node-telegram-bot-api');
const { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason } = require('@whiskeysockets/baileys');
const fs = require('fs-extra');
const https = require('https');
const path = require('path');
const xlsx = require('xlsx');
const qrcode = require('qrcode-terminal');

// Environment Variables
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ADMIN_CHAT_ID = process.env.ADMIN_CHAT_ID; // Admin ka Telegram Chat ID

if (!TELEGRAM_BOT_TOKEN || !ADMIN_CHAT_ID) {
    console.error("❌ Error: TELEGRAM_BOT_TOKEN aur ADMIN_CHAT_ID env variable me hona zaroori hai!");
    process.exit(1);
}

const bot = new TelegramBot(TELEGRAM_BOT_TOKEN, { polling: true });
let waSock = null;

// User Database (Local JSON file)
const USERS_FILE = path.join(__dirname, 'users.json');

function loadUsers() {
    if (!fs.existsSync(USERS_FILE)) {
        fs.writeFileSync(USERS_FILE, JSON.stringify({}));
    }
    return fs.readJsonSync(USERS_FILE);
}

function saveUsers(users) {
    fs.writeJsonSync(USERS_FILE, users, { spaces: 2 });
}

// Subscription Check Helper
function isUserActive(userId) {
    if (userId.toString() === ADMIN_CHAT_ID.toString()) return true; // Admin ke liye lifetime access
    const users = loadUsers();
    const user = users[userId];
    if (!user || !user.expiryDate) return false;
    return new Date(user.expiryDate) > new Date();
}

// Add Subscription Helper
function addUserSubscription(userId, days) {
    const users = loadUsers();
    let currentExpiry = new Date();
    if (users[userId] && users[userId].expiryDate && new Date(users[userId].expiryDate) > new Date()) {
        currentExpiry = new Date(users[userId].expiryDate);
    }
    currentExpiry.setDate(currentExpiry.getDate() + parseInt(days));
    users[userId] = {
        expiryDate: currentExpiry.toISOString(),
        joinedAt: users[userId]?.joinedAt || new Date().toISOString()
    };
    saveUsers(users);
    return currentExpiry;
}

// WhatsApp Connection Engine
async function startWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState('auth_info_baileys');
    const { version } = await fetchLatestBaileysVersion();

    waSock = makeWASocket({
        version,
        auth: state,
        printQRInTerminal: true
    });

    waSock.ev.on('creds.update', saveCreds);

    waSock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;
        if (qr) {
            console.log('📌 Scan this QR code in Render Logs:');
            qrcode.generate(qr, { small: true });
        }
        if (connection === 'open') {
            console.log('✅ WhatsApp Engine Connected!');
            bot.sendMessage(ADMIN_CHAT_ID, "✅ WhatsApp Engine Connected Successfully!");
        } else if (connection === 'close') {
            const shouldReconnect = (lastDisconnect?.error)?.output?.statusCode !== DisconnectReason.loggedOut;
            if (shouldReconnect) startWhatsApp();
        }
    });
}


// Health-check HTTP server (replaces the separate sandip.py health server)
const { createServer } = require('http');
const PORT = Number(process.env.PORT || 10000);

createServer((req, res) => {
    if (req.url === '/' || req.url === '/health') {
        res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('OK');
        return;
    }
    res.writeHead(404);
    res.end('Not Found');
}).listen(PORT, '0.0.0.0', () => {
    console.log(`🌐 Health Check Server Running on Port ${PORT}`);
});

startWhatsApp();

// /start Command Handler
bot.onText(/\/start/, (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;
    const users = loadUsers();

    if (!users[userId]) {
        users[userId] = { joinedAt: new Date().toISOString(), expiryDate: null };
        saveUsers(users);
    }

    const active = isUserActive(userId);
    let statusMsg = active ? "🟢 Membership Active" : "🔴 Membership Expired";
    let expiryStr = active && users[userId]?.expiryDate ? new Date(users[userId].expiryDate).toLocaleDateString('hi-IN') : "None";

    const welcomeMsg = `👋 **Welcome to WS Check Bot!**\n\n` +
        `👤 **User:** ${msg.from.first_name}\n` +
        `📊 **Status:** ${statusMsg}\n` +
        `📅 **Expiry:** ${expiryStr}\n\n` +
        `📁 **Features:** Send .txt, .csv, .xlsx file or numbers to check WhatsApp status.`;

    bot.sendMessage(chatId, welcomeMsg, { parse_mode: 'Markdown' });
});

// 👑 ADMIN COMMAND 1: Add Monthly Subscription (/add <USER_ID> <DAYS>)
bot.onText(/\/add (\d+) (\d+)/, (msg, match) => {
    const senderId = msg.chat.id;
    if (senderId.toString() !== ADMIN_CHAT_ID.toString()) return;

    const targetUserId = match[1];
    const days = parseInt(match[2]);

    const newExpiry = addUserSubscription(targetUserId, days);
    bot.sendMessage(senderId, `✅ **Added ${days} Days Subscription** for User \`${targetUserId}\`.\n📅 **New Expiry:** ${newExpiry.toLocaleString()}`, { parse_mode: 'Markdown' });

    // User ko notify karein
    bot.sendMessage(targetUserId, `🎉 **Subscription Activated!**\nAapka monthly plan **${newExpiry.toLocaleDateString('hi-IN')}** tak active kar diya gaya hai.`);
});

// 👑 ADMIN COMMAND 2: Broadcast Message to All Members (/broadcast <MESSAGE>)
bot.onText(/\/broadcast (.+)/, async (msg, match) => {
    const senderId = msg.chat.id;
    if (senderId.toString() !== ADMIN_CHAT_ID.toString()) return;

    const broadcastText = match[1];
    const users = loadUsers();
    const userIds = Object.keys(users);

    bot.sendMessage(senderId, `📢 Broadcasting message to **${userIds.length} users**...`, { parse_mode: 'Markdown' });

    let successCount = 0;
    let failCount = 0;

    for (let uid of userIds) {
        try {
            await bot.sendMessage(uid, `📢 **Admin Announcement:**\n\n${broadcastText}`, { parse_mode: 'Markdown' });
            successCount++;
        } catch (e) {
            failCount++;
        }
        await new Promise(r => setTimeout(r, 100)); // anti-spam delay
    }

    bot.sendMessage(senderId, `✅ **Broadcast Completed!**\n\n🎯 Success: ${successCount}\n❌ Failed: ${failCount}`);
});

// File aur Number Checking System
bot.on('message', async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from.id;

    if (msg.text && msg.text.startsWith('/')) return; // Ignore commands

    // Check Membership Status
    if (!isUserActive(userId)) {
        return bot.sendMessage(chatId, "⛔ **Aapka Subscription Expire ho gaya hai!**\n\nKripya Admin se sampark karke monthly plan renew karayein.", { parse_mode: 'Markdown' });
    }

    let numbersToCheck = [];

    // File Processing (.txt, .csv, .xlsx)
    if (msg.document) {
        const fileId = msg.document.file_id;
        const fileName = msg.document.file_name || '';
        const fileLink = await bot.getFileLink(fileId);

        bot.sendMessage(chatId, "⏳ **File process ho rahi hai...**", { parse_mode: 'Markdown' });

        const localPath = path.join(__dirname, fileName);
        const fileStream = fs.createWriteStream(localPath);

        https.get(fileLink, (response) => {
            response.pipe(fileStream);
            fileStream.on('finish', async () => {
                fileStream.close();

                if (fileName.endsWith('.xlsx') || fileName.endsWith('.xls') || fileName.endsWith('.csv')) {
                    const workbook = xlsx.readFile(localPath);
                    const sheetName = workbook.SheetNames[0];
                    const rows = xlsx.utils.sheet_to_json(workbook.Sheets[sheetName], { header: 1 });
                    numbersToCheck = rows.flat().map(v => String(v).trim()).filter(Boolean);
                } else {
                    const content = fs.readFileSync(localPath, 'utf8');
                    numbersToCheck = content.split(/\r?\n/).map(n => n.trim()).filter(Boolean);
                }

                fs.removeSync(localPath);
                await processScanning(chatId, numbersToCheck);
            });
        });
    } 
    // Direct Text Messages
    else if (msg.text) {
        numbersToCheck = msg.text.split(/\r?\n/).map(n => n.trim()).filter(Boolean);
        await processScanning(chatId, numbersToCheck);
    }
});

// WhatsApp Scanning Function
async function processScanning(chatId, numbers) {
    if (!waSock) {
        return bot.sendMessage(chatId, "⚠️ **WhatsApp Engine ready nahi hai. Thodi der baad prayas karein.**");
    }

    let registered = [];
    let notRegistered = [];

    bot.sendMessage(chatId, `⚙️ **Checking ${numbers.length} numbers...**`);

    for (let num of numbers) {
        let cleaned = num.replace(/[^0-9]/g, '');
        if (!cleaned) continue;

        try {
            const [result] = await waSock.onWhatsApp(cleaned);
            if (result && result.exists) {
                registered.push(`✅ +${cleaned}`);
            } else {
                notRegistered.push(`❌ +${cleaned}`);
            }
        } catch (e) {
            notRegistered.push(`❌ +${cleaned}`);
        }

        await new Promise(r => setTimeout(r, 400)); // anti-ban delay
    }

    let reportText = `📊 **WhatsApp Check Report**\n\n` +
        `✅ **Registered (${registered.length}):**\n` + registered.slice(0, 50).join('\n') +
        (registered.length > 50 ? `\n...and ${registered.length - 50} more` : '') +
        `\n\n❌ **Not Registered (${notRegistered.length}):**\n` + notRegistered.slice(0, 50).join('\n') +
        (notRegistered.length > 50 ? `\n...and ${notRegistered.length - 50} more` : '');

    bot.sendMessage(chatId, reportText, { parse_mode: 'Markdown' });
}
