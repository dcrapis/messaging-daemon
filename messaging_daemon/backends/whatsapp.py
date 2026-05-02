"""
backends/whatsapp.py — WhatsApp backend via a local Baileys bridge.

Requires the Node.js bridge to be running:
    node ~/messaging-daemon/whatsapp-bridge/index.js

The bridge exposes a simple HTTP API on 127.0.0.1:9250.
On first run it prints a QR code — scan it with WhatsApp
(Linked Devices → Link a Device) to authenticate.

CLI:
    messaging-daemon whatsapp status
    messaging-daemon whatsapp chats     — list known chats with JIDs
"""

import argparse
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from ..db import DB_PATH, store_message
from .base import Backend

BRIDGE_URL = "http://127.0.0.1:9250"


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BRIDGE_URL}{path}", timeout=10) as r:
        return json.loads(r.read())


def _post(path: str, data: dict) -> dict:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BRIDGE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class WhatsAppBackend(Backend):
    name = "whatsapp"

    def _bridge_status(self) -> dict | None:
        try:
            return _get("/status")
        except Exception:
            return None

    def _get_chats(self) -> list[dict]:
        try:
            return _get("/chats").get("chats", [])
        except Exception:
            return []

    def _fetch_groups(self) -> int:
        try:
            result = _post("/fetch-groups", {})
            return result.get("fetched", 0)
        except Exception:
            return 0

    def _lookup_phone(self, phone: str) -> str | None:
        try:
            result = _get(f"/lookup?phone={urllib.parse.quote(phone)}")
            return result.get("jid")
        except Exception:
            return None

    def _resolve_jid(self, recipient: str) -> str:
        """Resolve a display name or phone number to a JID."""
        if "@" in recipient:
            return recipient
        # Looks like a phone number — try direct lookup
        digits = recipient.replace("+", "").replace(" ", "").replace("-", "")
        if digits.isdigit():
            jid = self._lookup_phone(recipient)
            if jid:
                return jid
            raise RuntimeError(f"Phone number {recipient} is not on WhatsApp")

        # Name-based: search chats (fetch groups first to populate)
        self._fetch_groups()
        chats = self._get_chats()
        rl = recipient.strip().lower()
        for chat in chats:
            if chat.get("name", "").lower() == rl:
                return chat["jid"]
        matches = [c for c in chats if rl in c.get("name", "").lower()]
        if len(matches) == 1:
            return matches[0]["jid"]
        if len(matches) > 1:
            names = ", ".join(c["name"] for c in matches)
            raise RuntimeError(f"Ambiguous recipient '{recipient}' — matches: {names}")
        raise RuntimeError(f"No WhatsApp chat found for '{recipient}'")

    # ── Account management ────────────────────────────────────────────────────

    def accounts(self) -> list[dict]:
        status = self._bridge_status()
        if not status or status.get("state") != "connected":
            return []
        self_jid = status.get("self") or "whatsapp"
        return [{"account": self_jid, "backend": self.name}]

    # ── CLI ───────────────────────────────────────────────────────────────────

    def register_commands(self, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser("whatsapp", help="WhatsApp backend commands")
        ws = p.add_subparsers(dest="whatsapp_command")
        ws.add_parser("status", help="Show bridge connection status")
        ws.add_parser("chats", help="List known chats with JIDs")
        ws.add_parser("fetch-groups", help="Fetch all WhatsApp groups from server")

    def handle_command(self, args: argparse.Namespace) -> bool:
        if args.command != "whatsapp":
            return False

        if args.whatsapp_command == "fetch-groups":
            n = self._fetch_groups()
            print(f"Fetched {n} groups.")
            return True

        if args.whatsapp_command == "chats":
            chats = self._get_chats()
            if not chats:
                print("No chats found (bridge may need a moment to load them after connect).")
            for c in chats:
                tag = "[group]" if c.get("isGroup") else "[dm]"
                print(f"  {tag} {c['name']:<40} {c['jid']}")
            return True

        status = self._bridge_status()
        if not status:
            print("WhatsApp bridge is not running.")
            print("Start it with: node ~/messaging-daemon/whatsapp-bridge/index.js")
        else:
            state = status.get("state", "unknown")
            self_jid = status.get("self")
            if state == "connected":
                print(f"Connected as {self_jid}")
            elif state == "qr":
                print("Waiting for QR scan — check the bridge terminal.")
            else:
                print(f"Bridge state: {state}")
        return True

    # ── Recipient helpers ─────────────────────────────────────────────────────

    def is_self(self, account: str, recipient: str) -> bool:
        return recipient.strip() == account.strip()

    def resolve_display_name(self, account: str, recipient: str) -> str:
        if "@" not in recipient:
            return recipient
        chats = self._get_chats()
        for chat in chats:
            if chat["jid"] == recipient:
                return chat["name"]
        return recipient

    # ── Sending ───────────────────────────────────────────────────────────────

    def send(self, account: str, recipient: str, body: str, subject: str | None = None) -> None:
        jid = self._resolve_jid(recipient)
        try:
            result = _post("/send", {"jid": jid, "text": body})
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "unknown error"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"WhatsApp bridge unreachable: {e}")

    # ── Polling ───────────────────────────────────────────────────────────────

    def poll(self, db: sqlite3.Connection) -> int:
        status = self._bridge_status()
        if not status:
            print("  [whatsapp] Bridge not running — skipping poll.")
            return 0
        if status.get("state") != "connected":
            state = status.get("state")
            if state == "qr":
                print("  [whatsapp] Waiting for QR scan.")
            else:
                print(f"  [whatsapp] Not connected ({state}) — skipping poll.")
            return 0

        row = db.execute(
            "SELECT value FROM config WHERE key='whatsapp_last_poll_ms'"
        ).fetchone()
        since = int(row[0]) if row else 0

        try:
            data = _get(f"/messages?since={since}")
        except Exception as exc:
            print(f"  [whatsapp] Poll error: {exc}")
            return 0

        incoming = data.get("messages", [])
        count = 0
        max_ts = since

        for m in incoming:
            max_ts = max(max_ts, m.get("received_at", 0))
            msg = {
                "backend":      self.name,
                "account":      status.get("self", "whatsapp"),
                "uid":          m["id"],
                "sender":       m["sender"],
                "sender_name":  m.get("sender_name"),
                "recipient":    m["jid"],
                "thread_id":    m["jid"],
                "body":         m["text"],
                "timestamp_ms": m.get("timestamp_ms", 0),
                "metadata":     {k: v for k, v in m.items() if k != "text"},
            }
            if store_message(db, msg):
                count += 1

        if max_ts > since:
            db.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES ('whatsapp_last_poll_ms', ?)",
                (str(max_ts),),
            )
            db.commit()

        return count

    # ── Confirmation page fields ──────────────────────────────────────────────

    def confirmation_fields(self, account, recipient, body, subject):
        display = self.resolve_display_name(account, recipient)
        return [
            ("From", account),
            ("To", display),
            ("Message", body),
        ]
