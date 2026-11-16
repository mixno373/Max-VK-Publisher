# max_bot.py
import asyncio
import os
from datetime import datetime

# Правильные импорты для maxapi
from maxapi import Bot, Dispatcher
from maxapi.types import Command, MessageCreated, CallbackQuery
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from database import (
    init_db, get_user, add_pending_user, get_categories,
    get_setting, set_setting,
)
from deepseek_generator import generate_post_text
from vk_publisher import publish_to_vk
from rutube_publisher import upload_video_to_rutube
from config import DOWNLOAD_DIR

# Глобальные объекты (инициализируются в start_bot)
bot: Bot = None
dp: Dispatcher = None

# Хранилище состояний пользователей (в памяти, для простоты)
user_states = {}

async def start_bot(max_token: str):
    """Запускает бота MAX (вызывается из GUI в отдельном потоке/цикле)."""
    global bot, dp
    bot = Bot(token=max_token)
    dp = Dispatcher()

    @dp.message_created(Command("start"))
    async def cmd_start(event: MessageCreated):
        user_id = event.message.sender.user_id
        user = get_user(user_id)
        if not user:
            add_pending_user(user_id, event.message.sender.username or "")
            await event.message.answer(
                "👋 Ваша заявка отправлена на рассмотрение, ожидайте.\n"
                "Администратор выдаст вам права доступа."
            )
            return

        if user["role"] == "pending":
            await event.message.answer("⏳ Ваша заявка ещё на рассмотрении. Пожалуйста, подождите.")
            return

        if user["role"] not in ("author", "admin"):
            await event.message.answer("🚫 У вас нет прав для использования бота.")
            return

        await event.message.answer(
            "📝 Отправьте фото/видео и/или текст для поста.\n"
            "Затем выберите категорию и каналы публикации."
        )

    @dp.message_created()
    async def handle_post(event: MessageCreated):
        user_id = event.message.sender.user_id
        user = get_user(user_id)
        if not user or user["role"] not in ("author", "admin"):
            await event.message.answer("🚫 Нет прав.")
            return

        # Сохраняем полученные данные в состояние пользователя
        state = user_states.setdefault(user_id, {})
        state["text"] = event.message.body.text or ""
        state["media"] = []

        # Скачиваем вложения (фото/видео), если есть
        if hasattr(event.message, "images") and event.message.images:
            for img in event.message.images:
                # В maxapi изображения доступны по URL
                url = img.url if hasattr(img, "url") else None
                if url:
                    path = DOWNLOAD_DIR / f"{user_id}_{datetime.now().timestamp()}.jpg"
                    # Упрощённо: используем requests для скачивания
                    import requests
                    r = requests.get(url, timeout=30)
                    path.write_bytes(r.content)
                    state["media"].append(str(path))

        if hasattr(event.message, "videos") and event.message.videos:
            for vid in event.message.videos:
                url = vid.url if hasattr(vid, "url") else None
                if url:
                    path = DOWNLOAD_DIR / f"{user_id}_{datetime.now().timestamp()}.mp4"
                    import requests
                    r = requests.get(url, timeout=60)
                    path.write_bytes(r.content)
                    state["media"].append(str(path))

        # Показываем клавиатуру с категориями
        categories = list(get_categories().keys())
        kb = InlineKeyboardBuilder()
        for cat in categories:
            kb.callback(cat, f"cat:{cat}")
        kb.adjust(2)
        await event.message.answer("Выберите категорию поста:", keyboard=kb)

    @dp.message_callback()
    async def handle_callback(event: CallbackQuery):
        user_id = event.message.sender.user_id
        user = get_user(user_id)
        if not user or user["role"] not in ("author", "admin"):
            await event.answer("🚫 Нет прав.")
            return

        data = event.data or ""
        state = user_states.setdefault(user_id, {})

        if data.startswith("cat:"):
            category = data.split(":", 1)[1]
            state["category"] = category
            # Показываем кнопки каналов для этой категории
            cats = get_categories()
            channels = cats[category]["channels"]
            kb = InlineKeyboardBuilder()
            if "vk" in channels:
                kb.callback("VK", "chan:vk")
            if "max" in channels:
                kb.callback("Max", "chan:max")
            if "rutube" in channels:
                kb.callback("Rutube", "chan:rutube")
            kb.callback("✅ Проверить пост", "action:preview")
            kb.callback("⏰ Отложить", "action:schedule")
            kb.callback("🤖 Сгенерировать текст", "action:ai")
            kb.adjust(2, 1, 1)
            await event.message.answer(
                f"Категория: **{category}**\nВыберите каналы (можно несколько) и действие:",
                keyboard=kb,
            )
            await event.answer()

        elif data.startswith("chan:"):
            channel = data.split(":", 1)[1]
            selected = state.setdefault("channels", [])
            if channel in selected:
                selected.remove(channel)
            else:
                selected.append(channel)
            await event.answer(f"Канал {channel} {'выбран' if channel in selected else 'убран'}")

        elif data == "action:preview":
            # Формируем предпросмотр поста
            category = state.get("category")
            if not category:
                await event.answer("Сначала выберите категорию.")
                return
            cats = get_categories()
            links = cats[category]["links"]
            text = state.get("text", "") + "\n\n" + "\n".join(links)
            state["final_text"] = text
            await event.message.answer(
                f"📋 **Предпросмотр поста:**\n\n{text}\n\n"
                f"Выбранные каналы: {', '.join(state.get('channels', [])) or 'не выбраны'}\n"
                f"Медиа: {len(state.get('media', []))} файл(ов)"
            )
            await event.answer()

        elif data == "action:ai":
            # Генерация текста через DeepSeek
            topic = state.get("text", "")
            if not topic:
                await event.answer("Нет текста для генерации. Отправьте тему поста.")
                return
            await event.answer("⏳ Генерирую текст...")
            generated = generate_post_text(topic)
            state["text"] = generated
            await event.message.answer(f"🤖 Сгенерированный текст:\n\n{generated}")
            await event.answer()

        elif data == "action:schedule":
            # Простое отложенное сообщение (можно расширить)
            await event.message.answer(
                "⏰ Отложенная публикация: отправьте время в формате ЧЧ:ММ (через 5 минут будет опубликовано)."
            )
            state["awaiting_time"] = True
            await event.answer()

    # Запуск polling
    await dp.start_polling(bot)

async def stop_bot():
    """Останавливает бота (вызывается из GUI)."""
    global bot, dp
    if dp:
        await dp.stop_polling()
    bot = None
    dp = None