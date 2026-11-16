# max_bot.py
import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from maxapi import Bot, Dispatcher
from maxapi.types import Command, MessageCreated, MessageCallback, CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from database import (
    get_user, add_pending_user, list_categories, get_category,
    get_setting, list_targets,
)
from deepseek_generator import generate_post_text
from vk_publisher import publish_to_vk
from rutube_publisher import upload_video_to_rutube
from config import DOWNLOAD_DIR

bot: Bot = None
dp: Dispatcher = None
user_states: dict = {}

# --- Защита от самопрослушивания ---
BOT_USER_ID: int | None = None
_processed_mids: set = set()
_pending_last_reply: dict = {}
_ignored_chats_cache: set = set()


# ---------- Вспомогательные ----------
def _refresh_ignored_chats():
    global _ignored_chats_cache
    _ignored_chats_cache = {
        str(t["max_chat_id"]) for t in list_targets("max") if t["max_chat_id"]
    }


def _is_target_chat(chat_id: str) -> bool:
    if str(chat_id) in _ignored_chats_cache:
        return True
    for t in list_targets("max"):
        if t["max_chat_id"] and str(t["max_chat_id"]) == str(chat_id):
            _ignored_chats_cache.add(str(chat_id))
            return True
    return False


def _chat_type_str(msg) -> str:
    try:
        ct = msg.recipient.chat_type
        return getattr(ct, "value", str(ct)).lower()
    except (AttributeError, TypeError):
        return ""


def _should_ignore(event) -> bool:
    msg = event.message

    try:
        if BOT_USER_ID and msg.sender.user_id == BOT_USER_ID:
            return True
    except AttributeError:
        pass

    try:
        chat_id = str(msg.recipient.chat_id)
        if _is_target_chat(chat_id):
            return True
    except (AttributeError, TypeError):
        pass

    ct = _chat_type_str(msg)
    if ct in ("chat", "channel"):
        return True

    try:
        mid = msg.body.mid
        if mid:
            if mid in _processed_mids:
                return True
            _processed_mids.add(mid)
            if len(_processed_mids) > 5000:
                _processed_mids.clear()
    except AttributeError:
        pass

    return False


def _throttle_pending(uid: int, seconds: int = 60) -> bool:
    now = time.time()
    if now - _pending_last_reply.get(uid, 0) < seconds:
        return False
    _pending_last_reply[uid] = now
    return True


def _extract_text(msg) -> str:
    """
    Достаём текст из body.text, плюс забираем подписи к вложениям
    (payload.caption / payload.description) на случай,
    если MAX хранит подпись к фото/видео отдельно.
    Дубликаты отбрасываем.
    """
    parts: list[str] = []
    try:
        if msg.body.text:
            t = msg.body.text.strip()
            if t:
                parts.append(t)
    except AttributeError:
        pass

    try:
        attachments = msg.body.attachments or []
    except AttributeError:
        attachments = []

    for att in attachments:
        payload = getattr(att, "payload", None)
        if not payload:
            continue
        caption = (
            getattr(payload, "caption", None)
            or getattr(payload, "description", None)
        )
        if caption:
            c = caption.strip()
            if c and c not in parts:
                parts.append(c)

    return "\n\n".join(parts).strip()


async def _download(url: str, dest: Path) -> bool:
    def _sync():
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
            return True
        except Exception as e:
            print(f"[download] {e}")
            return False
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync)


async def _collect_media(event, uid: int) -> list:
    paths = []
    try:
        attachments = event.message.body.attachments or []
    except AttributeError:
        return paths

    for att in attachments:
        att_type = getattr(att, "type", "")
        payload = getattr(att, "payload", None)
        url = getattr(payload, "url", None) if payload else None
        if not url:
            continue
        if att_type == "image":
            ext = ".jpg"
        elif att_type == "video":
            ext = ".mp4"
        else:
            continue
        dest = DOWNLOAD_DIR / f"{uid}_{datetime.now().timestamp()+timedelta(hours=8).total_seconds()}{ext}"
        if await _download(url, dest):
            paths.append(str(dest))
    return paths


# ---------- Отправка в MAX ----------
async def _send_to_max(chat_id, text, media_paths=None):
    if not chat_id:
        return "❌ MAX: не задан chat_id"
    try:
        cid = chat_id
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            pass

        await bot.send_message(chat_id=cid, text=text)

        for p in (media_paths or []):
            try:
                with open(p, "rb") as f:
                    if hasattr(bot, "send_document"):
                        await bot.send_document(chat_id=cid, document=f)
            except Exception as e:
                print(f"[max media] {e}")
        return "✅ MAX: отправлено"
    except Exception as e:
        return f"❌ MAX: {e}"


