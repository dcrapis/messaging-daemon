/**
 * WhatsApp bridge using Baileys.
 *
 * Exposes a simple HTTP API on port 9250:
 *   GET  /status           — connection state + QR if not yet linked
 *   GET  /messages         — ?since=<epoch_ms> to filter
 *   GET  /chats            — all known chats with names and JIDs
 *   POST /send             — { jid, text }
 *
 * On first run, prints a QR code to the terminal. Scan it with WhatsApp
 * (Linked Devices → Link a Device) to authenticate. Auth state is saved
 * to ./auth_state/ so subsequent restarts don't need a QR scan.
 */

import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  jidNormalizedUser,
} from "@whiskeysockets/baileys";
import qrcode from "qrcode-terminal";
import { createServer } from "http";
import { readFileSync, existsSync, mkdirSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join, resolve } from "path";
import pino from "pino";
import tls from "tls";
import os from "os";

// Load custom CA bundle if present (needed for corporate proxy/VPN setups)
const CERTS_PATH = resolve(os.homedir(), "certs.pem");
if (existsSync(CERTS_PATH)) {
  const extra = readFileSync(CERTS_PATH, "utf8");
  const orig = tls.createSecureContext;
  tls.createSecureContext = (opts = {}) => {
    const ctx = orig(opts);
    ctx.context.addCACert(extra);
    return ctx;
  };
}

const __dirname = dirname(fileURLToPath(import.meta.url));
const AUTH_DIR = join(__dirname, "auth_state");
const PORT = 9250;

mkdirSync(AUTH_DIR, { recursive: true });

// In-memory stores
const messages = [];
const chats = new Map();    // jid -> { jid, name, isGroup }
const contacts = new Map(); // jid -> name

let currentQR = null;
let connectionState = "connecting";
let sock = null;

function respond(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function upsertChat(id, name) {
  const existing = chats.get(id);
  chats.set(id, {
    jid: id,
    name: name || existing?.name || id,
    isGroup: id.endsWith("@g.us") || id.endsWith("@broadcast"),
  });
}

async function connectToWhatsApp() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, pino({ level: "silent" })),
    },
    logger: pino({ level: "silent" }),
    printQRInTerminal: false,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      currentQR = qr;
      connectionState = "qr";
      console.log("\nScan this QR code with WhatsApp (Linked Devices → Link a Device):\n");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      currentQR = null;
      connectionState = "connected";
      console.log("WhatsApp connected.");
    }
    if (connection === "close") {
      connectionState = "disconnected";
      const code = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = code !== DisconnectReason.loggedOut;
      console.log("Connection closed, code:", code, "reconnecting:", shouldReconnect);
      if (shouldReconnect) setTimeout(connectToWhatsApp, 3000);
    }
  });

  // Populate chats on initial load
  sock.ev.on("chats.set", ({ chats: initial }) => {
    for (const chat of initial) {
      upsertChat(chat.id, chat.name);
    }
    console.log(`Loaded ${initial.length} chats.`);
  });

  sock.ev.on("chats.upsert", (newChats) => {
    for (const chat of newChats) upsertChat(chat.id, chat.name);
  });

  sock.ev.on("chats.update", (updates) => {
    for (const u of updates) {
      if (u.id && u.name) upsertChat(u.id, u.name);
    }
  });

  sock.ev.on("contacts.upsert", (newContacts) => {
    for (const c of newContacts) {
      const name = c.name || c.notify || c.verifiedName;
      if (name) {
        contacts.set(c.id, name);
        // Backfill chat name if it was only a JID before
        const chat = chats.get(c.id);
        if (chat && chat.name === c.id) chat.name = name;
      }
    }
  });

  sock.ev.on("messages.upsert", ({ messages: incoming, type }) => {
    if (type !== "notify") return;
    for (const msg of incoming) {
      if (!msg.message) continue;

      // Track push names for sender lookup
      if (msg.pushName && msg.key.remoteJid) {
        const senderJid = msg.key.participant || msg.key.remoteJid;
        if (!contacts.has(senderJid)) contacts.set(senderJid, msg.pushName);
        upsertChat(msg.key.remoteJid, chats.get(msg.key.remoteJid)?.name);
      }

      if (msg.key.fromMe) continue;

      const text =
        msg.message.conversation ||
        msg.message.extendedTextMessage?.text ||
        msg.message.imageMessage?.caption ||
        null;
      if (!text) continue;

      const senderJid = msg.key.participant || msg.key.remoteJid;
      messages.push({
        id: msg.key.id,
        jid: msg.key.remoteJid,
        sender: senderJid,
        sender_name: msg.pushName || contacts.get(senderJid) || null,
        chat_name: chats.get(msg.key.remoteJid)?.name || null,
        text,
        timestamp_ms: (msg.messageTimestamp || 0) * 1000,
        received_at: Date.now(),
      });
    }
  });
}

