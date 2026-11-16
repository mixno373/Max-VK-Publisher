# -*- coding: utf-8 -*-
"""
PostPult — оконное приложение для приёма постов через MAX.

Python 3.11+
Зависимости:
    requests requests-toolbelt truststore cryptography tzdata

Запуск:
    python postpult.py

Данные:
    Windows: %LOCALAPPDATA%\\PostPultStandalone
    Linux/macOS: ~/.postpult-standalone

ВНИМАНИЕ:
    secrets.key нужен для расшифровки настроек.
    Для резервного копирования сохраняйте всю папку данных.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import queue
import re
import socket
import sqlite3
import threading
import time
import uuid

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import truststore

# Используем системное хранилище доверия.
# Проверка сертификатов НЕ отключается.
truststore.inject_into_ssl()

import requests

from cryptography.fernet import Fernet
from requests_toolbelt.multipart.encoder import MultipartEncoder

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ---------------------------------------------------------------------------
# Общие настройки
# ---------------------------------------------------------------------------

if os.name == "nt":
    HOME = Path(
        os.environ.get("LOCALAPPDATA", str(Path.home()))
    ) / "PostPultStandalone"
else:
    HOME = Path.home() / ".postpult-standalone"

HOME.mkdir(parents=True, exist_ok=True)
MEDIA_DIR = HOME / "media"
MEDIA_DIR.mkdir(exist_ok=True)

CATEGORIES = [
    "ВТК",
    "Казачья община",
    "Лазертаг арена",
    "Секция",
    "ВТИ-news",
    "Байкальский рубеж",
]

PLATFORMS = ["VK", "MAX", "Rutube"]

ROUTE_DEFAULT = {
    "vk_group": "",
    "vk_token": "",
    "max_chat": "",
    "max_token": "",
    "rutube_channel": "",
    "rutube_token": "",
    "rutube_url": "",
    "links": [],
}

DEFAULT_CONFIG = {
    "receive_token": "",
    "admin_ids": "",
    "max_base": "https://platform-api2.max.ru",
    "ai_url": "https://ask.chadgpt.ru/api/v1/chat/completions",
    "ai_token": "",
    "ai_model": "",
    "timezone": "Asia/Irkutsk",
    "ca_bundle": "",
    "categories": {
        name: copy.deepcopy(ROUTE_DEFAULT)
        for name in CATEGORIES
    },
}

logging.basicConfig(
    filename=str(HOME / "postpult.log"),
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s %(levelname)s %(message)s",
)


def js(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def now():
    return time.time()


def date_text(value, timezone):
    if not value:
        return "сейчас"
    return datetime.fromtimestamp(
        float(value), ZoneInfo(timezone)
    ).strftime("%d.%m.%Y %H:%M")


def validate_url(url):
    parsed = urlparse(str(url).strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(
            "Нужен HTTPS URL без логина, пароля и фрагмента."
        )
    return parsed


def validate_config(cfg):
    validate_url(cfg["max_base"])
    validate_url(cfg["ai_url"])
    ZoneInfo(cfg["timezone"])

    bundle = cfg.get("ca_bundle", "").strip()
    if bundle and not Path(bundle).is_file():
        raise ValueError("Указанный CA bundle не найден.")

    for category in CATEGORIES:
        route = cfg["categories"][category]
        if route.get("rutube_url"):
            validate_url(route["rutube_url"])

        group = str(route.get("vk_group", "")).strip()
        if group and not group.lstrip("-").isdigit():
            raise ValueError(
                f"{category}: ID группы VK должен быть числом."
            )

        chat = str(route.get("max_chat", "")).strip()
        if chat and not chat.lstrip("-").isdigit():
            raise ValueError(
                f"{category}: ID чата MAX должен быть числом."
            )

        for link in route.get("links", []):
            validate_url(link)


class Rejected(RuntimeError):
    """Сервис явно отклонил запрос."""

    def __init__(self, text, code=""):
        super().__init__(text)
        self.code = str(code)


class Uncertain(RuntimeError):
    """Нельзя достоверно определить результат внешнего действия."""


# ---------------------------------------------------------------------------
# SQLite и шифрование конфигурации
# ---------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.path = HOME / "postpult.sqlite3"
        key_path = HOME / "secrets.key"

        if not key_path.exists():
            if self.path.exists():
                raise RuntimeError(
                    "Найдена база, но отсутствует secrets.key. "
                    "Восстановите ключ из резервной копии."
                )

            fd = os.open(
                str(key_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "wb") as file:
                file.write(Fernet.generate_key())

        self.cipher = Fernet(key_path.read_bytes().strip())

        with self.cx() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    data TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    uid TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    role TEXT NOT NULL DEFAULT 'author',
                    allowed TEXT NOT NULL DEFAULT '[]',
                    notified INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drafts (
                    uid TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY,
                    bot TEXT NOT NULL,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    uid TEXT NOT NULL,
                    category TEXT NOT NULL,
                    data TEXT NOT NULL,
                    due REAL NOT NULL,
                    created REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    job_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    media_index INTEGER NOT NULL DEFAULT -1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result TEXT NOT NULL DEFAULT '',
                    updated REAL NOT NULL,
                    PRIMARY KEY(job_id, target)
                );
            """)

            exists = db.execute(
                "SELECT 1 FROM settings WHERE id=1"
            ).fetchone()

            if not exists:
                db.execute(
                    "INSERT INTO settings(id,data) VALUES(?,?)",
                    (1, self.seal(DEFAULT_CONFIG)),
                )

    @contextmanager
    def cx(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def seal(self, value):
        return self.cipher.encrypt(js(value).encode()).decode()

    def unseal(self, value):
        return json.loads(
            self.cipher.decrypt(value.encode()).decode()
        )

    def config(self):
        with self.cx() as db:
            row = db.execute(
                "SELECT data FROM settings WHERE id=1"
            ).fetchone()
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg.update(self.unseal(row["data"]))
        return cfg

    def save_config(self, cfg):
        validate_config(cfg)
        with self.cx() as db:
            db.execute(
                "UPDATE settings SET data=? WHERE id=1",
                (self.seal(cfg),),
            )

    def meta(self, key, default=""):
        with self.cx() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def user(self, uid, name=""):
        uid = str(uid)
        with self.cx() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO users
                (uid,name,status,role,allowed,notified,created)
                VALUES(?,?,?,?,?,?,?)
                """,
                (uid, name or uid, "pending", "author", "[]", 0, now()),
            )
            if name:
                db.execute(
                    "UPDATE users SET name=? WHERE uid=?",
                    (name, uid),
                )
            return dict(
                db.execute(
                    "SELECT * FROM users WHERE uid=?", (uid,)
                ).fetchone()
            )

    def users(self):
        with self.cx() as db:
            return [
                dict(row) for row in db.execute(
                    "SELECT * FROM users ORDER BY created DESC"
                )
            ]

    def access(self, uid, status, role, allowed):
        with self.cx() as db:
            db.execute(
                """
                UPDATE users SET status=?,role=?,allowed=?
                WHERE uid=?
                """,
                (status, role, js(allowed), str(uid)),
            )

    def draft(self, uid):
        with self.cx() as db:
            row = db.execute(
                "SELECT data FROM drafts WHERE uid=?",
                (str(uid),),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def save_draft(self, draft):
        with self.cx() as db:
            db.execute(
                """
                INSERT INTO drafts(uid,data) VALUES(?,?)
                ON CONFLICT(uid) DO UPDATE SET data=excluded.data
                """,
                (str(draft["uid"]), js(draft)),
            )

    def queue_job(self, draft, body, route, due):
        data = {
            "body": body,
            "media": copy.deepcopy(draft["media"]),
            "route": copy.deepcopy(route),
            "selected": list(draft["selected"]),
        }

        with self.cx() as db:
            existing = db.execute(
                "SELECT id FROM jobs WHERE id=?",
                (draft["id"],),
            ).fetchone()

            if existing:
                return False

            db.execute(
                """
                INSERT INTO jobs
                (id,uid,category,data,due,created)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    draft["id"], draft["uid"], draft["cat"],
                    self.seal(data), due, now(),
                ),
            )

            for platform in draft["selected"]:
                indices = [-1]
                if platform == "Rutube":
                    indices = [
                        i for i, item in enumerate(draft["media"])
                        if item["type"] == "video"
                    ]

                for index in indices:
                    target = (
                        platform if index == -1
                        else f"{platform}:{index}"
                    )
                    db.execute(
                        """
                        INSERT INTO deliveries
                        (job_id,target,platform,media_index,
                         status,result,updated)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            draft["id"], target, platform,
                            index, "pending", "", now(),
                        ),
                    )
        return True

    def deliveries(self, job_id):
        with self.cx() as db:
            return [
                dict(row) for row in db.execute(
                    """
                    SELECT * FROM deliveries
                    WHERE job_id=? ORDER BY target
                    """,
                    (job_id,),
                )
            ]

    def set_delivery(self, job_id, target, status, result):
        with self.cx() as db:
            db.execute(
                """
                UPDATE deliveries
                SET status=?,result=?,updated=?
                WHERE job_id=? AND target=?
                """,
                (status, str(result), now(), job_id, target),
            )

    def retry_failed(self, job_id):
        # Неопределённые отправки автоматически не повторяем.
        with self.cx() as db:
            db.execute(
                """
                UPDATE deliveries
                SET status='pending',result='',updated=?
                WHERE job_id=? AND status='failed'
                """,
                (now(), job_id),
            )

    def recover(self):
        with self.cx() as db:
            db.execute(
                """
                UPDATE deliveries
                SET status='unknown',
                    result='Приложение остановилось во время отправки',
                    updated=?
                WHERE status='sending'
                """,
                (now(),),
            )


# ---------------------------------------------------------------------------
# HTTP, MAX и работа с файлами
# ---------------------------------------------------------------------------

class Network:
    def __init__(self, store):
        self.store = store

    def clean(self, text):
        text = str(text)
        cfg = self.store.config()

        secrets = [
            cfg.get("receive_token", ""),
            cfg.get("ai_token", ""),
        ]
        for route in cfg["categories"].values():
            secrets += [
                route.get("vk_token", ""),
                route.get("max_token", ""),
                route.get("rutube_token", ""),
            ]

        for secret in secrets:
            if secret:
                text = text.replace(secret, "***")

        return re.sub(
            r"(?i)(access_token|api_key|authorization|signature)"
            r"([=:\s]+)[^\s&,\"<>]+",
            r"\1\2***",
            text,
        )[:3500]

    def request(
        self, method, url, *,
        raw=False, timeout=(15, 180), **kwargs
    ):
        validate_url(url)
        cfg = self.store.config()
        verify = cfg.get("ca_bundle", "").strip() or True

        kwargs.setdefault("allow_redirects", False)

        try:
            response = requests.request(
                method, url,
                verify=verify,
                timeout=timeout,
                **kwargs,
            )
        except requests.exceptions.SSLError:
            raise Rejected(
                "Не пройдена проверка TLS-сертификата. "
                "Проверьте доверенные сертификаты Windows, "
                "прокси/антивирус или настройте CA bundle PEM."
            ) from None
        except requests.exceptions.RequestException as exc:
            raise Uncertain(
                "Сетевая ошибка/тайм-аут: "
                + type(exc).__name__
                + ". Результат запроса может быть неизвестен."
            ) from None

        if response.status_code >= 500 or response.status_code == 408:
            raise Uncertain(
                f"HTTP {response.status_code}: "
                + self.clean(response.text[:2000])
            )

        if not response.ok:
            raise Rejected(
                f"HTTP {response.status_code}: "
                + self.clean(response.text[:2500])
            )

        if raw:
            return response

        if 300 <= response.status_code < 400:
            raise Rejected(
                "Сервис вернул перенаправление. "
                "Укажите конечный HTTPS-адрес API."
            )

        try:
            return response.json()
        except ValueError:
            raise Uncertain(
                "API вернул не JSON: "
                + self.clean(response.text[:1000])
            ) from None

    def multipart(self, url, fields, headers=None):
        encoder = MultipartEncoder(fields=fields)
        headers = dict(headers or {})
        headers["Content-Type"] = encoder.content_type

        return self.request(
            "POST", url,
            data=encoder,
            headers=headers,
            timeout=(20, 900),
        )

    def download(self, url, path, limit):
        # Не отправляем токены на URL медиа.
        for _ in range(6):
            parsed = validate_url(url)

            try:
                addresses = socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or 443,
                    type=socket.SOCK_STREAM,
                )
            except OSError:
                raise Rejected(
                    "Не удалось определить адрес сервера медиа."
                ) from None

            if not addresses or any(
                not ipaddress.ip_address(item[4][0]).is_global
                for item in addresses
            ):
                raise Rejected(
                    "Скачивание с локального/служебного адреса запрещено."
                )

            response = self.request(
                "GET", url,
                raw=True,
                stream=True,
                timeout=(15, 180),
            )

            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise Rejected("Пустое перенаправление медиа.")
                url = urljoin(url, location)
                continue

            try:
                length = int(
                    response.headers.get("Content-Length", "0") or 0
                )
                if length > limit:
                    raise Rejected("Медиафайл слишком большой.")

                total = 0
                with Path(path).open("wb") as file:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > limit:
                            raise Rejected("Медиафайл слишком большой.")
                        file.write(chunk)

                if not total:
                    raise Rejected("Получен пустой медиафайл.")
                return

            except Exception:
                Path(path).unlink(missing_ok=True)
                raise
            finally:
                response.close()

        raise Rejected("Слишком много перенаправлений медиа.")


class MaxAPI:
    def __init__(self, store, token=None):
        self.store = store
        self.http = Network(store)
        self.token = token

    def call(self, method, path, **kwargs):
        cfg = self.store.config()
        token = self.token or cfg["receive_token"]
        if not token:
            raise Rejected("Не указан токен MAX.")

        data = self.http.request(
            method,
            cfg["max_base"].rstrip("/") + path,
            headers={"Authorization": token},
            **kwargs,
        )

        if not isinstance(data, dict):
            raise Rejected("MAX вернул неожиданный формат ответа.")

        if data.get("success") is False:
            raise Rejected(
                self.http.clean(
                    data.get("message") or js(data)
                ),
                data.get("code", ""),
            )
        return data

    def send(self, target, text, attachments=None):
        result = self.call(
            "POST", "/messages",
            params=target,
            json={
                "text": text or None,
                "attachments": attachments or [],
            },
        )

        mid = (
            (result.get("message") or {}).get("body") or {}
        ).get("mid")

        if not mid:
            raise Uncertain(
                "MAX не подтвердил ID отправленного сообщения."
            )
        return str(mid)

    def edit(self, mid, text, rows):
        return self.call(
            "PUT", "/messages",
            params={"message_id": mid},
            json={
                "text": text,
                "attachments": [{
                    "type": "inline_keyboard",
                    "payload": {"buttons": rows},
                }],
            },
        )

    def upload(self, media):
        kind = media["type"]
        path = Path(media["path"])
        if not path.is_file():
            raise Rejected(f"Не найден файл: {path.name}")

        slot = self.call(
            "POST", "/uploads",
            params={"type": kind},
        )
        if not slot.get("url"):
            raise Rejected("MAX не вернул URL загрузки.")

        with path.open("rb") as file:
            uploaded = self.http.multipart(
                slot["url"],
                {
                    "data": (
                        path.name, file, media["mime"]
                    )
                },
            )

        if not isinstance(uploaded, dict):
            raise Rejected("Неожиданный ответ загрузчика MAX.")

        if kind == "image":
            if uploaded.get("photos"):
                return {
                    "type": "image",
                    "payload": {"photos": uploaded["photos"]},
                }
            token = uploaded.get("token") or slot.get("token")
            if token:
                return {
                    "type": "image",
                    "payload": {"token": token},
                }
        else:
            token = uploaded.get("token") or slot.get("token")
            if token:
                return {
                    "type": kind,
                    "payload": {"token": token},
                }

        raise Rejected(
            "MAX не вернул токен загруженного вложения."
        )

    def send_media(self, target, text, media):
        attachments = [self.upload(item) for item in media]

        # Повторяем только явно отклонённое действие из-за обработки файла.
        # Сетевые тайм-ауты автоматически не повторяются.
        for attempt in range(6):
            try:
                return self.send(target, text, attachments)
            except Rejected as exc:
                if "attachment.not.ready" not in str(exc):
                    raise
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 16))


# ---------------------------------------------------------------------------
# Публикация VK / MAX / Rutube
# ---------------------------------------------------------------------------

class Publisher:
    def __init__(self, store):
        self.store = store
        self.http = Network(store)

    def vk_call(self, method, token, **params):
        data = self.http.request(
            "POST",
            "https://api.vk.com/method/" + method,
            data={
                **params,
                "access_token": token,
                "v": "5.199",
            },
        )
        if data.get("error"):
            raise Rejected(
                "VK: " + self.http.clean(js(data["error"]))
            )
        if "response" not in data:
            raise Uncertain("VK не вернул response.")
        return data["response"]

    def vk(self, job, payload):
        route = payload["route"]
        token = route["vk_token"]
        group = abs(int(route["vk_group"]))
        attachments = []

        for media in payload["media"]:
            path = Path(media["path"])
            if not path.is_file():
                raise Rejected(f"Не найден файл {path.name}")

            if media["type"] == "image":
                slot = self.vk_call(
                    "photos.getWallUploadServer",
                    token,
                    group_id=group,
                )

                with path.open("rb") as file:
                    uploaded = self.http.multipart(
                        slot["upload_url"],
                        {
                            "photo": (
                                path.name, file, media["mime"]
                            )
                        },
                    )

                saved = self.vk_call(
                    "photos.saveWallPhoto",
                    token,
                    group_id=group,
                    photo=uploaded["photo"],
                    server=uploaded["server"],
                    hash=uploaded["hash"],
                )

                for photo in saved:
                    attachments.append(
                        f"photo{photo['owner_id']}_{photo['id']}"
                    )
            else:
                slot = self.vk_call(
                    "video.save",
                    token,
                    group_id=group,
                    name=(
                        payload["body"].split("\n")[0] or "Видео"
                    )[:100],
                    wallpost=0,
                )

                with path.open("rb") as file:
                    uploaded = self.http.multipart(
                        slot["upload_url"],
                        {
                            "video_file": (
                                path.name, file, media["mime"]
                            )
                        },
                    )

                if uploaded.get("error"):
                    raise Rejected(
                        "VK отклонил загрузку видео: "
                        + self.http.clean(js(uploaded))
                    )

                owner = slot.get("owner_id")
                video_id = slot.get("video_id")
                if owner is None or video_id is None:
                    raise Uncertain(
                        "VK не подтвердил ID загруженного видео."
                    )
                attachments.append(f"video{owner}_{video_id}")

        guid = int(
            hashlib.sha256(job["id"].encode()).hexdigest()[:8], 16
        ) % 2147483647

        result = self.vk_call(
            "wall.post",
            token,
            owner_id=-group,
            from_group=1,
            message=payload["body"],
            attachments=",".join(attachments),
            guid=guid,
        )

        if not isinstance(result, dict) or not result.get("post_id"):
            raise Uncertain("VK не подтвердил post_id.")

        return f"https://vk.com/wall-{group}_{result['post_id']}"

    def max(self, job, payload):
        route = payload["route"]
        api = MaxAPI(self.store, route["max_token"])
        mid = api.send_media(
            {"chat_id": int(route["max_chat"])},
            payload["body"],
            payload["media"],
        )
        return "MAX message_id=" + mid

    def rutube(self, job, payload, delivery):
        """
        Контракт пользовательского HTTPS-шлюза:
        POST multipart/form-data:
            channel_id, title, description, file, idempotency_key

        Успешный JSON:
            {
                "status": "published",
                "video_id": "...",
                "url": "https://rutube.ru/video/..."
            }

        Ответ "accepted" не считается подтверждением публикации.
        """
        route = payload["route"]
        media = payload["media"][delivery["media_index"]]
        path = Path(media["path"])
        key = job["id"] + ":" + delivery["target"]

        with path.open("rb") as file:
            result = self.http.multipart(
                route["rutube_url"],
                {
                    "channel_id": route["rutube_channel"],
                    "title": (
                        payload["body"].split("\n")[0] or "Видео"
                    )[:100],
                    "description": payload["body"],
                    "idempotency_key": key,
                    "file": (
                        path.name, file, media["mime"]
                    ),
                },
                headers={
                    "Authorization":
                        "Bearer " + route["rutube_token"],
                    "Idempotency-Key": key,
                },
            )

        if result.get("status") == "failed":
            raise Rejected(
                "Rutube: " + self.http.clean(js(result))
            )

        video_id = result.get("video_id") or result.get("id")
        if result.get("status") != "published" or not video_id:
            raise Uncertain(
                "Шлюз не подтвердил публикацию Rutube: "
                + self.http.clean(js(result))
            )

        return str(result.get("url") or video_id)

    def publish(self, job, delivery):
        payload = self.store.unseal(job["data"])
        platform = delivery["platform"]

        if platform == "VK":
            return self.vk(job, payload)
        if platform == "MAX":
            return self.max(job, payload)
        if platform == "Rutube":
            return self.rutube(job, payload, delivery)

        raise Rejected("Неизвестная площадка.")


# ---------------------------------------------------------------------------
# Бот
# ---------------------------------------------------------------------------

class Bot:
    def __init__(self, store, events):
        self.store = store
        self.events = events
        self.api = MaxAPI(store)
        self.http = Network(store)
        self.publisher = Publisher(store)

        self.stop_event = threading.Event()
        self.threads = []
        self.draft_lock = threading.RLock()
        self.online = False
        self.store.recover()

    def log(self, text):
        text = self.http.clean(text)
        logging.info(text)
        self.events.put(("log", text))

    def alive(self):
        return any(thread.is_alive() for thread in self.threads)

    def start(self):
        if self.alive():
            raise RuntimeError(
                "Бот уже работает либо ещё останавливается."
            )

        cfg = self.store.config()
        validate_config(cfg)
        if not cfg["receive_token"].strip():
            raise ValueError("Укажите токен принимающего MAX-бота.")

        self.stop_event.clear()
        self.threads = [
            threading.Thread(
                target=self.poll_loop, daemon=True
            ),
            threading.Thread(
                target=self.publish_loop, daemon=True
            ),
        ]
        for thread in self.threads:
            thread.start()
        self.log("Бот запущен.")

    def stop(self):
        self.stop_event.set()
        self.log(
            "Запрошена остановка. Текущий сетевой запрос "
            "может завершаться некоторое время."
        )

    def user_access(self, uid, name=""):
        cfg = self.store.config()
        user = self.store.user(uid, name)
        admin_ids = {
            part.strip()
            for part in cfg["admin_ids"].split(",")
            if part.strip()
        }

        if str(uid) in admin_ids:
            self.store.access(
                uid, "approved", "admin", CATEGORIES
            )
            user = self.store.user(uid)

        return user

    def allowed_categories(self, user):
        if user["role"] == "admin":
            return list(CATEGORIES)
        allowed = json.loads(user["allowed"])
        return [name for name in CATEGORIES if name in allowed]

    def assert_access(self, uid, category):
        user = self.user_access(uid)
        if user["status"] != "approved":
            raise Rejected("Доступ пользователя отозван.")
        if category not in self.allowed_categories(user):
            raise Rejected("Нет доступа к этой категории.")

    def poll_loop(self):
        while not self.stop_event.is_set():
            try:
                cfg = self.store.config()
                token_id = hashlib.sha256(
                    cfg["receive_token"].encode()
                ).hexdigest()[:24]
                marker_key = "marker:" + token_id

                params = {
                    "timeout": 5,
                    "limit": 100,
                    "types":
                        "message_created,message_callback,bot_started",
                }
                marker = self.store.meta(marker_key)
                if marker:
                    params["marker"] = marker

                result = self.api.call(
                    "GET", "/updates",
                    params=params,
                    timeout=(15, 20),
                )
                self.online = True

                # События и следующий marker сохраняются одной транзакцией.
                with self.store.cx() as db:
                    for event in result.get("updates", []):
                        raw = js(event)
                        event_id = hashlib.sha256(
                            (token_id + raw).encode()
                        ).hexdigest()

                        db.execute(
                            """
                            INSERT OR IGNORE INTO inbox
                            (id,bot,data,status,created)
                            VALUES(?,?,?,?,?)
                            """,
                            (
                                event_id, token_id, raw,
                                "pending", now(),
                            ),
                        )

                    next_marker = result.get("marker")
                    if next_marker is not None:
                        db.execute(
                            """
                            INSERT INTO meta(key,value) VALUES(?,?)
                            ON CONFLICT(key)
                            DO UPDATE SET value=excluded.value
                            """,
                            (marker_key, str(next_marker)),
                        )

                with self.store.cx() as db:
                    pending = [
                        dict(row) for row in db.execute(
                            """
                            SELECT * FROM inbox
                            WHERE bot=? AND status='pending'
                            ORDER BY created LIMIT 100
                            """,
                            (token_id,),
                        )
                    ]

                for row in pending:
                    if self.stop_event.is_set():
                        break

                    try:
                        self.receive(json.loads(row["data"]))
                        status = "done"
                    except Exception as exc:
                        self.log(
                            "Входящее событие: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        status = "error"

                    with self.store.cx() as db:
                        db.execute(
                            "UPDATE inbox SET status=? WHERE id=?",
                            (status, row["id"]),
                        )

            except Exception as exc:
                self.online = False
                self.log(f"Опрос MAX: {exc}")
                self.stop_event.wait(5)

        self.online = False

    def parse(self, event):
        kind = event.get("update_type", "")
        callback = event.get("callback") or {}
        message = event.get("message") or {}

        if kind == "bot_started":
            sender = event.get("user") or {}
            uid = str(sender.get("user_id", ""))
            chat = event.get("chat_id")
            target = (
                {"chat_id": int(chat)}
                if chat is not None
                else {"user_id": int(uid)}
            )
            return kind, uid, sender, target, message, callback

        sender = (
            callback.get("user")
            if callback
            else message.get("sender")
        ) or {}

        uid = str(sender.get("user_id", ""))
        recipient = message.get("recipient") or {}
        chat = recipient.get("chat_id")

        target = (
            {"chat_id": int(chat)}
            if chat is not None
            else {"user_id": int(uid)} if uid else {}
        )

        return kind, uid, sender, target, message, callback

    def new_draft(self, uid, target, old=None):
        user = self.user_access(uid)
        cats = self.allowed_categories(user)
        if not cats:
            raise Rejected(
                "Вам не назначены доступные категории."
            )

        draft = {
            "id": uuid.uuid4().hex,
            "uid": str(uid),
            "target": target,
            "mid": (old or {}).get("mid", ""),
            "body": "",
            "media": [],
            "cat": cats[0],
            "selected": [],
            "due": None,
            "checked": "",
            "mode": "",
            "job": "",
            "note": "",
        }
        draft["selected"] = self.available(draft)
        return draft

    def route(self, draft):
        return self.store.config()["categories"][draft["cat"]]

    def available(self, draft):
        route = self.route(draft)
        result = []

        if route["vk_group"] and route["vk_token"]:
            result.append("VK")

        if route["max_chat"] and route["max_token"]:
            result.append("MAX")

        has_video = any(
            item["type"] == "video"
            for item in draft["media"]
        )
        if (
            has_video
            and route["rutube_channel"]
            and route["rutube_token"]
            and route["rutube_url"]
        ):
            result.append("Rutube")

        return result

    def compiled(self, draft):
        text = draft["body"].strip()
        links = [
            str(link).strip()
            for link in self.route(draft)["links"]
            if str(link).strip()
        ]
        if links:
            text = "\n\n".join(
                part for part in [text, "\n".join(links)] if part
            )
        return text

    def fingerprint(self, draft):
        data = {
            "body": draft["body"],
            "media": draft["media"],
            "cat": draft["cat"],
            "selected": sorted(draft["selected"]),
            "route": self.route(draft),
        }
        return hashlib.sha256(
            json.dumps(
                data, sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest()

    def validate_draft(self, draft):
        self.assert_access(draft["uid"], draft["cat"])

        if not draft["body"].strip() and not draft["media"]:
            raise Rejected("Пост пустой.")

        available = set(self.available(draft))
        selected = set(draft["selected"])
        if not selected:
            raise Rejected("Не выбраны площадки.")

        if not selected <= available:
            raise Rejected(
                "Настройки выбранных площадок неполные "
                "или Rutube выбран без видео."
            )

        if len(draft["media"]) > 10:
            raise Rejected(
                "В этой версии допускается не более 10 вложений."
            )

        # Предпросмотр отправляется одним сообщением MAX.
        if len(self.compiled(draft)) > 4000:
            raise Rejected(
                "Текст со ссылками длиннее 4000 символов. "
                "Сократите его для предпросмотра в MAX."
            )

    def job_summary(self, job_id):
        rows = self.store.deliveries(job_id)
        if not rows:
            return ""

        labels = {
            "pending": "в очереди",
            "sending": "отправляется",
            "ok": "опубликовано",
            "failed": "ошибка",
            "unknown": "результат неизвестен",
        }

        return "\n".join(
            f"{row['target']}: "
            f"{labels.get(row['status'], row['status'])}"
            + (
                "\n" + str(row["result"])[:280]
                if row["result"] else ""
            )
            for row in rows
        )

    def panel(self, draft):
        self.store.save_draft(draft)
        cfg = self.store.config()
        user = self.user_access(draft["uid"])
        cats = self.allowed_categories(user)

        def btn(text, action, value=""):
            return {
                "type": "callback",
                "text": text,
                "payload": f"{draft['id']}|{action}|{value}",
            }

        selected = set(draft["selected"])
        available = self.available(draft)
        rows = []

        if not draft["job"]:
            if available:
                rows.append([
                    btn(
                        ("☑ " if p in selected else "☐ ") + p,
                        "platform", p,
                    )
                    for p in available
                ])

            for i in range(0, len(cats), 2):
                rows.append([
                    btn(
                        ("● " if cat == draft["cat"] else "○ ")
                        + cat,
                        "category",
                        str(CATEGORIES.index(cat)),
                    )
                    for cat in cats[i:i + 2]
                ])

            rows.extend([
                [
                    btn("Проверить пост", "check"),
                    btn("Опубликовать", "publish"),
                ],
                [
                    btn("Время публикации", "time"),
                    btn("В очередь", "schedule"),
                ],
                [
                    btn("Генерировать текст", "ai"),
                    btn("Заменить текст", "text"),
                ],
            ])
        else:
            rows.append([
                btn("Обновить статус", "refresh"),
                btn("Повторить ошибки", "retry"),
            ])

        rows.append([btn("Новый пост", "new")])

        photos = sum(
            item["type"] == "image" for item in draft["media"]
        )
        videos = sum(
            item["type"] == "video" for item in draft["media"]
        )

        checked = (
            draft["checked"] == self.fingerprint(draft)
        )

        text = (
            f"Категория: {draft['cat']}\n"
            f"Площадки: {', '.join(draft['selected']) or 'не выбраны'}\n"
            f"Фото: {photos}; видео: {videos}\n"
            f"Время: {date_text(draft['due'], cfg['timezone'])} "
            f"({cfg['timezone']})\n"
            f"Проверен: {'да' if checked else 'нет'}\n\n"
            f"{draft['body'][:700] or 'Текст пока отсутствует.'}"
        )

        if draft["job"]:
            text += "\n\n" + self.job_summary(draft["job"])

        if draft.get("note"):
            text += "\n\n" + draft["note"][:1000]

        text += (
            "\n\nФото и видео можно присылать несколькими сообщениями."
            "\n/new — новый пост; /ai задание — генерация."
        )

        if draft["mid"]:
            # При ошибке редактирования не создаём новую панель молча.
            self.api.edit(draft["mid"], text[:4000], rows)
        else:
            mid = self.api.send(
                draft["target"],
                text[:4000],
                [{
                    "type": "inline_keyboard",
                    "payload": {"buttons": rows},
                }],
            )
            draft["mid"] = mid
            self.store.save_draft(draft)

    def ingest_media(self, message):
        body = message.get("body") or {}
        attachments = (
            body.get("attachments")
            or message.get("attachments")
            or []
        )
        result = []

        for attachment in attachments:
            kind = attachment.get("type")
            if kind not in ("image", "video"):
                continue

            payload = attachment.get("payload") or {}
            url = payload.get("url") or payload.get("download_url")

            if kind == "video" and not url:
                token = payload.get("token")
                if not token:
                    raise Rejected("У входящего видео нет токена.")

                info = self.api.call(
                    "GET", "/videos/" + quote(token, safe="")
                )
                urls = info.get("urls") or {}
                url = next(
                    (
                        urls.get(key)
                        for key in (
                            "mp4_1080", "mp4_720", "mp4_480",
                            "mp4_360", "mp4_240", "mp4_144",
                        )
                        if urls.get(key)
                    ),
                    None,
                )

            if not url:
                raise Rejected(
                    f"MAX не предоставил URL для {kind}."
                )

            extension = Path(urlparse(url).path).suffix.lower()
            allowed_extensions = {
                "image": {".jpg", ".jpeg", ".png", ".gif",
                          ".webp", ".bmp", ".tiff", ".heic"},
                "video": {".mp4", ".mov", ".mkv", ".webm"},
            }
            if extension not in allowed_extensions[kind]:
                extension = ".jpg" if kind == "image" else ".mp4"

            path = MEDIA_DIR / (uuid.uuid4().hex + extension)
            limit = (
                50 * 1024 * 1024
                if kind == "image"
                else 250 * 1024 * 1024
            )
            self.http.download(url, path, limit)

            result.append({
                "type": kind,
                "path": str(path),
                "mime": (
                    mimetypes.guess_type(path.name)[0]
                    or (
                        "image/jpeg"
                        if kind == "image"
                        else "video/mp4"
                    )
                ),
            })

        return result

    def generate(self, draft, instruction):
        cfg = self.store.config()
        if not cfg["ai_token"].strip():
            raise Rejected("Не указан API-ключ генерации.")
        if not cfg["ai_model"].strip():
            raise Rejected("Не указан точный ID модели генерации.")

        data = self.http.request(
            "POST", cfg["ai_url"],
            headers={
                "Authorization": "Bearer " + cfg["ai_token"],
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "model": cfg["ai_model"].strip(),
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Ты редактор публикаций для социальных сетей. "
                            "Не выдумывай факты. "
                            "Верни только готовый текст публикации."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Категория: {draft['cat']}\n"
                            f"Задание: {instruction}\n\n"
                            f"Текущий текст:\n{draft['body']}"
                        ),
                    },
                ],
            },
            timeout=(20, 180),
        )

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise Rejected(
                "Неожиданный ответ генерации: "
                + self.http.clean(js(data))
            ) from None

        if not isinstance(text, str) or not text.strip():
            raise Rejected("API вернул пустой текст.")

        draft["body"] = text.strip()
        draft["checked"] = ""
        draft["mode"] = ""
        draft["note"] = "Текст сгенерирован. Проверьте факты."

    def receive(self, event):
        (
            kind, uid, sender, target,
            message, callback
        ) = self.parse(event)

        if not uid or sender.get("is_bot"):
            return

        recipient = message.get("recipient") or {}
        chat_type = recipient.get("chat_type")
        if chat_type and chat_type != "dialog":
            # Не создаём заявки из сообщений целевых групп.
            return

        name = (
            sender.get("name")
            or " ".join(
                str(sender.get(key) or "")
                for key in ("first_name", "last_name")
            ).strip()
            or uid
        )

        user = self.user_access(uid, name)

        if user["status"] != "approved":
            if user["status"] == "pending" and not user["notified"]:
                self.api.send(
                    target,
                    "Ваша заявка отправлена на рассмотрение, ожидайте.",
                )
                with self.store.cx() as db:
                    db.execute(
                        "UPDATE users SET notified=1 WHERE uid=?",
                        (uid,),
                    )
            return

        if callback.get("callback_id"):
            try:
                self.api.call(
                    "POST", "/answers",
                    params={
                        "callback_id": callback["callback_id"]
                    },
                    json={"notification": "Принято"},
                )
            except Exception as exc:
                self.log(f"Ответ на callback: {exc}")

        with self.draft_lock:
            draft = self.store.draft(uid)

            try:
                if not draft:
                    draft = self.new_draft(uid, target)

                draft["target"] = target
                draft["note"] = ""

                if callback:
                    self.action(
                        draft, str(callback.get("payload") or "")
                    )
                    return

                body = message.get("body") or {}
                text = str(body.get("text") or "").strip()

                if kind == "bot_started" or text == "/start":
                    self.panel(draft)
                    return

                if text == "/new":
                    draft = self.new_draft(uid, target, draft)
                    self.panel(draft)
                    return

                if draft["job"]:
                    draft["note"] = (
                        "Этот пост уже передан на публикацию. "
                        "Для следующего нажмите «Новый пост»."
                    )
                    self.panel(draft)
                    return

                self.assert_access(uid, draft["cat"])

                mode = draft.get("mode", "")
                if mode == "time":
                    timezone = ZoneInfo(
                        self.store.config()["timezone"]
                    )
                    due = datetime.strptime(
                        text, "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=timezone).timestamp()

                    if due <= now():
                        raise Rejected(
                            "Время должно находиться в будущем."
                        )

                    draft["due"] = due
                    draft["mode"] = ""
                    draft["note"] = (
                        "Время сохранено. После проверки "
                        "нажмите «В очередь»."
                    )

                elif mode == "ai" or text.startswith("/ai "):
                    instruction = (
                        text[4:] if text.startswith("/ai ")
                        else text
                    ).strip()
                    if not instruction:
                        raise Rejected("Введите задание генерации.")
                    self.generate(draft, instruction)

                elif mode == "text":
                    if not text:
                        raise Rejected("Отправьте новый текст.")
                    draft["body"] = text
                    draft["checked"] = ""
                    draft["mode"] = ""

                else:
                    media = self.ingest_media(message)
                    if len(draft["media"]) + len(media) > 10:
                        raise Rejected(
                            "Можно добавить не более 10 вложений."
                        )

                    draft["media"].extend(media)
                    if text and not text.startswith("/"):
                        draft["body"] = "\n\n".join(
                            part for part in
                            [draft["body"], text] if part
                        )

                    draft["checked"] = ""
                    if not draft["selected"]:
                        draft["selected"] = self.available(draft)

                self.panel(draft)

            except Exception as exc:
                self.log(
                    f"Обработка пользователя {uid}: {exc}"
                )
                if draft:
                    draft["note"] = "Ошибка: " + self.http.clean(exc)
                    try:
                        self.panel(draft)
                    except Exception as panel_exc:
                        self.log(
                            f"Не удалось обновить панель: {panel_exc}"
                        )

    def action(self, draft, payload):
        parts = payload.split("|", 2)
        if len(parts) != 3:
            raise Rejected("Неизвестная команда кнопки.")

        draft_id, action, value = parts
        if draft_id != draft["id"]:
            raise Rejected(
                "Это кнопка старого поста. "
                "Используйте актуальную панель."
            )

        self.assert_access(draft["uid"], draft["cat"])

        if action == "new":
            draft = self.new_draft(
                draft["uid"], draft["target"], draft
            )
            self.panel(draft)
            return

        if action == "refresh":
            self.panel(draft)
            return

        if action == "retry":
            if not draft["job"]:
                raise Rejected("Нет задания публикации.")
            self.store.retry_failed(draft["job"])
            draft["note"] = (
                "Повторно поставлены только явно неудачные отправки. "
                "Отправки с неизвестным результатом не повторяются."
            )
            self.panel(draft)
            return

        if draft["job"]:
            raise Rejected(
                "Задание уже создано. Создайте новый пост."
            )

        if action == "platform":
            if value not in self.available(draft):
                raise Rejected(
                    "Площадка не настроена или недоступна."
                )

            selected = set(draft["selected"])
            if value in selected:
                selected.remove(value)
            else:
                selected.add(value)

            draft["selected"] = [
                p for p in PLATFORMS if p in selected
            ]
            draft["checked"] = ""

        elif action == "category":
            category = CATEGORIES[int(value)]
            self.assert_access(draft["uid"], category)
            draft["cat"] = category
            draft["selected"] = self.available(draft)
            draft["checked"] = ""

        elif action == "time":
            draft["mode"] = "time"
            draft["note"] = (
                "Отправьте дату и время:\n"
                "2026-11-17 15:30\n"
                "Часовой пояс указан в настройках."
            )

        elif action == "text":
            draft["mode"] = "text"
            draft["note"] = (
                "Отправьте новый текст. Он заменит текущий. "
                "Вложения сохранятся."
            )

        elif action == "ai":
            draft["mode"] = "ai"
            draft["note"] = (
                "Отправьте задание для генерации текста."
            )

        elif action == "check":
            self.validate_draft(draft)
            self.api.send_media(
                draft["target"],
                self.compiled(draft),
                draft["media"],
            )
            draft["checked"] = self.fingerprint(draft)
            draft["note"] = (
                "Предпросмотр отправлен отдельным сообщением. "
                "Панель управления остаётся прежней."
            )

        elif action in ("publish", "schedule"):
            self.validate_draft(draft)

            if draft["checked"] != self.fingerprint(draft):
                raise Rejected(
                    "Сначала нажмите «Проверить пост». "
                    "После изменения текста, категории, площадок "
                    "или настроек нужна повторная проверка."
                )

            due = now()
            if action == "schedule":
                if not draft["due"] or draft["due"] <= now():
                    raise Rejected(
                        "Сначала задайте будущее время публикации."
                    )
                due = draft["due"]

            self.store.queue_job(
                draft,
                self.compiled(draft),
                self.route(draft),
                due,
            )
            draft["job"] = draft["id"]
            draft["note"] = (
                "Публикация поставлена в очередь."
                if action == "schedule"
                else "Начата публикация на выбранные площадки."
            )

        else:
            raise Rejected("Неизвестное действие.")

        self.panel(draft)

    def publish_loop(self):
        while not self.stop_event.is_set():
            try:
                with self.store.cx() as db:
                    rows = [
                        dict(row) for row in db.execute(
                            """
                            SELECT j.id,j.uid,j.category,j.data,
                                   d.target,d.platform,d.media_index
                            FROM jobs j
                            JOIN deliveries d ON d.job_id=j.id
                            WHERE j.due<=?
                              AND d.status='pending'
                            ORDER BY j.due,j.created,d.target
                            LIMIT 20
                            """,
                            (now(),),
                        )
                    ]

                for item in rows:
                    if self.stop_event.is_set():
                        break

                    job_id = item["id"]
                    target = item["target"]

                    # Атомарно захватываем отправку.
                    with self.store.cx() as db:
                        changed = db.execute(
                            """
                            UPDATE deliveries
                            SET status='sending',updated=?
                            WHERE job_id=? AND target=?
                              AND status='pending'
                            """,
                            (now(), job_id, target),
                        ).rowcount

                    if not changed:
                        continue

                    try:
                        self.assert_access(
                            item["uid"], item["category"]
                        )
                        result = self.publisher.publish(item, item)
                        status = "ok"

                    except Rejected as exc:
                        result = self.http.clean(exc)
                        status = "failed"

                    except Exception as exc:
                        # Тайм-аут/сбой после отправки не доказывает,
                        # что публикации не было.
                        result = self.http.clean(exc)
                        status = "unknown"

                    self.store.set_delivery(
                        job_id, target, status, result
                    )
                    self.log(
                        f"Публикация {job_id[:8]} / "
                        f"{target}: {status}: {result}"
                    )

                    with self.draft_lock:
                        draft = self.store.draft(item["uid"])
                        if draft and draft.get("job") == job_id:
                            try:
                                draft["note"] = ""
                                self.panel(draft)
                            except Exception as exc:
                                self.log(
                                    f"Обновление результата: {exc}"
                                )

            except Exception as exc:
                self.log(f"Очередь публикаций: {exc}")

            self.stop_event.wait(2)


# ---------------------------------------------------------------------------
# Окно настроек
# ---------------------------------------------------------------------------

class SettingsWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.store = app.store
        self.cfg = copy.deepcopy(self.store.config())

        self.title("Настройки PostPult")
        self.geometry("850x760")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        common = ttk.Frame(notebook)
        category_tab = ttk.Frame(notebook)
        notebook.add(common, text="Общие")
        notebook.add(category_tab, text="Категории и каналы")

        self.variables = {}
        fields = [
            ("receive_token", "Токен принимающего MAX-бота"),
            ("admin_ids", "ID администраторов через запятую"),
            ("max_base", "MAX API URL"),
            ("ai_url", "URL генерации текста"),
            ("ai_token", "API-ключ генерации"),
            ("ai_model", "Точный ID модели"),
            ("timezone", "Часовой пояс"),
            ("ca_bundle", "CA bundle PEM, необязательно"),
        ]

        for row, (key, label) in enumerate(fields):
            ttk.Label(common, text=label).grid(
                row=row, column=0, sticky="w", padx=8, pady=8
            )
            var = tk.StringVar(value=self.cfg.get(key, ""))
            self.variables[key] = var
            ttk.Entry(
                common,
                textvariable=var,
                show="*" if "token" in key else "",
            ).grid(
                row=row, column=1, sticky="ew", padx=8, pady=8
            )

        common.columnconfigure(1, weight=1)

        ttk.Button(
            common,
            text="Выбрать CA bundle",
            command=self.choose_ca,
        ).grid(row=len(fields), column=1, sticky="w", padx=8)

        ttk.Label(
            common,
            text=(
                "Точный ID модели берётся из списка моделей вашего ключа.\n"
                "Пустой CA bundle означает системное хранилище доверия.\n"
                "Не отключайте проверку HTTPS."
            ),
            wraplength=700,
        ).grid(
            row=len(fields) + 1, column=0, columnspan=2,
            sticky="w", padx=8, pady=20,
        )

        self.category = tk.StringVar(value=CATEGORIES[0])
        selector = ttk.Combobox(
            category_tab,
            textvariable=self.category,
            values=CATEGORIES,
            state="readonly",
        )
        selector.pack(fill="x", padx=8, pady=8)
        selector.bind("<<ComboboxSelected>>", self.switch_category)

        form = ttk.Frame(category_tab)
        form.pack(fill="x", padx=8, pady=8)
        form.columnconfigure(1, weight=1)

        self.route_variables = {}
        route_fields = [
            ("vk_group", "Числовой ID группы VK"),
            ("vk_token", "Токен VK этой категории"),
            ("max_chat", "Числовой ID группы/канала MAX"),
            ("max_token", "Токен отправляющего MAX-бота"),
            ("rutube_channel", "ID канала Rutube"),
            ("rutube_token", "Токен Rutube-шлюза"),
            ("rutube_url", "HTTPS URL Rutube-шлюза"),
        ]

        for row, (key, label) in enumerate(route_fields):
            ttk.Label(form, text=label).grid(
                row=row, column=0, sticky="w", pady=6, padx=4
            )
            var = tk.StringVar()
            self.route_variables[key] = var
            ttk.Entry(
                form,
                textvariable=var,
                show="*" if "token" in key else "",
            ).grid(
                row=row, column=1, sticky="ew", pady=6, padx=4
            )

        ttk.Label(
            category_tab,
            text="Ссылки в конце каждого поста — по одной на строку:",
        ).pack(anchor="w", padx=12, pady=8)

        self.links = tk.Text(category_tab, height=8)
        self.links.pack(
            fill="both", expand=True, padx=12, pady=5
        )

        ttk.Label(
            category_tab,
            text=(
                "Неиспользуемые площадки оставьте пустыми. "
                "Кнопки показываются только для настроенных площадок.\n"
                "Rutube доступен только при наличии видео."
            ),
            wraplength=780,
        ).pack(anchor="w", padx=12, pady=8)

        self.current_category = CATEGORIES[0]
        self.load_category()

        ttk.Button(
            self,
            text="Сохранить настройки",
            command=self.save,
        ).pack(pady=10)

    def choose_ca(self):
        path = filedialog.askopenfilename(
            parent=self,
            title="CA bundle PEM",
            filetypes=[("PEM", "*.pem"), ("Все файлы", "*.*")],
        )
        if path:
            self.variables["ca_bundle"].set(path)

    def store_category(self):
        route = self.cfg["categories"][self.current_category]
        for key, var in self.route_variables.items():
            route[key] = var.get().strip()

        route["links"] = [
            line.strip()
            for line in self.links.get("1.0", "end").splitlines()
            if line.strip()
        ]

    def load_category(self):
        route = self.cfg["categories"][self.current_category]
        for key, var in self.route_variables.items():
            var.set(route.get(key, ""))

        self.links.delete("1.0", "end")
        self.links.insert("1.0", "\n".join(route.get("links", [])))

    def switch_category(self, _event=None):
        self.store_category()
        self.current_category = self.category.get()
        self.load_category()

    def save(self):
        try:
            self.store_category()
            for key, var in self.variables.items():
                self.cfg[key] = var.get().strip()

            self.store.save_config(self.cfg)
            messagebox.showinfo(
                "Настройки", "Настройки сохранены.", parent=self
            )
            self.destroy()

        except Exception as exc:
            messagebox.showerror(
                "Настройки", str(exc), parent=self
            )


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PostPult — MAX / VK / Rutube")
        self.geometry("1080x720")

        self.store = Store()
        self.events = queue.Queue()
        self.bot = Bot(self.store, self.events)

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=10)

        for title, command in [
            ("Настройки", lambda: SettingsWindow(self)),
            ("Запустить бота", self.start_bot),
            ("Остановить", self.bot.stop),
            ("Проверить MAX", self.check_max),
            ("Папка данных", self.show_data_folder),
        ]:
            ttk.Button(
                toolbar, text=title, command=command
            ).pack(side="left", padx=4)

        self.status = ttk.Label(self, text="")
        self.status.pack(anchor="w", padx=14)

        notebook = ttk.Notebook(self)
        notebook.pack(
            fill="both", expand=True, padx=10, pady=10
        )

        users_tab = ttk.Frame(notebook)
        jobs_tab = ttk.Frame(notebook)
        log_tab = ttk.Frame(notebook)

        notebook.add(users_tab, text="Пользователи и заявки")
        notebook.add(jobs_tab, text="Публикации")
        notebook.add(log_tab, text="Журнал")

        self.user_tree = ttk.Treeview(
            users_tab,
            columns=("uid", "name", "status", "role"),
            show="headings",
            selectmode="browse",
            height=12,
        )
        for key, label in [
            ("uid", "ID"),
            ("name", "Имя"),
            ("status", "Статус"),
            ("role", "Роль"),
        ]:
            self.user_tree.heading(key, text=label)
            self.user_tree.column(key, width=160)

        self.user_tree.pack(
            fill="both", expand=True, padx=8, pady=8
        )
        self.user_tree.bind(
            "<<TreeviewSelect>>", self.select_user
        )

        access_frame = ttk.Frame(users_tab)
        access_frame.pack(fill="x", padx=8, pady=8)

        ttk.Label(access_frame, text="Роль:").pack(side="left")
        self.role = tk.StringVar(value="author")
        ttk.Combobox(
            access_frame,
            textvariable=self.role,
            values=["author", "admin"],
            state="readonly",
            width=12,
        ).pack(side="left", padx=8)

        self.category_flags = {}
        for category in CATEGORIES:
            var = tk.BooleanVar(value=True)
            self.category_flags[category] = var
            ttk.Checkbutton(
                access_frame, text=category, variable=var
            ).pack(side="left", padx=3)

        buttons = ttk.Frame(users_tab)
        buttons.pack(fill="x", padx=8, pady=8)

        ttk.Button(
            buttons,
            text="Выдать / обновить доступ",
            command=lambda: self.change_access("approved"),
        ).pack(side="left", padx=4)

        ttk.Button(
            buttons,
            text="Игнорировать / отозвать доступ",
            command=lambda: self.change_access("ignored"),
        ).pack(side="left", padx=4)

        ttk.Button(
            buttons,
            text="Обновить список",
            command=self.refresh_users,
        ).pack(side="left", padx=4)

        self.job_tree = ttk.Treeview(
            jobs_tab,
            columns=("id", "uid", "category", "due", "status"),
            show="headings",
        )
        for key, label, width in [
            ("id", "Задание", 120),
            ("uid", "Автор", 120),
            ("category", "Категория", 160),
            ("due", "Время", 150),
            ("status", "Результаты", 450),
        ]:
            self.job_tree.heading(key, text=label)
            self.job_tree.column(key, width=width)

        self.job_tree.pack(
            fill="both", expand=True, padx=8, pady=8
        )
        self.job_tree.bind(
            "<Double-1>", self.job_details
        )

        job_buttons = ttk.Frame(jobs_tab)
        job_buttons.pack(fill="x", padx=8, pady=8)

        ttk.Button(
            job_buttons,
            text="Обновить",
            command=self.refresh_jobs,
        ).pack(side="left", padx=4)

        ttk.Button(
            job_buttons,
            text="Повторить только ошибки",
            command=self.retry_job,
        ).pack(side="left", padx=4)

        ttk.Button(
            job_buttons,
            text="Сбросить неизвестный результат вручную",
            command=self.reset_unknown,
        ).pack(side="left", padx=4)

        self.logbox = tk.Text(
            log_tab, state="disabled", wrap="word"
        )
        self.logbox.pack(fill="both", expand=True)

        self.refresh_users()
        self.refresh_jobs()
        self.after(250, self.tick)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def start_bot(self):
        try:
            self.bot.start()
        except Exception as exc:
            messagebox.showerror("Запуск", str(exc), parent=self)

    def check_max(self):
        def worker():
            try:
                data = MaxAPI(self.store).call("GET", "/me")
                name = data.get("name") or data.get("username") or ""
                self.events.put((
                    "info", "MAX",
                    "Связь установлена.\n" + str(name),
                ))
            except Exception as exc:
                self.events.put((
                    "error", "MAX",
                    self.bot.http.clean(exc),
                ))

        threading.Thread(target=worker, daemon=True).start()

    def show_data_folder(self):
        if os.name == "nt":
            os.startfile(str(HOME))
        else:
            messagebox.showinfo(
                "Папка данных", str(HOME), parent=self
            )

    def refresh_users(self):
        selected = self.user_tree.selection()
        old = selected[0] if selected else None

        for item in self.user_tree.get_children():
            self.user_tree.delete(item)

        for user in self.store.users():
            self.user_tree.insert(
                "",
                "end",
                iid=user["uid"],
                values=(
                    user["uid"], user["name"],
                    user["status"], user["role"],
                ),
            )

        if old and self.user_tree.exists(old):
            self.user_tree.selection_set(old)

    def select_user(self, _event=None):
        selection = self.user_tree.selection()
        if not selection:
            return

        user = self.store.user(selection[0])
        self.role.set(user["role"])
        allowed = json.loads(user["allowed"])
        for category, var in self.category_flags.items():
            var.set(
                category in allowed or user["role"] == "admin"
            )

    def change_access(self, status):
        selected = self.user_tree.selection()
        if not selected:
            messagebox.showwarning(
                "Доступ", "Выберите пользователя.", parent=self
            )
            return

        allowed = [
            name for name, var in self.category_flags.items()
            if var.get()
        ]
        role = self.role.get()

        if status == "approved" and role != "admin" and not allowed:
            messagebox.showwarning(
                "Доступ",
                "Выберите хотя бы одну категорию.",
                parent=self,
            )
            return

        self.store.access(
            selected[0], status, role, allowed
        )
        self.refresh_users()

    def refresh_jobs(self):
        cfg = self.store.config()
        selected = self.job_tree.selection()
        old = selected[0] if selected else None

        for item in self.job_tree.get_children():
            self.job_tree.delete(item)

        with self.store.cx() as db:
            jobs = [
                dict(row) for row in db.execute(
                    """
                    SELECT id,uid,category,due
                    FROM jobs ORDER BY created DESC LIMIT 500
                    """
                )
            ]

        for job in jobs:
            deliveries = self.store.deliveries(job["id"])
            summary = "; ".join(
                f"{row['target']}: {row['status']}"
                for row in deliveries
            )
            self.job_tree.insert(
                "", "end", iid=job["id"],
                values=(
                    job["id"][:8],
                    job["uid"],
                    job["category"],
                    date_text(job["due"], cfg["timezone"]),
                    summary,
                ),
            )

        if old and self.job_tree.exists(old):
            self.job_tree.selection_set(old)

    def job_details(self, _event=None):
        selected = self.job_tree.selection()
        if not selected:
            return
        messagebox.showinfo(
            "Результаты публикации",
            self.bot.job_summary(selected[0]),
            parent=self,
        )

    def retry_job(self):
        selected = self.job_tree.selection()
        if not selected:
            return
        self.store.retry_failed(selected[0])
        self.refresh_jobs()

    def reset_unknown(self):
        selected = self.job_tree.selection()
        if not selected:
            return

        confirmed = messagebox.askyesno(
            "Возможны дубликаты",
            "Сначала проверьте целевые группы и каналы вручную.\n\n"
            "Неизвестный результат не означает, что публикации нет.\n"
            "Повторная отправка может создать дубликат.\n\n"
            "Вы проверили площадки и хотите повторить отправку?",
            parent=self,
        )
        if not confirmed:
            return

        with self.store.cx() as db:
            db.execute(
                """
                UPDATE deliveries
                SET status='pending',result='',updated=?
                WHERE job_id=? AND status='unknown'
                """,
                (now(), selected[0]),
            )

        self.bot.log(
            "Оператор вручную разрешил повтор неизвестных "
            "отправок: " + selected[0]
        )
        self.refresh_jobs()

    def append_log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert(
            "end",
            datetime.now().strftime("%H:%M:%S")
            + " " + str(text) + "\n",
        )
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def tick(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self.append_log(event[1])
                elif event[0] == "info":
                    messagebox.showinfo(
                        event[1], event[2], parent=self
                    )
                elif event[0] == "error":
                    messagebox.showerror(
                        event[1], event[2], parent=self
                    )
        except queue.Empty:
            pass

        if self.bot.alive():
            state = (
                "останавливается"
                if self.bot.stop_event.is_set()
                else "работает"
            )
        else:
            state = "остановлен"

        connection = (
            "есть" if self.bot.online else "нет / не проверена"
        )

        pending = sum(
            user["status"] == "pending"
            for user in self.store.users()
        )
        self.status.configure(
            text=(
                f"Бот: {state}    Связь MAX: {connection}    "
                f"Новые заявки: {pending}"
            )
        )
        self.after(500, self.tick)

    def close(self):
        if self.bot.alive():
            if not messagebox.askyesno(
                "Выход",
                "Бот и отложенные публикации работают только "
                "при открытом приложении.\n\n"
                "Закрыть приложение?",
                parent=self,
            ):
                return

            self.bot.stop()

            def wait_for_stop():
                if self.bot.alive():
                    self.after(300, wait_for_stop)
                else:
                    self.destroy()

            wait_for_stop()
        else:
            self.destroy()


if __name__ == "__main__":
    try:
        App().mainloop()
    except Exception as exc:
        # Не печатаем конфигурацию или ключи.
        logging.error(
            "Критическая ошибка: %s: %s",
            type(exc).__name__,
            str(exc)[:2000],
        )
        raise