# ---------- Публикация ----------
async def _publish_now(category_name, text_with_links, media_paths, selected_types):
    category = get_category(category_name)
    if not category:
        return ["❌ Категория не найдена"]

    loop = asyncio.get_event_loop()
    results = []

    for t in category["targets"]:
        if t["type"] not in selected_types:
            continue

        if t["type"] == "vk":
            r = await loop.run_in_executor(
                None, publish_to_vk,
                t["vk_token"], t["vk_group_id"], text_with_links, media_paths,
            )
        elif t["type"] == "max":
            r = await _send_to_max(t["max_chat_id"], text_with_links, media_paths)
        elif t["type"] == "rutube":
            videos = [p for p in (media_paths or [])
                      if p.lower().endswith((".mp4", ".mov", ".avi"))]
            if not videos:
                r = f"⚠️ Rutube [{t['name']}]: нет видео"
            else:
                r = await loop.run_in_executor(
                    None, upload_video_to_rutube,
                    t["rutube_token"], t["rutube_channel_id"], videos[0],
                    text_with_links[:100], text_with_links,
                )
        else:
            r = f"❌ Неизвестный тип: {t['type']}"

        results.append(f"[{t['type'].upper()}:{t['name']}] {r}")

    return results or ["⚠️ Не выбрано ни одного канала"]


# ---------- Клавиатуры ----------
def _category_keyboard():
    builder = InlineKeyboardBuilder()
    for cat in list_categories():
        builder.row(CallbackButton(text=cat["name"], payload=f"cat:{cat['name']}"))
    return builder.as_markup()


def _actions_keyboard(cat_name: str, selected: list):
    category = get_category(cat_name)
    if not category:
        return None
    types_in_cat = sorted({t["type"] for t in category["targets"]})
    if not types_in_cat:
        return None

    builder = InlineKeyboardBuilder()
    for tp in types_in_cat:
        mark = "☑" if tp in selected else "☐"
        builder.row(CallbackButton(text=f"{mark} {tp.upper()}", payload=f"chan:{tp}"))
    builder.row(
        CallbackButton(text="✅ Проверить пост", payload="action:preview"),
        CallbackButton(text="🚀 Опубликовать", payload="action:publish"),
    )
    builder.row(
        CallbackButton(text="🤖 DeepSeek", payload="action:ai"),
        CallbackButton(text="🆕 Новый пост", payload="action:reset"),
    )
    return builder.as_markup()


def _compose_text(state: dict) -> str:
    """
    Финальный текст поста по требованию:
        <текст пользователя>
        <пустая строка>
        <ссылки>
    Пустые строки внутри links сохраняются (пользователь мог
    настроить разделители между ссылками в редакторе категории).
    """
    base = (state.get("text") or "").strip()
    cat_name = state.get("category")
    category = get_category(cat_name) if cat_name else None
    links = category["links"] if category else []

    parts = []
    if base:
        parts.append(base)
    if links:
        parts.append("\n".join(links))
    return "\n\n".join(parts).strip()


