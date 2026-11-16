# database.py
import sqlite3
import json
from config import DB_PATH, DEFAULT_CATEGORIES

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            role TEXT DEFAULT 'pending',  -- pending / author / admin
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS categories (
            name TEXT PRIMARY KEY,
            channels TEXT,   -- JSON список
            links TEXT       -- JSON список
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        # Заполняем категории по умолчанию, если пусто
        cur = conn.execute("SELECT COUNT(*) FROM categories")
        if cur.fetchone()[0] == 0:
            for name, data in DEFAULT_CATEGORIES.items():
                conn.execute(
                    "INSERT INTO categories (name, channels, links) VALUES (?, ?, ?)",
                    (name, json.dumps(data["channels"]), json.dumps(data["links"])),
                )

def get_user(user_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def add_pending_user(user_id: int, username: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, role) VALUES (?, ?, 'pending')",
            (user_id, username),
        )

def approve_user(user_id: int, role: str = "author"):
    with get_conn() as conn:
        conn.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))

def ignore_user(user_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))

def get_all_users():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()

def get_pending_users():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE role='pending'").fetchall()

def get_categories():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM categories").fetchall()
        result = {}
        for r in rows:
            result[r["name"]] = {
                "channels": json.loads(r["channels"]),
                "links": json.loads(r["links"]),
            }
        return result

def save_category(name: str, channels: list, links: list):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO categories (name, channels, links) VALUES (?, ?, ?)",
            (name, json.dumps(channels), json.dumps(links)),
        )

def get_setting(key: str, default: str = ""):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )