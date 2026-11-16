# database.py
import sqlite3
import json
from config import DB_PATH

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            role TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Каналы назначения: у каждого типа — свои поля
        CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,              -- 'vk' | 'max' | 'rutube'
            name TEXT NOT NULL,

            vk_token TEXT,
            vk_group_id TEXT,

            max_chat_id TEXT,

            rutube_token TEXT,
            rutube_channel_id TEXT,

            UNIQUE(type, name)
        );

        CREATE TABLE IF NOT EXISTS categories (
            name TEXT PRIMARY KEY,
            links TEXT
        );

        CREATE TABLE IF NOT EXISTS category_targets (
            category_name TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            PRIMARY KEY (category_name, target_id),
            FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

# ---------- Пользователи ----------
def get_user(user_id):
    with get_conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def add_pending_user(user_id, username=""):
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id, username, role) VALUES (?, ?, 'pending')",
                  (user_id, username))

def approve_user(user_id, role="author"):
    with get_conn() as c:
        c.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))

def ignore_user(user_id):
    with get_conn() as c:
        c.execute("DELETE FROM users WHERE user_id=?", (user_id,))

def get_all_users():
    with get_conn() as c:
        return c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()

def get_pending_users():
    with get_conn() as c:
        return c.execute("SELECT * FROM users WHERE role='pending'").fetchall()

# ---------- Каналы назначения ----------
def list_targets(type_=None):
    with get_conn() as c:
        if type_:
            return c.execute("SELECT * FROM targets WHERE type=? ORDER BY name", (type_,)).fetchall()
        return c.execute("SELECT * FROM targets ORDER BY type, name").fetchall()

def get_target(target_id):
    with get_conn() as c:
        return c.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()

def upsert_target(type_, name, target_id=None, **kwargs):
    """
    Разрешённые kwargs в зависимости от type_:
      vk:     vk_token, vk_group_id
      max:    max_chat_id
      rutube: rutube_token, rutube_channel_id
    Остальные поля сбрасываются в NULL, чтобы не тащить мусор между типами.
    """
    vk_token = kwargs.get("vk_token") if type_ == "vk" else None
    vk_group_id = kwargs.get("vk_group_id") if type_ == "vk" else None
    max_chat_id = kwargs.get("max_chat_id") if type_ == "max" else None
    rutube_token = kwargs.get("rutube_token") if type_ == "rutube" else None
    rutube_channel_id = kwargs.get("rutube_channel_id") if type_ == "rutube" else None

    with get_conn() as c:
        if target_id:
            c.execute("""UPDATE targets SET
                            type=?, name=?,
                            vk_token=?, vk_group_id=?,
                            max_chat_id=?,
                            rutube_token=?, rutube_channel_id=?
                         WHERE id=?""",
                      (type_, name, vk_token, vk_group_id,
                       max_chat_id, rutube_token, rutube_channel_id, target_id))
            return target_id
        cur = c.execute("""INSERT INTO targets
                            (type, name, vk_token, vk_group_id,
                             max_chat_id, rutube_token, rutube_channel_id)
                           VALUES (?,?,?,?,?,?,?)""",
                        (type_, name, vk_token, vk_group_id,
                         max_chat_id, rutube_token, rutube_channel_id))
        return cur.lastrowid

def delete_target(target_id):
    with get_conn() as c:
        c.execute("DELETE FROM targets WHERE id=?", (target_id,))

# ---------- Категории ----------
def list_categories():
    with get_conn() as c:
        return c.execute("SELECT * FROM categories ORDER BY name").fetchall()

def get_category(name):
    with get_conn() as c:
        row = c.execute("SELECT * FROM categories WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        links = json.loads(row["links"]) if row["links"] else []
        targets = c.execute("""SELECT t.* FROM targets t
                               JOIN category_targets ct ON ct.target_id = t.id
                               WHERE ct.category_name = ?""", (name,)).fetchall()
        return {"name": row["name"], "links": links, "targets": targets}

def save_category(name, links, target_ids):
    with get_conn() as c:
        c.execute("INSERT OR REPLACE INTO categories (name, links) VALUES (?, ?)",
                  (name, json.dumps(links)))
        c.execute("DELETE FROM category_targets WHERE category_name=?", (name,))
        for tid in target_ids:
            c.execute("INSERT INTO category_targets (category_name, target_id) VALUES (?, ?)",
                      (name, tid))

def delete_category(name):
    with get_conn() as c:
        c.execute("DELETE FROM category_targets WHERE category_name=?", (name,))
        c.execute("DELETE FROM categories WHERE name=?", (name,))

# ---------- Настройки ----------
def get_setting(key, default=""):
    with get_conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with get_conn() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))