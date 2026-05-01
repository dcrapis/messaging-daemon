"""
backends/telegram.py — Telegram backend via Telethon (MTProto user API).

Authenticates as your real Telegram account, not a bot — gives full access
to all chats and groups, just like Signal.

Requires api_id and api_hash from https://my.telegram.org/apps

CLI:
    messaging-daemon telegram setup --api-id ID --api-hash HASH
    messaging-daemon telegram auth          # one-time phone verification
    messaging-daemon telegram list
"""

import argparse
import asyncio
import concurrent.futures
import json
import os
import sqlite3

from ..db import DB_PATH, get_config, set_config, store_message
from .base import Backend

SESSION_PATH = os.path.expanduser("~/.messaging_daemon/telegram_session")


def _get_client(api_id: int, api_hash: str):
    from telethon.sync import TelegramClient
    from telethon.sessions import SQLiteSession
    return TelegramClient(SESSION_PATH, api_id, api_hash)


class TelegramBackend(Backend):
    name = "telegram"

    # ── Account management ────────────────────────────────────────────────────

    def _get_creds(self, db: sqlite3.Connection) -> tuple[int, str] | None:
        raw = get_config(db, "telegram_creds")
        if not raw:
            return None
        creds = json.loads(raw)
        return int(creds["api_id"]), creds["api_hash"]

    def accounts(self) -> list[dict]:
        db = sqlite3.connect(DB_PATH)
        creds = self._get_creds(db)
        db.close()
        if not creds:
            return []
        try:
            api_id, api_hash = creds
            with _get_client(api_id, api_hash) as client:
                me = client.get_me()
                username = f"@{me.username}" if me.username else f"+{me.phone}"
                return [{"account": username, "backend": self.name}]
        except Exception:
            return [{"account": "telegram", "backend": self.name}]

    # ── CLI ───────────────────────────────────────────────────────────────────

    def register_commands(self, subparsers: argparse._SubParsersAction) -> None:
        p = subparsers.add_parser("telegram", help="Telegram backend commands")
        ts = p.add_subparsers(dest="telegram_command")

        setup = ts.add_parser("setup", help="Save API credentials")
        setup.add_argument("--api-id", required=True, type=int)
        setup.add_argument("--api-hash", required=True)

        ts.add_parser("auth", help="Authenticate with your phone number (run once)")
        ts.add_parser("list", help="Show configured account")

    def handle_command(self, args: argparse.Namespace) -> bool:
        if args.command != "telegram":
            return False

        if args.telegram_command == "setup":
            db = sqlite3.connect(DB_PATH)
            set_config(db, "telegram_creds", json.dumps({
                "api_id": args.api_id,
                "api_hash": args.api_hash,
            }))
            db.close()
            print(f"Telegram credentials saved. Run: messaging-daemon telegram auth")

        elif args.telegram_command == "auth":
            db = sqlite3.connect(DB_PATH)
            creds = self._get_creds(db)
            db.close()
            if not creds:
                print("Run setup first: messaging-daemon telegram setup --api-id ID --api-hash HASH")
                return True
            api_id, api_hash = creds
            with _get_client(api_id, api_hash) as client:
                client.start()
                me = client.get_me()
                name = f"@{me.username}" if me.username else f"+{me.phone}"
                print(f"Authenticated as {name}")

        elif args.telegram_command == "list":
            accts = self.accounts()
            if not accts:
                print("No Telegram account configured.")
            for a in accts:
                print(f"  {a['account']}")

        else:
            print("Usage: messaging-daemon telegram [setup|auth|list]")
        return True

    # ── Recipient helpers ─────────────────────────────────────────────────────

    def is_self(self, account: str, recipient: str) -> bool:
        return recipient.strip().lower() == account.strip().lower()

    def resolve_display_name(self, account: str, recipient: str) -> str:
        return recipient

    # ── Sending ───────────────────────────────────────────────────────────────

    @staticmethod
    def _in_thread(fn):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(fn).result()

    @staticmethod
    def _parse_recipient(recipient: str):
        try:
            return int(recipient)
        except (ValueError, TypeError):
            return recipient

    def send(self, account: str, recipient: str, body: str, subject: str | None = None) -> None:
        db = sqlite3.connect(DB_PATH)
        creds = self._get_creds(db)
        db.close()
        if not creds:
            raise RuntimeError("No Telegram credentials configured")
        api_id, api_hash = creds
        entity = self._parse_recipient(recipient)

        def _send():
            with _get_client(api_id, api_hash) as client:
                client.send_message(entity, body)

        self._in_thread(_send)

    # ── Polling ───────────────────────────────────────────────────────────────

    def poll(self, db: sqlite3.Connection) -> int:
        db_conn = sqlite3.connect(DB_PATH)
        creds = self._get_creds(db_conn)
        if not creds:
            print("  [telegram] No credentials configured — skipping poll.")
            db_conn.close()
            return 0

        raw_offset = get_config(db_conn, "telegram_update_offset")
        offset_id = int(raw_offset) if raw_offset else 0
        api_id, api_hash = creds

        def _fetch():
            records = []
            new_offset = offset_id
            with _get_client(api_id, api_hash) as client:
                me = client.get_me()
                account = f"@{me.username}" if me.username else f"+{me.phone}"

                messages = client.get_messages(None, limit=50, min_id=offset_id)
                for msg in reversed(list(messages)):
                    if not msg.text:
                        continue
                    new_offset = max(new_offset, msg.id)

                    sender_id = str(msg.sender_id or "")
                    sender_name = None
                    try:
                        sender = msg.get_sender()
                        if sender:
                            if hasattr(sender, "username") and sender.username:
                                sender_name = f"@{sender.username}"
                            elif hasattr(sender, "first_name"):
                                sender_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
                            elif hasattr(sender, "title"):
                                sender_name = sender.title
                    except Exception:
                        pass

                    chat_id = str(msg.chat_id or msg.peer_id)
                    records.append({
                        "backend":      self.name,
                        "account":      account,
                        "uid":          str(msg.id),
                        "sender":       sender_id,
                        "sender_name":  sender_name,
                        "recipient":    chat_id,
                        "thread_id":    chat_id,
                        "body":         msg.text,
                        "timestamp_ms": int(msg.date.timestamp() * 1000),
                        "metadata":     {"message_id": msg.id, "chat_id": chat_id},
                    })
            return records, new_offset

        try:
            records, new_offset = self._in_thread(_fetch)
        except Exception as exc:
            print(f"  [telegram] Poll error: {exc}")
            db_conn.close()
            return 0

        count = sum(1 for r in records if store_message(db, r))
        if new_offset > offset_id:
            set_config(db_conn, "telegram_update_offset", str(new_offset))
        db_conn.close()
        return count

    # ── Confirmation page fields ──────────────────────────────────────────────

    def confirmation_fields(self, account, recipient, body, subject):
        return [
            ("From", account),
            ("To", recipient),
            ("Message", body),
        ]
