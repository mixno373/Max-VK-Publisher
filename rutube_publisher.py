# rutube_publisher.py
import requests

RUTUBE_API = "https://rutube.ru/api"

def upload_video_to_rutube(token, channel_id, video_path, title, description=""):
    if not token:
        return "❌ Rutube: не задан токен"
    try:
        with open(video_path, "rb") as f:
            resp = requests.post(
                f"{RUTUBE_API}/video/upload/",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": f},
                data={"title": title, "description": description, "channel_id": channel_id or ""},
                timeout=120,
            )
        if resp.status_code in (200, 201):
            vid = resp.json().get("video_id", "?")
            return f"✅ Rutube: видео {vid}"
        return f"❌ Rutube: HTTP {resp.status_code}"
    except Exception as e:
        return f"❌ Rutube: {e}"