"""Клиент chad API. Не запускает бота и не публикует посты."""
from urllib.parse import urlsplit, urlunsplit
import requests


def completion_url(url):
    parts = urlsplit(url.strip())
    if parts.scheme != 'https' or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('Укажите HTTPS URL API без логина, пароля, параметров и фрагмента.')
    path = parts.path.rstrip('/')
    if path in ('', '/api/v1'):
        path = '/api/v1/chat/completions'
    elif path != '/api/v1/chat/completions':
        raise ValueError('Ожидается URL /api/v1/chat/completions либо базовый URL /api/v1.')
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def generate_text(*, url, token, model, prompt, current_text='', ca_bundle=None):
    if not token.strip():
        raise ValueError('Не задан API-ключ chad.')
    if not model.strip():
        raise ValueError('Задайте ID доступной модели в настройке ai_model.')
    try:
        response = requests.post(
            completion_url(url),
            headers={'Authorization': 'Bearer ' + token.strip(),
                     'Content-Type': 'application/json'},
            json={
                'model': model.strip(),
                'messages': [
                    {'role': 'system', 'content': 'Подготовь текст публикации. Верни только текст. Не добавляй выдуманные факты.'},
                    {'role': 'user', 'content': prompt + '\n\nТекущий текст:\n' + current_text},
                ],
                'stream': False,
            },
            timeout=(15, 120),
            verify=ca_bundle if ca_bundle else True,
        )
    except requests.exceptions.SSLError:
        raise RuntimeError(
            'TLS: цепочка сертификатов не подтверждена. Проверьте системное '
            'хранилище Windows, HTTPS-прокси/антивирус или укажите проверенны CA bundle. '
            'Не отключайте проверку сертификата.'
        ) from None
    if not response.ok:
        # Do not echo request headers, tokens or full provider responses into the log.
        raise RuntimeError(f'chad API: HTTP {response.status_code}. Проверьте ключ, модель, квоту и формат запроса.')
    try:
        text = response.json()['choices'][0]['message']['content']
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError('chad API вернул ответ без choices[0].message.content.') from None
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError('chad API вернул пустой или нетекстовый ответ.')
    return text.strip()
