# rutube_publisher.py
import requests
from database import get_setting

RUTUBE_API_BASE = "https://rutube.ru/api"

def upload_video_to_rutube(video_path: str, title: str, description: str = "") -> str:
    """
    Загружает видео на Rutube.
    Возвращает video_id или сообщение об ошибке.
    Примечание: Rutube API может требовать предварительной авторизации.
    """
    token = get_setting("rutube_token")
    if not token:
        return "❌ Не задан токен Rutube."

    # Пример endpoint (может отличаться в зависимости от актуальной документации)
    upload_url = f"{RUTUBE_API_BASE}/video/upload/"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        with open(video_path, "rb") as f:
            files = {"file": f}
            data = {"title": title, "description": description}
            resp = requests.post(upload_url, headers=headers, files=files, data=data, timeout=60)

        if resp.status_code in (200, 201):
            video_id = resp.json().get("video_id")
            return f"✅ Rutube: видео загружено (ID: {video_id})"
        else:
            return f"❌ Ошибка Rutube: HTTP {resp.status_code} – {resp.text[:200]}"
    except Exception as e:
        return f"❌ Ошибка Rutube: {e}"

def publish_text_to_rutube(text: str) -> str:
    """Публикация текстового поста (если поддерживается)."""
    # В Rutube нет публикации чисто текстовых постов, только видео с описанием.
    return "⚠️ Rutube поддерживает только видео с описанием. Текст будет использован как описание."