# ---------- Старт ----------
async def start_bot(max_token):
    global bot, dp, BOT_USER_ID
    bot = Bot(token=max_token)
    dp = Dispatcher()

    try:
        me = await bot.get_me()
        BOT_USER_ID = getattr(me, "user_id", None) or getattr(me, "id", None)
        print(f"[max_bot] Bot user_id = {BOT_USER_ID}")
    except Exception as e:
        print(f"[max_bot] Could not fetch bot info: {e}")

    _refresh_ignored_chats()

    # ---------- /start ----------
    @dp.message_created(Command("start"))
    async def cmd_start(event: MessageCreated):
        if _should_ignore(event):
            return

        uid = event.message.sender.user_id
        username = getattr(event.message.sender, "username", "") or ""
        user = get_user(uid)

        if not user:
            add_pending_user(uid, username)
            await event.message.answer(
                "👋 Ваша заявка отправлена на рассмотрение, ожидайте."
            )
            return

        if user["role"] == "pending":
            await event.message.answer("⏳ Ваша заявка ещё на рассмотрении.")
            return

        if user["role"] not in ("author", "admin"):
            await event.message.answer("🚫 У вас нет прав.")
            return

        # Сброс — /start начинает новый пост
        user_states[uid] = {}
        await event.message.answer(
            "📝 Отправьте фото/видео и/или текст для поста.\n"
            "Можно отправить несколькими сообщениями — всё соберётся в один пост."
        )

    # ---------- Любое сообщение ----------
    @dp.message_created()
    async def handle_post(event: MessageCreated):
        if _should_ignore(event):
            return

        uid = event.message.sender.user_id
        user = get_user(uid)

        if not user:
            add_pending_user(uid, getattr(event.message.sender, "username", "") or "")
            await event.message.answer(
                "👋 Ваша заявка отправлена на рассмотрение, ожидайте."
            )
            return

        if user["role"] == "pending":
            if _throttle_pending(uid, 60):
                await event.message.answer(
                    "⏳ Ваша заявка ещё на рассмотрении. Пожалуйста, подождите."
                )
            return

        if user["role"] not in ("author", "admin"):
            await event.message.answer("🚫 У вас нет прав.")
            return

        # --- Накопители контента ---
        state = user_states.setdefault(uid, {})
        state.setdefault("text_parts", [])
        state.setdefault("media", [])

        # --- Новый текст (body.text + подписи к вложениям) ---
        new_text = _extract_text(event.message)
        if new_text:
            state["text_parts"].append(new_text)

        # --- Новые медиа ---
        new_media = await _collect_media(event, uid)
        state["media"].extend(new_media)

        # --- Итоговый текст = все блоки через пустую строку ---
        state["text"] = "\n\n".join(state["text_parts"]).strip()

        # Контент изменился — старый выбор категории/каналов сбрасываем
        state["category"] = None
        state["channels"] = []

        if not state["text"] and not state["media"]:
            await event.message.answer("📝 Отправьте текст и/или фото/видео.")
            return

        await event.message.answer(
            f"📎 Собрано для поста:\n"
            f"• Блоков текста: {len(state['text_parts'])}\n"
            f"• Медиа: {len(state['media'])} файл(ов)\n\n"
            f"Выберите категорию (или пришлите ещё контент):",
            attachments=[_category_keyboard()],
        )

    # ---------- Callback'и ----------
    @dp.message_callback()
    async def handle_callback(event: MessageCallback):
        uid = event.message.sender.user_id
        user = get_user(uid)

        if not user:
            add_pending_user(uid, "")
            await event.answer(new_text="👋 Ваша заявка отправлена на рассмотрение, ожидайте.")
            return

        if user["role"] == "pending":
            await event.answer(new_text="⏳ Заявка ещё на рассмотрении.")
            return

        if user["role"] not in ("author", "admin"):
            await event.answer(new_text="🚫 Нет прав.")
            return

        data = event.callback.payload or ""
        state = user_states.setdefault(uid, {})

        # --- Выбор категории ---
        if data.startswith("cat:"):
            cat_name = data.split(":", 1)[1]
            category = get_category(cat_name)
            if not category or not category["targets"]:
                await event.answer(
                    new_text=f"⚠️ В категории «{cat_name}» нет каналов. Настройте в GUI."
                )
                return

            state["category"] = cat_name
            state["channels"] = []

            await event.answer(
                new_text=f"Категория: {cat_name}\nВыберите каналы и действие:",
                attachments=[_actions_keyboard(cat_name, [])],
            )

        # --- Тоггл канала ---
        elif data.startswith("chan:"):
            tp = data.split(":", 1)[1]
            sel = state.setdefault("channels", [])
            if tp in sel:
                sel.remove(tp)
            else:
                sel.append(tp)

            cat_name = state.get("category")
            kb = _actions_keyboard(cat_name, sel) if cat_name else None
            await event.answer(
                new_text=f"Категория: {cat_name}\nВыбрано: {', '.join(sel) or '—'}",
                attachments=[kb] if kb else [],
            )

        # --- Предпросмотр ---
        elif data == "action:preview":
            if not state.get("category"):
                await event.answer(new_text="Сначала выберите категорию.")
                return
            text = _compose_text(state)
            await event.answer(new_text="📋 Предпросмотр отправлен ниже.")
            await event.message.answer(
                f"📋 **Предпросмотр**\n"
                f"Категория: {state['category']}\n"
                f"Каналы: {', '.join(state.get('channels', [])) or '—'}\n"
                f"Медиа: {len(state.get('media', []))}\n\n"
                f"——————\n{text}\n——————"
            )

        # --- Публикация ---
        elif data == "action:publish":
            if not state.get("category"):
                await event.answer(new_text="Сначала выберите категорию.")
                return
            if not state.get("channels"):
                await event.answer(new_text="Выберите хотя бы один канал.")
                return

            text = _compose_text(state)
            await event.answer(new_text="⏳ Публикую...")

            results = await _publish_now(
                state["category"], text, state.get("media", []), state["channels"]
            )
            await event.message.answer(
                "📤 **Результаты:**\n" + "\n".join(results)
            )

            # Сброс — следующий пост начнётся с чистого листа
            user_states[uid] = {}

        # --- DeepSeek ---
        elif data == "action:ai":
            topic = state.get("text", "")
            if not topic:
                await event.answer(new_text="Нет текста для генерации.")
                return
            await event.answer(new_text="⏳ Генерирую текст...")
            loop = asyncio.get_event_loop()
            gen = await loop.run_in_executor(None, generate_post_text, topic)
            # Добавляем сгенерированный текст как ещё один блок
            state.setdefault("text_parts", []).append(gen)
            state["text"] = "\n\n".join(state["text_parts"]).strip()
            await event.message.answer(f"🤖 Готово:\n\n{gen}")

        # --- Сброс ---
        elif data == "action:reset":
            user_states[uid] = {}
            await event.answer(
                new_text="🆕 Начните новый пост — просто отправьте текст/медиа."
            )

    await dp.start_polling(bot)


async def stop_bot():
    global bot, dp
    if dp:
        try:
            await dp.stop_polling()
        except Exception:
            pass
    bot = None
    dp = None