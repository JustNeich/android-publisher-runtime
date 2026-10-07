"""Purpose-owned Telegram authorization. Human secrets never leave this process."""
import asyncio
import http.cookiejar
import json
import re
import subprocess
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from .config import UserError
from .provision import provisioning_lock
from .vault import Vault


def private_prompt(prompt):
    script = 'text returned of (display dialog ' + json.dumps(prompt, ensure_ascii=False) + ' with title "Android Publisher · вход" default answer "" with hidden answer buttons {"Отмена", "Продолжить"} default button "Продолжить" cancel button "Отмена")'
    result = subprocess.run(['/usr/bin/osascript', '-'], input=script, text=True, capture_output=True)
    if result.returncode or not result.stdout.strip():
        raise UserError('Приватный вход отменён. Повторный запуск продолжит проверку имеющейся авторизации.')
    return result.stdout.rstrip('\n')


def app_keys(page):
    # Existing official /apps page uses readonly spans; newer pages may use inputs.
    id_match = re.search(r'App api_id:?[\s\S]{0,600}?(?:<strong>\s*|value=[\"\'])([1-9]\d{2,10})', page, re.I)
    hash_match = re.search(r'App api_hash:?[\s\S]{0,600}?(?:<span[^>]*>\s*|value=[\"\'])([a-f0-9]{32})', page, re.I)
    if id_match and hash_match:
        return id_match[1], hash_match[1]
    return None


def verify_identity(config, account):
    expected = config.settings.get('telegram_user_id') or config.settings.get('operator_chat')
    if not account or account.bot:
        raise UserError('Нужен собственный аккаунт оператора, вход бота здесь не подходит.')
    if expected and int(expected) != account.id:
        raise UserError('Telegram вошёл под другим оператором. Сессия и личные уведомления не заменены; нужна проверка правильного аккаунта.')


def official_api_credentials(phone, prompt=private_prompt):
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, data=None):
        try:
            req = Request('https://my.telegram.org' + path, urlencode(data).encode() if data is not None else None,
                          {'User-Agent': 'AndroidPublisher/1.0', 'Referer': 'https://my.telegram.org/'})
            with opener.open(req, timeout=30) as response:
                if not response.url.startswith('https://my.telegram.org/'):
                    raise UserError('Официальный вход Telegram перенаправлен на другой сайт.')
                return response.read(1024 * 1024).decode()
        except (OSError, ValueError):
            raise UserError('Официальный сайт Telegram недоступен; повторите вход после восстановления сети.') from None

    try:
        sent = json.loads(request('/auth/send_password', {'phone': phone}))
    except ValueError:
        raise UserError('Telegram не подтвердил запрос кода для API-реквизитов. Не повторяем запрос автоматически.') from None
    if not sent.get('random_hash'):
        raise UserError('Telegram не выдал код для официального сайта. Проверьте номер/лимит на my.telegram.org.')
    code = prompt('Введите здесь код от Telegram для my.telegram.org. Не отправляйте его в чат Codex или Telegram.')
    response = request('/auth/login', {'phone': phone, 'random_hash': sent['random_hash'], 'password': code})
    if response.strip() != 'true':
        raise UserError('Официальный сайт не подтвердил вход. Код не повторяем; следующий вход только по запросу.')
    page = request('/apps')
    keys = app_keys(page)
    if keys:
        return keys
    csrf = re.search(r'name=[\"\']hash[\"\'][^>]*value=[\"\']([^\"\']+)', page)
    if not csrf:
        raise UserError('Форма API Telegram изменилась. Codex: завершите создание приложения на официальном my.telegram.org; не собирайте чужие session-файлы.')
    request('/apps/create', {'hash': csrf[1], 'app_title': 'Android Publisher', 'app_shortname': 'androidpublisher',
                            'app_url': '', 'app_platform': 'desktop', 'app_desc': 'Personal Android Publisher on my Mac'})
    keys = app_keys(request('/apps'))
    if not keys:
        raise UserError('Создание API-приложения не подтверждено. Сначала сверка /apps; повторное создание запрещено.')
    return keys


async def sign_in(config, vault, phone, prompt):
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
    session = vault.get('telegram-provision-session') if vault.has('telegram-provision-session') else ''
    client = TelegramClient(StringSession(session), int(vault.get('telegram-api-id')), vault.get('telegram-api-hash'),
                            request_retries=0, connection_retries=1, flood_sleep_threshold=0)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            sent = await client.send_code_request(phone)
            code = prompt('Введите код входа в отдельную сессию Android Publisher, полученный в Telegram. Код остаётся только на этом Mac.')
            try:
                await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
            except errors.SessionPasswordNeededError:
                password = prompt('Введите пароль двухэтапной защиты Telegram. Он не сохраняется и не передаётся Codex.')
                await client.sign_in(password=password)
        account = await client.get_me()
        verify_identity(config, account)
        vault.put('telegram-provision-session', client.session.save())
        config.save(telegram_user_id=account.id, operator_chat=account.id)
        return {'telegram_authorized': True, 'account_id': account.id, 'session': 'dedicated_keychain', 'secrets_returned': False}
    except UserError:
        raise
    except Exception as error:
        # Telegram exceptions may embed phone/code/password. Only the class is safe.
        raise UserError('Вход Telegram не завершён: ' + type(error).__name__ + '. Код не повторяем автоматически.') from None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def login(config, prompt=private_prompt, vault=None):
    vault = vault or Vault(config.keychain_service)
    with provisioning_lock(config):
        if all(vault.has(k) for k in ('telegram-api-id', 'telegram-api-hash', 'telegram-provision-session')):
            from .provision import authorized_client
            async def verify(client):
                account = await client.get_me()
                verify_identity(config, account)
                config.save(telegram_user_id=account.id, operator_chat=account.id)
                return {'telegram_authorized': True, 'account_id': account.id, 'reused': True}
            try:
                return asyncio.run(authorized_client(config, vault, verify))
            except UserError:
                pass  # An explicitly requested login can repair an expired purpose-owned session.
        phone = prompt('Введите номер вашего Telegram с кодом страны, например +79990000000. Вход нужен для BotFather и чтения MP4 от любых отправителей.')
        if not re.fullmatch(r'\+[1-9]\d{6,14}', phone):
            raise UserError('Номер должен быть в международном формате +…')
        if not all(vault.has(k) for k in ('telegram-api-id', 'telegram-api-hash')):
            api_id, api_hash = official_api_credentials(phone, prompt)
            vault.put('telegram-api-id', api_id); vault.put('telegram-api-hash', api_hash)
        return asyncio.run(sign_in(config, vault, phone, prompt))


def private_secret(config, name):
    from .provision import import_secret
    if not re.fullmatch(r'phone-[a-z0-9-]{1,40}-pin', name):
        raise UserError('Приватное окно предназначено только для разрешённого PIN этого телефона.')
    return import_secret(config, name, private_prompt('Введите цифровой PIN выбранного Android для разрешённой фоновой разблокировки. Не отправляйте PIN в чат.'))
