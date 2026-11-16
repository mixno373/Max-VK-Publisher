# max_bot.py
import asyncio
from datetime import datetime
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
user_states = {}


# ---------- Отправка в MAX ----------
async def _send_to_max(chat_id, text, media_paths=None):
    """Отправляет пост в конкретный MAX-чат. Один бот — много чатов."""
    if not chat_id:
        return "❌ MAX: не задан chat_id"
    try:
        if not media_paths:
            await bot.send_message(chat_id=chat_id, text=text)
        else:
            # Простейший вариант: шлём текст + файлы
            await bot.send_message(chat_id=chat_id, text=text)
            for p in media_paths:
                # maxapi может предоставлять send_photo/send_video — уточните по вашей версии
                try:
                    with open(p, "rb") as f:
                        await bot.send_document(chat_id=chat_id, document=f)
                except Exception:
                    pass
        return "✅ MAX: отправлено"
    except Exception as e:
        return f"❌ MAX: {e}"


# ---------- Публикация ----------
async def _publish_now(category_name, text_with_links, media_paths, selected_types):
    category = get_category(category_name)
    if not category:
        return ["❌ Категория не найдена"]

    results = []
    for t in category["targets"]:
        if t["type"] not in selected_types:
            continue

        if t["type"] == "vk":
            r = publish_to_vk(t["token"], t["group_id"], text_with_links, media_paths)
        elif t["type"] == "max":
            r = await _send_to_max(t["chat_id"], text_with_links, media_paths)
        elif t["type"] == "rutube":
            videos = [p for p in media_paths if p.lower().endswith((".mp4", ".mov", ".avi"))]
            if not videos:
                r = f"⚠️ Rutube [{t['name']}]: нет видео"
            else:
                r = upload_video_to_rutube(
                    t["token"], t["channel_id"], videos[0],
                    title=text_with_links[:100], description=text_with_links)
        else:
            r = f"❌ Неизвестный тип: {t['type']}"

        results.append(f"[{t['type'].upper()}:{t['name']}] {r}")
    return results or ["⚠️ Не выбрано ни одного канала"]


