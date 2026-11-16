# vk_publisher.py
import vk_api
from vk_api.upload import VkUpload
from database import get_setting

def publish_to_vk(text: str, media_paths: list = None, group_id: int = None) -> str:
    """Публикует пост в группу VK. Возвращает ID поста или сообщение об ошибке."""
    token = get_setting("vk_token")
    if not token:
        return "❌ Не задан токен VK."
    gid = group_id or int(get_setting("vk_group_id", "0"))
    if gid == 0:
        return "❌ Не задан ID группы VK."

    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    upload = VkUpload(vk_session)

    attachments = []
    if media_paths:
        for path in media_paths:
            path_lower = path.lower()
            if path_lower.endswith((".jpg", ".jpeg", ".png", ".gif")):
                photo = upload.photo_wall(path, group_id=gid)[0]
                attachments.append(f"photo{photo['owner_id']}_{photo['id']}")
            elif path_lower.endswith((".mp4", ".mov", ".avi")):
                # Для видео требуется предварительная загрузка (упрощённо)
                # В реальном проекте нужно использовать video.save
                return "⚠️ Загрузка видео в VK требует дополнительной настройки."

    try:
        post = vk.wall.post(
            owner_id=-gid,  # отрицательный ID для групп
            message=text,
            attachments=",".join(attachments) if attachments else None,
            from_group=1,
        )
        return f"✅ VK: пост опубликован (ID: {post['post_id']})"
    except Exception as e:
        return f"❌ Ошибка VK: {e}"