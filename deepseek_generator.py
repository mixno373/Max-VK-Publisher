# deepseek_generator.py
from openai import OpenAI
from database import get_setting


def generate_post_text(topic: str, prompt_suffix: str = "") -> str:
    api_key = get_setting("deepseek_api_key")
    if not api_key:
        return "❌ Не задан API-ключ DeepSeek. Настройте его в GUI."

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    system_prompt = (
        "Ты — опытный SMM-специалист. Напиши текст для поста в социальных сетях. "
        "Стиль: живой, вовлекающий, без излишнего официоза. "
        "Длина: 3–5 предложений. В конце добавь призыв к действию."
    )
    user_prompt = f"Тема поста: {topic}. {prompt_suffix}".strip()

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            temperature=0.8,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"❌ Ошибка генерации: {e}"