# ---------- Старт ----------
async def start_bot(max_token):
    global bot, dp
    bot = Bot(token=max_token)
    dp = Dispatcher()

    @dp.message_created(Command("start"))
    async def cmd_start(event: MessageCreated):
        uid = event.message.sender.user_id
        user = get_user(uid)
        if not user:
            add_pending_user(uid, getattr(event.message.sender, "username", "") or "")
            await event.message.answer(
                "👋 Ваша заявка отправлена на рассмотрение, ожидайте.")
            return
        if user["role"] == "pending":
            await event.message.answer("⏳ Заявка ещё на рассмотрении.")
            return
        if user["role"] not in ("author", "admin"):
            await event.message.answer("🚫 Нет прав.")
            return
        await event.message.answer(
            "📝 Отправьте фото/видео и/или текст для поста.")

    @dp.message_created()
    async def handle_post(event: MessageCreated):
        uid = event.message.sender.user_id
        user = get_user(uid)
        if not user or user["role"] not in ("author", "admin"):
            await event.message.answer("🚫 Нет прав.")
            return

        state = user_states.setdefault(uid, {})
        state["text"] = event.message.body.text or ""
        state["media"] = []
        state["channels"] = []  # выбранные типы каналов

        for img in (getattr(event.message, "images", None) or []):
            url = getattr(img, "url", None)
            if url:
                path = DOWNLOAD_DIR / f"{uid}_{datetime.now().timestamp()}.jpg"
                path.write_bytes(requests.get(url, timeout=30).content)
                state["media"].append(str(path))

        for vid in (getattr(event.message, "videos", None) or []):
            url = getattr(vid, "url", None)
            if url:
                path = DOWNLOAD_DIR / f"{uid}_{datetime.now().timestamp()}.mp4"
                path.write_bytes(requests.get(url, timeout=60).content)
                state["media"].append(str(path))

        builder = InlineKeyboardBuilder()
        for cat in list_categories():
            builder.row(CallbackButton(text=cat["name"], payload=f"cat:{cat['name']}"))
        await event.message.answer("Выберите категорию поста:", keyboard=builder.as_markup())

    @dp.message_callback()
    async def handle_callback(event: MessageCallback):
        uid = event.message.sender.user_id
        user = get_user(uid)
        if not user or user["role"] not in ("author", "admin"):
            await event.answer(new_text="🚫 Нет прав.")
            return

        data = event.callback.payload or ""
        state = user_states.setdefault(uid, {})

        # --- Выбор категории ---
        if data.startswith("cat:"):
            cat_name = data.split(":", 1)[1]
            state["category"] = cat_name
            state["channels"] = []

            category = get_category(cat_name)
            types_in_cat = sorted({t["type"] for t in category["targets"]})
            if not types_in_cat:
                await event.answer(new_text=f"⚠️ В категории «{cat_name}» нет каналов. Настройте в GUI.")
                return

            builder = InlineKeyboardBuilder()
            for tp in types_in_cat:
                builder.row(CallbackButton(text=f"☐ {tp.upper()}", payload=f"chan:{tp}"))
            builder.row(
                CallbackButton(text="✅ Проверить пост", payload="action:preview"),
                CallbackButton(text="🚀 Опубликовать", payload="action:publish"),
            )
            builder.row(
                CallbackButton(text="🤖 DeepSeek", payload="action:ai"),
                CallbackButton(text="⏰ Отложить", payload="action:schedule"),
            )
            await event.answer(
                new_text=f"Категория: {cat_name}\nВыберите каналы и действие:",
                attachments=[builder.as_markup()],
            )

        # --- Тоггл типа канала ---
        elif data.startswith("chan:"):
            tp = data.split(":", 1)[1]
            sel = state.setdefault("channels", [])
            if tp in sel:
                sel.remove(tp)
            else:
                sel.append(tp)
            await event.answer(new_text=f"{tp.upper()}: {'✅ выбран' if tp in sel else '☐ снят'}")

        # --- Предпросмотр ---
        elif data == "action:preview":
            cat_name = state.get("category")
            if not cat_name:
                await event.answer(new_text="Сначала выберите категорию.")
                return
            category = get_category(cat_name)
            text = (state.get("text", "") + "\n\n" + "\n".join(category["links"])).strip()
            state["final_text"] = text
            await event.message.answer(
                f"📋 **Предпросмотр**\nКатегория: {cat_name}\n"
                f"Каналы: {', '.join(state.get('channels', [])) or '—'}\n"
                f"Медиа: {len(state.get('media', []))}\n\n{text}")

        # --- Публикация ---
        elif data == "action:publish":
            cat_name = state.get("category")
            if not cat_name:
                await event.answer(new_text="Сначала выберите категорию.")
                return
            if not state.get("channels"):
                await event.answer(new_text="Выберите хотя бы один канал.")
                return
            category = get_category(cat_name)
            text = (state.get("text", "") + "\n\n" + "\n".join(category["links"])).strip()
            await event.answer(new_text="⏳ Публикую...")
            results = await _publish_now(cat_name, text, state.get("media", []), state["channels"])
            await event.message.answer("📤 Результаты:\n" + "\n".join(results))

        # --- DeepSeek ---
        elif data == "action:ai":
            topic = state.get("text", "")
            if not topic:
                await event.answer(new_text="Нет текста для генерации.")
                return
            await event.answer(new_text="⏳ Генерирую...")
            gen = generate_post_text(topic)
            state["text"] = gen
            await event.message.answer(f"🤖 Готово:\n\n{gen}")

        elif data == "action:schedule":
            await event.answer(new_text="⏰ Отложенная публикация появится в следующей версии.")

    await dp.start_polling(bot)


async def stop_bot():
    global bot, dp
    if dp:
        await dp.stop_polling()
    bot = None
    dp = None