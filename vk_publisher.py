# vk_publisher.py
import vk_api
from vk_api.upload import VkUpload


def publish_to_vk(token, group_id, text, media_paths=None):
    if not token:
        return "❌ VK: не задан токен"
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        return "❌ VK: неверный group_id"

    vk_session = vk_api.VkApi(token=token)
    vk = vk_session.get_api()
    upload = VkUpload(vk_session)

    attachments = []
    for path in (media_paths or []):
        low = path.lower()
        if low.endswith((".jpg", ".jpeg", ".png", ".gif")):
            try:
                photo = upload.photo_wall(path, group_id=gid)[0]
                attachments.append(f"photo{photo['owner_id']}_{photo['id']}")
            except Exception as e:
                return f"❌ VK (фото): {e}"
        elif low.endswith((".mp4", ".mov", ".avi")):
            return "⚠️ VK: загрузка видео не реализована"

    try:
        post = vk.wall.post(
            owner_id=-gid,
            message=text,
            attachments=",".join(attachments) if attachments else None,
            from_group=1,
        )
        return f"✅ VK: пост ID {post['post_id']}"
    except Exception as e:
        return f"❌ VK: {e}"