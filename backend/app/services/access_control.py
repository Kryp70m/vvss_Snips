import secrets
import sqlite3
import string
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def random_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class AccessControl:
    def __init__(self, package_root: Path) -> None:
        self.package_root = package_root
        default_data_dir = (
            package_root / "backend" / "app" / "data"
            if os.name == "nt"
            else Path("/root/alpha-sniper-data")
        )
        self.data_dir = Path(os.getenv("ALPHA_SNIPER_DATA_DIR", str(default_data_dir)))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "access.sqlite3"
        self.admin_password_path = self.data_dir / "ADMIN_PASSWORD.txt"
        self.admin_password_fallback_paths = [
            self.admin_password_path,
            package_root / "ADMIN_PASSWORD.txt",
            package_root / "backend" / "ADMIN_PASSWORD.txt",
            Path.cwd() / "ADMIN_PASSWORD.txt",
        ]
        self._ensure_admin_password()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_admin_password(self) -> None:
        for path in self.admin_password_fallback_paths:
            if path.exists():
                password = path.read_text(encoding="utf-8").strip()
                if password:
                    if path != self.admin_password_path and not self.admin_password_path.exists():
                        self.admin_password_path.write_text(password + "\n", encoding="utf-8")
                    return
        password = random_code(12)
        self.admin_password_path.write_text(password + "\n", encoding="utf-8")

    def _admin_password(self) -> str:
        return self.admin_password_path.read_text(encoding="utf-8").strip()

    def _admin_passwords(self) -> list[str]:
        passwords: list[str] = []
        for path in self.admin_password_fallback_paths:
            try:
                password = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                continue
            if password and password not in passwords:
                passwords.append(password)
        return passwords

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    days INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    used_at TEXT,
                    expires_at TEXT,
                    revoked_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    pin_id INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    ip TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_settings (
                    pin_id INTEGER PRIMARY KEY,
                    settings_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS site_content (
                    key TEXT PRIMARY KEY,
                    content_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    pin_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL DEFAULT 'binance',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (pin_id, symbol, exchange)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Magic-link invite tokens — single-use activation links
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS invite_tokens (
                    token TEXT PRIMARY KEY,
                    days INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    used_at TEXT,
                    pin_id INTEGER,
                    expires_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    # ── site content ────────────────────────────────────────────────────────

    def default_site_content(self) -> dict:
        return {
            "subscription_label": "Monthly",
            "subscription_amount": "$7",
            "subscription_compare_amount": "$11",
            "discount_text": "Discount valid only for 3 days",
            "free_use_title": "FREE USE",
            "free_use_terms": "\n".join(
                [
                    "Step 1. Create account on BingX through our link",
                    "Step 2. Deposit $100",
                    "Step 3. Take 10 trades of $100 with 10x leverage",
                    "Step 4. Send screenshot to admin and take free access for 1 day",
                ]
            ),
            "contact_text": "Contact Admin: @ShibutradesYT",
            "contact_link": "https://t.me/ShibutradesYT",
            "footer_free_text": "Keep trading and generate monthly trading volume of $1M to use this free of cost for now. No paid version.",
            "admin_chat_title": "ADMIN CHAT",
            "admin_chat_message": "Welcome. Admin updates will appear here.",
            "admin_chat_messages": [
                {
                    "id": "welcome",
                    "message": "Welcome. Admin updates will appear here.",
                    "created_at": iso(),
                }
            ],
        }

    def _clean_chat_messages(self, messages: list[dict] | object) -> list[dict]:
        if not isinstance(messages, list):
            return []
        cleaned: list[dict] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or item.get("text") or "").strip()
            if not message:
                continue
            cleaned.append(
                {
                    "id": str(item.get("id") or secrets.token_urlsafe(8)),
                    "message": message[:5000],
                    "created_at": str(item.get("created_at") or iso()),
                }
            )
        return cleaned[:30]

    def get_site_content(self) -> dict:
        content = self.default_site_content()
        with self._connect() as conn:
            row = conn.execute("SELECT content_json FROM site_content WHERE key = 'main'").fetchone()
            if not row:
                return content
            try:
                stored = json.loads(row["content_json"])
            except json.JSONDecodeError:
                return content
            for key in content.keys() & stored.keys():
                if key == "admin_chat_messages":
                    content[key] = self._clean_chat_messages(stored[key])
                else:
                    content[key] = str(stored[key])
            if "admin_chat_messages" not in stored and str(stored.get("admin_chat_message") or "").strip():
                content["admin_chat_messages"] = [
                    {
                        "id": secrets.token_urlsafe(8),
                        "message": str(stored.get("admin_chat_message")).strip()[:5000],
                        "created_at": str(stored.get("updated_at") or iso()),
                    }
                ]
            else:
                content["admin_chat_messages"] = self._clean_chat_messages(content.get("admin_chat_messages"))
            if content["admin_chat_messages"]:
                content["admin_chat_message"] = content["admin_chat_messages"][0]["message"]
            return content

    def update_site_content(self, updates: dict) -> dict:
        content = self.get_site_content()
        for key, value in updates.items():
            if key not in content or value is None:
                continue
            if key == "admin_chat_messages":
                content[key] = self._clean_chat_messages(value)
            else:
                content[key] = str(value).strip()[:5000]
        content["admin_chat_messages"] = self._clean_chat_messages(content.get("admin_chat_messages"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO site_content(key, content_json, updated_at)
                VALUES ('main', ?, ?)
                ON CONFLICT(key) DO UPDATE SET content_json = excluded.content_json, updated_at = excluded.updated_at
                """,
                (json.dumps(content, separators=(",", ":")), iso()),
            )
            conn.commit()
        return content

    def add_admin_chat_message(self, message: str) -> dict:
        content = self.get_site_content()
        clean_message = str(message or "").strip()[:5000]
        if not clean_message:
            raise ValueError("Message cannot be empty")
        messages = [
            {"id": secrets.token_urlsafe(8), "message": clean_message, "created_at": iso()},
            *self._clean_chat_messages(content.get("admin_chat_messages")),
        ][:30]
        content["admin_chat_messages"] = messages
        content["admin_chat_message"] = messages[0]["message"]
        return self.update_site_content(content)

    def update_admin_chat_message(self, message_id: str, message: str) -> dict:
        content = self.get_site_content()
        clean_message = str(message or "").strip()[:5000]
        if not clean_message:
            raise ValueError("Message cannot be empty")
        messages = self._clean_chat_messages(content.get("admin_chat_messages"))
        for item in messages:
            if item["id"] == message_id:
                item["message"] = clean_message
                content["admin_chat_messages"] = messages[:30]
                content["admin_chat_message"] = messages[0]["message"] if messages else ""
                return self.update_site_content(content)
        raise ValueError("Message not found")

    def delete_admin_chat_message(self, message_id: str) -> dict:
        content = self.get_site_content()
        messages = [item for item in self._clean_chat_messages(content.get("admin_chat_messages")) if item["id"] != message_id]
        content["admin_chat_messages"] = messages[:30]
        content["admin_chat_message"] = messages[0]["message"] if messages else ""
        return self.update_site_content(content)

    # ── auth ─────────────────────────────────────────────────────────────────

    def admin_login(self, password: str, ip: str = "", user_agent: str = "") -> dict:
        supplied = str(password or "")
        if not any(secrets.compare_digest(supplied, current) for current in self._admin_passwords()):
            raise ValueError("Wrong owner password")
        token = secrets.token_urlsafe(32)
        expires_at = now_utc() + timedelta(days=30)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions(token, role, pin_id, created_at, expires_at, last_seen_at, ip, user_agent, active)
                VALUES (?, 'admin', NULL, ?, ?, ?, ?, ?, 1)
                """,
                (token, iso(), iso(expires_at), iso(), ip, user_agent),
            )
            conn.commit()
        return {"token": token, "expires_at": iso(expires_at), "role": "admin"}

    def login_with_pin(self, code: str, ip: str = "", user_agent: str = "") -> dict:
        """
        Login with a PIN code.

        Key fix: PINs are reusable across server restarts.
        - First use: marks used_at, creates a session valid for `days` days.
        - Subsequent logins with the same PIN (e.g. after server restart or token
          expiry): creates a NEW session using the original expiry from the pin row.
          This means users never get "invalid PIN" just because the server restarted.
        - Revoked or expired pins are still rejected.
        """
        clean_code = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM pins WHERE code = ?", (clean_code,)).fetchone()
            if not row:
                raise ValueError("Invalid PIN — check with your admin")
            if row["revoked_at"]:
                raise ValueError("PIN has been revoked — contact admin")

            # Determine expiry
            if row["expires_at"]:
                expires_at = parse_iso(row["expires_at"])
            elif row["used_at"]:
                # Legacy row: no expires_at stored, derive from used_at + days
                expires_at = parse_iso(row["used_at"]) + timedelta(days=int(row["days"]))
            else:
                # First use
                expires_at = now_utc() + timedelta(days=int(row["days"]))

            if expires_at and expires_at <= now_utc():
                raise ValueError("Subscription expired — contact admin to renew")

            # First use: stamp used_at and expires_at
            if not row["used_at"]:
                conn.execute(
                    "UPDATE pins SET used_at = ?, expires_at = ? WHERE id = ?",
                    (iso(), iso(expires_at), row["id"]),
                )

            # Always issue a fresh session token (safe for re-login after restart)
            token = secrets.token_urlsafe(32)
            conn.execute(
                """
                INSERT INTO sessions(token, role, pin_id, created_at, expires_at, last_seen_at, ip, user_agent, active)
                VALUES (?, 'user', ?, ?, ?, ?, ?, ?, 1)
                """,
                (token, row["id"], iso(), iso(expires_at), iso(), ip, user_agent),
            )
            conn.commit()

        days_left = max(0, (expires_at - now_utc()).days) if expires_at else 0
        return {
            "token": token,
            "expires_at": iso(expires_at),
            "role": "user",
            "days_left": days_left,
            "subscription_label": f"{days_left} day{'s' if days_left != 1 else ''} remaining",
        }

    # ── magic-link invite tokens ─────────────────────────────────────────────

    def create_invite(self, days: int, note: str = "") -> dict:
        """
        Create a single-use activation link token.
        Admin generates this, sends the URL to the user.
        User clicks it, sets their own PIN, account is created.
        """
        days = max(1, min(365, int(days or 30)))
        token = secrets.token_urlsafe(24)
        # Invite link itself expires in 72 h if not used
        link_expires_at = now_utc() + timedelta(hours=72)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO invite_tokens(token, days, note, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (token, days, str(note or "")[:120], iso(), iso(link_expires_at)),
            )
            conn.commit()
        return {
            "token": token,
            "days": days,
            "note": note,
            "created_at": iso(),
            "link_expires_at": iso(link_expires_at),
        }

    def activate_invite(self, invite_token: str, desired_pin: str, ip: str = "", user_agent: str = "") -> dict:
        """
        User arrives via magic link, optionally sets their own PIN, account created.
        Returns a session token — user is logged in immediately.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM invite_tokens WHERE token = ?", (invite_token,)).fetchone()
            if not row:
                raise ValueError("Invite link not found or already used")
            if row["used_at"]:
                raise ValueError("This invite link has already been used")
            link_expires = parse_iso(row["expires_at"])
            if link_expires and link_expires <= now_utc():
                raise ValueError("Invite link has expired — ask admin for a new one")

            # Validate / auto-generate PIN
            clean_pin = "".join(ch for ch in str(desired_pin or "").upper() if ch.isalnum())
            if len(clean_pin) < 4:
                clean_pin = random_code(8)

            # Create the pin
            pin_expires_at = now_utc() + timedelta(days=int(row["days"]))
            for _ in range(10):
                try:
                    conn.execute(
                        "INSERT INTO pins(code, days, note, created_at, used_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (clean_pin, row["days"], row["note"], iso(), iso(), iso(pin_expires_at)),
                    )
                    pin_row = conn.execute("SELECT * FROM pins WHERE code = ?", (clean_pin,)).fetchone()
                    break
                except sqlite3.IntegrityError:
                    clean_pin = random_code(8)
            else:
                raise ValueError("Could not create a unique PIN")

            # Mark invite used
            conn.execute(
                "UPDATE invite_tokens SET used_at = ?, pin_id = ? WHERE token = ?",
                (iso(), pin_row["id"], invite_token),
            )

            # Issue session
            session_token = secrets.token_urlsafe(32)
            conn.execute(
                """
                INSERT INTO sessions(token, role, pin_id, created_at, expires_at, last_seen_at, ip, user_agent, active)
                VALUES (?, 'user', ?, ?, ?, ?, ?, ?, 1)
                """,
                (session_token, pin_row["id"], iso(), iso(pin_expires_at), iso(), ip, user_agent),
            )
            conn.commit()

        days_left = max(0, (pin_expires_at - now_utc()).days)
        return {
            "token": session_token,
            "pin": clean_pin,
            "expires_at": iso(pin_expires_at),
            "role": "user",
            "days_left": days_left,
            "subscription_label": f"{days_left} day{'s' if days_left != 1 else ''} remaining",
        }

    def list_invites(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invite_tokens ORDER BY rowid DESC LIMIT 100"
            ).fetchall()
        result = []
        for row in rows:
            link_expires = parse_iso(row["expires_at"])
            if row["used_at"]:
                status = "used"
            elif link_expires and link_expires <= now_utc():
                status = "expired"
            else:
                status = "pending"
            result.append({
                "token": row["token"],
                "days": row["days"],
                "note": row["note"],
                "created_at": row["created_at"],
                "used_at": row["used_at"],
                "pin_id": row["pin_id"],
                "link_expires_at": row["expires_at"],
                "status": status,
            })
        return result

    # ── user settings (per-pin, persisted in SQLite) ─────────────────────────

    def default_user_settings(self) -> dict:
        return {
            "paxg_target_move_usd": 10.0,
            "xag_target_move_usd": 10.0,
            "manipulation_sensitivity": 60.0,
            "retracement_percentage": 40.0,
            "liquidity_sensitivity": 55.0,
            "volume_shock_multiplier": 1.0,
            "market_cap_filter": 1500.0,
        }

    def get_user_settings(self, session: dict | None) -> dict:
        settings = self.default_user_settings()
        pin_id = session.get("pin_id") if session else None
        if session and session.get("role") == "admin" and pin_id is None:
            pin_id = 0
        if pin_id is None:
            return settings
        with self._connect() as conn:
            row = conn.execute("SELECT settings_json FROM user_settings WHERE pin_id = ?", (pin_id,)).fetchone()
            if not row:
                return settings
            try:
                stored = json.loads(row["settings_json"])
            except json.JSONDecodeError:
                return settings
            settings.update({key: stored[key] for key in settings.keys() & stored.keys()})
            return settings

    def update_user_settings(self, session: dict | None, updates: dict) -> dict:
        settings = self.get_user_settings(session)
        for key, value in updates.items():
            if key not in settings or value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if key in {"paxg_target_move_usd", "xag_target_move_usd"}:
                numeric = max(1.0, min(100.0, numeric))
            elif key in {"manipulation_sensitivity", "liquidity_sensitivity"}:
                numeric = max(1.0, min(100.0, numeric))
            elif key == "retracement_percentage":
                numeric = max(30.0, min(50.0, numeric))
            elif key == "volume_shock_multiplier":
                numeric = max(0.5, min(3.0, numeric))
            elif key == "market_cap_filter":
                numeric = max(0.0, min(10_000.0, numeric))
            settings[key] = numeric
        pin_id = session.get("pin_id") if session else None
        if session and session.get("role") == "admin" and pin_id is None:
            pin_id = 0
        if pin_id is not None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_settings(pin_id, settings_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(pin_id) DO UPDATE SET settings_json = excluded.settings_json, updated_at = excluded.updated_at
                    """,
                    (pin_id, json.dumps(settings, separators=(",", ":")), iso()),
                )
                conn.commit()
        return settings

    # ── PIN management ────────────────────────────────────────────────────────

    def create_pin(self, days: int, note: str = "", code: str | None = None) -> dict:
        days = max(1, min(365, int(days or 30)))
        clean_code = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
        if len(clean_code) < 4:
            clean_code = random_code(8)
        with self._connect() as conn:
            for _ in range(10):
                try:
                    conn.execute(
                        "INSERT INTO pins(code, days, note, created_at) VALUES (?, ?, ?, ?)",
                        (clean_code, days, str(note or "")[:120], iso()),
                    )
                    conn.commit()
                    row = conn.execute("SELECT * FROM pins WHERE code = ?", (clean_code,)).fetchone()
                    return self._pin_row(row)
                except sqlite3.IntegrityError:
                    clean_code = random_code(8)
            raise ValueError("Could not create a unique PIN")

    def check_session(self, token: str | None, roles: set[str] | None = None) -> dict | None:
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE token = ? AND active = 1", (token,)).fetchone()
            if not row:
                return None
            expires_at = parse_iso(row["expires_at"])
            if not expires_at or expires_at <= now_utc():
                conn.execute("UPDATE sessions SET active = 0 WHERE token = ?", (token,))
                conn.commit()
                return None
            if roles and row["role"] not in roles:
                return None
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token = ?", (iso(), token))
            conn.commit()
            return dict(row)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET active = 0 WHERE token = ?", (token,))
            conn.commit()

    def list_pins(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM pins ORDER BY id DESC LIMIT 300").fetchall()
            return [self._pin_row(row, conn) for row in rows]

    def pins_expiring_soon(self, within_days: int = 7) -> list[dict]:
        """Return pins expiring within `within_days` days — for admin dashboard warning."""
        cutoff = iso(now_utc() + timedelta(days=within_days))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM pins
                WHERE used_at IS NOT NULL
                  AND revoked_at IS NULL
                  AND expires_at IS NOT NULL
                  AND expires_at > ?
                  AND expires_at <= ?
                ORDER BY expires_at ASC
                """,
                (iso(), cutoff),
            ).fetchall()
            return [self._pin_row(row, conn) for row in rows]

    def stats(self) -> dict:
        pins = self.list_pins()
        expiring_soon = self.pins_expiring_soon(7)
        return {
            "total": len(pins),
            "unused": sum(1 for pin in pins if pin["status"] == "unused"),
            "active": sum(1 for pin in pins if pin["status"] == "active"),
            "expired": sum(1 for pin in pins if pin["status"] == "expired"),
            "revoked": sum(1 for pin in pins if pin["status"] == "revoked"),
            "expiring_soon_7d": len(expiring_soon),
            "expiring_soon": expiring_soon,
            "server_time": iso(),
        }

    def revoke_pin(self, pin_id: int) -> dict:
        with self._connect() as conn:
            conn.execute("UPDATE pins SET revoked_at = ? WHERE id = ?", (iso(), pin_id))
            conn.execute("UPDATE sessions SET active = 0 WHERE pin_id = ?", (pin_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
            if not row:
                raise ValueError("PIN not found")
            return self._pin_row(row, conn)

    def extend_pin(self, pin_id: int, days: int) -> dict:
        days = max(1, min(365, int(days or 1)))
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
            if not row:
                raise ValueError("PIN not found")
            base = parse_iso(row["expires_at"]) or now_utc()
            if base < now_utc():
                base = now_utc()
            expires_at = base + timedelta(days=days)
            conn.execute("UPDATE pins SET expires_at = ?, revoked_at = NULL WHERE id = ?", (iso(expires_at), pin_id))
            conn.execute("UPDATE sessions SET expires_at = ?, active = 1 WHERE pin_id = ?", (iso(expires_at), pin_id))
            conn.commit()
            row = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
            return self._pin_row(row, conn)

    def enable_pin(self, pin_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
            if not row:
                raise ValueError("PIN not found")
            expires_at = parse_iso(row["expires_at"]) or (now_utc() + timedelta(days=int(row["days"])))
            if expires_at <= now_utc():
                expires_at = now_utc() + timedelta(days=max(1, int(row["days"])))
            conn.execute("UPDATE pins SET revoked_at = NULL, expires_at = ? WHERE id = ?", (iso(expires_at), pin_id))
            conn.execute("UPDATE sessions SET active = 1, expires_at = ? WHERE pin_id = ?", (iso(expires_at), pin_id))
            conn.commit()
            row = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
            return self._pin_row(row, conn)

    def _pin_id_from_session(self, session: dict | None) -> int | None:
        if not session:
            return None
        pin_id = session.get("pin_id")
        if session.get("role") == "admin" and pin_id is None:
            return 0
        return int(pin_id) if pin_id is not None else None

    def list_favorites(self, session: dict | None) -> list[dict]:
        pin_id = self._pin_id_from_session(session)
        if pin_id is None:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, exchange, created_at FROM favorites WHERE pin_id = ? ORDER BY created_at DESC",
                (pin_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def add_favorite(self, session: dict | None, symbol: str, exchange: str = "binance") -> dict:
        pin_id = self._pin_id_from_session(session)
        if pin_id is None:
            raise ValueError("Login required")
        clean_symbol = "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())
        clean_exchange = "".join(ch for ch in str(exchange or "binance").lower() if ch.isalnum() or ch == "_")[:32] or "binance"
        if not clean_symbol:
            raise ValueError("Symbol required")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO favorites(pin_id, symbol, exchange, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(pin_id, symbol, exchange) DO NOTHING
                """,
                (pin_id, clean_symbol, clean_exchange, iso()),
            )
            conn.commit()
        return {"symbol": clean_symbol, "exchange": clean_exchange}

    def delete_favorite(self, session: dict | None, symbol: str, exchange: str | None = None) -> dict:
        pin_id = self._pin_id_from_session(session)
        if pin_id is None:
            raise ValueError("Login required")
        clean_symbol = "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())
        with self._connect() as conn:
            if exchange:
                clean_exchange = "".join(ch for ch in str(exchange).lower() if ch.isalnum() or ch == "_")[:32]
                conn.execute("DELETE FROM favorites WHERE pin_id = ? AND symbol = ? AND exchange = ?", (pin_id, clean_symbol, clean_exchange))
            else:
                conn.execute("DELETE FROM favorites WHERE pin_id = ? AND symbol = ?", (pin_id, clean_symbol))
            conn.commit()
        return {"symbol": clean_symbol, "deleted": True}

    def get_admin_setting(self, key: str, default):
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM admin_settings WHERE key = ?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value_json"])
            except json.JSONDecodeError:
                return default

    def set_admin_setting(self, key: str, value) -> dict:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO admin_settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, separators=(",", ":")), iso()),
            )
            conn.commit()
        return {"key": key, "value": value, "updated_at": iso()}

    def _pin_row(self, row: sqlite3.Row, conn: sqlite3.Connection | None = None) -> dict:
        expires_at = parse_iso(row["expires_at"])
        if row["revoked_at"]:
            status = "revoked"
        elif expires_at and expires_at <= now_utc():
            status = "expired"
        elif row["used_at"]:
            status = "active"
        else:
            status = "unused"
        days_left = max(0, (expires_at - now_utc()).days) if expires_at else 0
        active_session = None
        if conn is not None:
            session = conn.execute(
                """
                SELECT ip, user_agent, created_at, last_seen_at, expires_at
                FROM sessions
                WHERE pin_id = ? AND role = 'user' AND active = 1
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (row["id"],),
            ).fetchone()
            if session:
                active_session = {
                    "ip": session["ip"],
                    "device": session["user_agent"],
                    "created_at": session["created_at"],
                    "last_seen_at": session["last_seen_at"],
                    "expires_at": session["expires_at"],
                }
        return {
            "id": row["id"],
            "code": row["code"],
            "days": row["days"],
            "note": row["note"],
            "created_at": row["created_at"],
            "used_at": row["used_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "status": status,
            "days_left": days_left,
            "active_session": active_session,
        }