// ── HTTP server ───────────────────────────────────────────────────────────────

createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  if (req.method === "GET" && url.pathname === "/status") {
    return respond(res, 200, {
      state: connectionState,
      qr: currentQR,
      self: sock?.user ? jidNormalizedUser(sock.user.id) : null,
    });
  }

  if (req.method === "GET" && url.pathname === "/messages") {
    const since = parseInt(url.searchParams.get("since") || "0", 10);
    const filtered = messages.filter((m) => m.received_at > since);
    return respond(res, 200, { messages: filtered });
  }

  if (req.method === "GET" && url.pathname === "/chats") {
    const list = Array.from(chats.values()).sort((a, b) =>
      a.name.localeCompare(b.name)
    );
    return respond(res, 200, { chats: list });
  }

  if (req.method === "GET" && url.pathname === "/contacts") {
    const list = Array.from(contacts.entries())
      .map(([jid, name]) => ({ jid, name }))
      .sort((a, b) => a.name.localeCompare(b.name));
    return respond(res, 200, { contacts: list });
  }

  // Fetch all groups from WhatsApp servers and populate chats map
  if (req.method === "POST" && url.pathname === "/fetch-groups") {
    if (connectionState !== "connected") return respond(res, 503, { error: "not connected" });
    try {
      const groups = await sock.groupFetchAllParticipating();
      for (const [jid, meta] of Object.entries(groups)) {
        upsertChat(jid, meta.subject);
      }
      return respond(res, 200, { fetched: Object.keys(groups).length });
    } catch (e) {
      return respond(res, 500, { error: e.message });
    }
  }

  // Look up a phone number on WhatsApp — returns JID if registered
  if (req.method === "GET" && url.pathname === "/lookup") {
    const phone = url.searchParams.get("phone");
    if (!phone) return respond(res, 400, { error: "missing phone parameter" });
    if (connectionState !== "connected") return respond(res, 503, { error: "not connected" });
    try {
      const results = await sock.onWhatsApp(phone);
      if (!results || results.length === 0) return respond(res, 404, { error: "not on WhatsApp" });
      const jid = results[0].jid;
      upsertChat(jid, contacts.get(jid) || phone);
      return respond(res, 200, { jid, exists: results[0].exists });
    } catch (e) {
      return respond(res, 500, { error: e.message });
    }
  }

  if (req.method === "POST" && url.pathname === "/send") {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", async () => {
      try {
        const { jid, text } = JSON.parse(body);
        if (!jid || !text) return respond(res, 400, { error: "missing jid or text" });
        if (connectionState !== "connected") return respond(res, 503, { error: "not connected" });
        await sock.sendMessage(jid, { text });
        respond(res, 200, { ok: true });
      } catch (e) {
        respond(res, 500, { error: e.message });
      }
    });
    return;
  }

  respond(res, 404, { error: "not found" });
}).listen(PORT, "127.0.0.1", () => {
  console.log(`WhatsApp bridge listening on http://127.0.0.1:${PORT}`);
});

connectToWhatsApp();
