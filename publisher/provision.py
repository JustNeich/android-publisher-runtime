"""Codex-only provisioning. Secrets enter via stdin or remain inside Keychain.

The optional BotFather transport uses a separately authorized project session;
it never opens another application's session files and never polls bot updates.
"""
import asyncio
import fcntl
import json
import re
import time
from contextlib import contextmanager
from .config import UserError, write_private
from .telegram import Bot
from .vault import Vault

TOKEN = re.compile(r'(?<![\w-])\d{6,16}:[A-Za-z0-9_-]{30,100}(?![\w-])')


@contextmanager
def provisioning_lock(config):
    path = config.data / 'provision.lock'
    path.touch(mode=0o600, exist_ok=True)
    with path.open('r+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UserError('Настройка уже выполняется другим Codex; повторите позже.') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def import_secret(config, name, value, vault=None):
    allowed = name in ('telegram-bot-token', 'telegram-api-id', 'telegram-api-hash', 'telegram-provision-session') or bool(re.fullmatch(r'phone-[a-z0-9-]{1,40}-(token|pin)', name))
    if not allowed or not value or len(value) > 8192:
        raise UserError('Недопустимое имя или формат секрета.')
    if name.endswith('-pin') and not re.fullmatch(r'\d{4,16}', value):
        raise UserError('PIN должен быть цифровым; значение не выводится.')
    if name == 'telegram-api-id' and not re.fullmatch(r'[1-9]\d{0,10}', value):
        raise UserError('Некорректный API ID.')
    if name == 'telegram-api-hash' and not re.fullmatch(r'[a-fA-F0-9]{32}', value):
        raise UserError('Некорректный API hash.')
    vault = vault or Vault(config.keychain_service)
    if name == 'telegram-bot-token':
        if not TOKEN.fullmatch(value):
            raise UserError('Некорректный формат bot token.')
        me = Bot(value, config.settings.get('bot_api', 'https://api.telegram.org')).call('getMe')
        expected = config.settings.get('bot_username')
        if expected and me['username'].lower() != expected.lower():
            raise UserError('Секрет относится к другому боту; автоматическая замена запрещена.')
        config.save(bot_username=me['username'], bot_id=me['id'])
    vault.put(name, value)
    return {'saved_to_keychain': True, 'name': name, 'value_returned': False}


def verified_bot(config, vault, username):
    if not vault.has('telegram-bot-token'):
        return None
    me = Bot(vault.get('telegram-bot-token'), config.settings.get('bot_api', 'https://api.telegram.org')).call('getMe')
    if me['username'].lower() != username.lower():
        raise UserError('В Keychain уже другой бот. Сначала нужна явная миграция; не перезаписываем его.')
    config.save(bot_username=me['username'], bot_id=me['id'])
    return me


async def botfather(config, vault, username, display_name, client):
    """Fresh correlated responses only; a lost creation receipt cannot create twice."""
    journal_path = config.data / 'bot-provision.json'
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
    if journal and journal.get('username', '').lower() != username.lower():
        raise UserError('Есть незавершённая настройка другого бота; Codex должен сначала сверить её.')
    entity = await client.get_entity('BotFather')
    if not (getattr(entity, 'verified', False) and getattr(entity, 'bot', False) and entity.username.lower() == 'botfather'):
        raise UserError('Официальный BotFather не подтверждён.')
    async with client.conversation(entity, timeout=30, total_timeout=180, max_messages=50, exclusive=True) as conv:
        async def ask(text):
            sent = await conv.send_message(text)
            reply = await conv.get_response(sent)
            if reply.sender_id != entity.id or reply.id <= sent.id:
                raise UserError('Ответ BotFather не связан со свежей командой.')
            return reply

        async def choose(reply):
            for row in reply.buttons or []:
                for button in row:
                    if button.text.lstrip('@').lower() == username.lower():
                        return await ask(button.text)
            return None

        me = verified_bot(config, vault, username)
        if not me:
            await ask('/cancel')
            choice = await ask('/token')
            reply = await choose(choice)
            if reply is None:
                if journal.get('creation_started'):
                    raise UserError('Результат создания бота неизвестен. Codex должен сверить BotFather; повторное /newbot запрещено.')
                journal = {'username': username, 'creation_started': True, 'state': 'creation_pending'}
                write_private(journal_path, json.dumps(journal))  # before the first mutation
                await ask('/cancel')
                prompt = await ask('/newbot')
                if 'name' not in prompt.raw_text.lower():
                    raise UserError('BotFather не подтвердил шаг имени; Codex должен разобрать ответ без передачи секрета человеку.')
                prompt = await ask(display_name)
                if 'username' not in prompt.raw_text.lower():
                    raise UserError('BotFather не подтвердил шаг username; настройка приостановлена.')
                reply = await ask(username)
            found = TOKEN.findall(reply.raw_text)
            if len(found) != 1:
                raise UserError('BotFather не выдал однозначный токен. Возможны занятый username, лимит или проверка; Codex должен сверить ответ, не повторять создание.')
            token = found[0]
            me = Bot(token, config.settings.get('bot_api', 'https://api.telegram.org')).call('getMe')
            if me['username'].lower() != username.lower():
                raise UserError('BotFather вернул другой бот; секрет не сохранён.')
            vault.put('telegram-bot-token', token)
            config.save(bot_username=me['username'], bot_id=me['id'])
            journal.update(state='token_saved', token_reply_id=reply.id, bot_id=me['id'])
            write_private(journal_path, json.dumps(journal))
        if not me.get('can_read_all_group_messages'):
            await ask('/cancel')
            choice = await ask('/setprivacy')
            prompt = await choose(choice)
            if prompt is None:
                raise UserError('BotFather не предложил нужный бот для privacy; настройки другого бота не меняем.')
            disable = next((b.text for row in prompt.buttons or [] for b in row if b.text.lower() == 'disable'), None)
            if not disable:
                raise UserError('BotFather не предложил Disable; Codex должен проверить текущий диалог.')
            await ask(disable)
            me = verified_bot(config, vault, username)
            if not me.get('can_read_all_group_messages'):
                raise UserError('Отключение privacy пока не подтверждено getMe.')
        journal.update(username=username, state='verified', bot_id=me['id'])
        write_private(journal_path, json.dumps(journal))
        return {'verified': True, 'username': me['username'], 'bot_id': me['id'],
                'privacy_disabled': True, 'token_saved_to_keychain': True, 'polling_started': False}


async def authorized_client(config, vault, operation):
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        raise UserError('Codex: установите дополнительный пакет проекта .[provision].') from None
    for name in ('telegram-api-id', 'telegram-api-hash', 'telegram-provision-session'):
        if not vault.has(name):
            raise UserError('Нет разрешённой Telegram-авторизации для настройки. Codex должен подключить официальный аккаунт через разрешённый connector; не просить помощника создавать бота или копировать секреты.')
    client = None
    try:
        client = TelegramClient(StringSession(vault.get('telegram-provision-session')), int(vault.get('telegram-api-id')),
                                vault.get('telegram-api-hash'), request_retries=0, connection_retries=1, flood_sleep_threshold=0)
        await client.connect()
        if not await client.is_user_authorized():
            raise UserError('Telegram-авторизация истекла; требуется восстановление разрешённого доступа Codex.')
        account = await client.get_me()
        if account is None or account.bot:
            raise UserError('Для BotFather и прав группы нужен разрешённый аккаунт человека; bot session не подходит.')
        expected = config.settings.get('telegram_user_id') or config.settings.get('operator_chat')
        if expected and account.id != int(expected):
            raise UserError('Выбран другой Telegram-оператор. Настройки не меняем; требуется восстановить разрешённый аккаунт.')
        result = await operation(client)
        current = client.session.save()
        if current != vault.get('telegram-provision-session'):
            vault.put('telegram-provision-session', current)
        return result
    except UserError:
        raise
    except Exception:
        # Library exceptions and BotFather replies may contain secrets; never echo them.
        raise UserError('Telegram-настройка не подтверждена. Codex должен сверить журнал и доступ; повторное создание и отзыв токена запрещены.') from None
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass  # Never print a library teardown exception containing credentials.


def provision_bot(config, username, display_name):
    username = username.lstrip('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username) or not username.lower().endswith('bot') or not 1 <= len(display_name) <= 64 or display_name.startswith('/') or any(c in display_name for c in ('\n', '\r')):
        raise UserError('Нужен валидный username бота и имя до 64 символов.')
    with provisioning_lock(config):
        vault = Vault(config.keychain_service)
        me = verified_bot(config, vault, username)
        if me and me.get('can_read_all_group_messages') and config.settings.get('operator_bot_started'):
            return {'verified': True, 'username': me['username'], 'bot_id': me['id'], 'privacy_disabled': True, 'reused': True, 'polling_started': False}
        result = asyncio.run(authorized_client(config, vault, lambda client: botfather(config, vault, username, display_name, client)))
        async def start_operator(client):
            bot = await client.get_entity(username)
            if bot.id != result['bot_id']:
                raise UserError('Личное уведомление относится к другому боту.')
            if not config.settings.get('operator_bot_started'):
                await client.send_message(bot, '/start')
                config.save(operator_bot_started=True)
            return True
        asyncio.run(authorized_client(config, vault, start_operator))
        return result


async def group_bot(config, vault, chat_id, client):
    from telethon import functions, types, errors, utils
    if not str(chat_id).startswith('-100'):
        raise UserError('Нужен точный ID существующей форумной супергруппы.')
    me = verified_bot(config, vault, config.settings.get('bot_username', ''))
    if not me or not me.get('can_read_all_group_messages'):
        raise UserError('Сначала Codex должен завершить provision-bot.')
    # Resolve through the authenticated dialog inventory, not an invented access hash.
    group = None
    async for dialog in client.iter_dialogs():
        if utils.get_peer_id(dialog.entity) == chat_id:
            group = dialog.entity
            break
    if group is None or not getattr(group, 'forum', False):
        raise UserError('Разрешённый аккаунт не видит указанную форумную группу.')
    bot = await client.get_entity(me['username'])
    if bot.id != me['id'] or not bot.bot:
        raise UserError('Telegram identity бота не совпала с getMe.')
    try:
        participant = (await client(functions.channels.GetParticipantRequest(group, bot))).participant
    except errors.UserNotParticipantError:
        await client(functions.channels.InviteToChannelRequest(group, [bot]))
        participant = (await client(functions.channels.GetParticipantRequest(group, bot))).participant
    rights = getattr(participant, 'admin_rights', None)
    if not rights or not rights.manage_topics:
        # Preserve rights of an existing admin; add only topic management.
        rights = rights or types.ChatAdminRights()
        rights.manage_topics = True
        await client(functions.channels.EditAdminRequest(group, bot, rights, rank='Publisher'))
    bot_api = Bot(vault.get('telegram-bot-token'), config.settings.get('bot_api', 'https://api.telegram.org'))
    member = bot_api.call('getChatMember', {'chat_id': chat_id, 'user_id': me['id']})
    if member.get('status') != 'administrator' or not member.get('can_manage_topics'):
        raise UserError('Права бота не подтверждены свежим getChatMember.')
    return {'chat_id': chat_id, 'bot_id': me['id'], 'administrator_verified': True, 'topics_preserved': True}


def configure_group(config, chat_id):
    with provisioning_lock(config):
        vault = Vault(config.keychain_service)
        return asyncio.run(authorized_client(config, vault, lambda client: group_bot(config, vault, chat_id, client)))


async def forum_topics(client, group):
    from telethon import functions
    result = await client(functions.channels.GetForumTopicsRequest(group, offset_date=None, offset_id=0, offset_topic=0, limit=100))
    return result.topics


async def create_forum(config, vault, channel_id, title, topic_title, client):
    from telethon import functions, utils
    path = config.data / 'provisioning' / ('group-' + channel_id + '.json')
    record = json.loads(path.read_text()) if path.exists() else {'state':'prepared', 'title':title, 'topic_title':topic_title, 'started':time.time()}
    if record['title'] != title or record['topic_title'] != topic_title:
        raise UserError('Для этой связки существует другой журнал группы. Сначала сверка, без новой группы.')
    matches=[]
    async for dialog in client.iter_dialogs():
        group=dialog.entity
        if record.get('chat_id') and utils.get_peer_id(group) == record['chat_id']:
            matches=[group];break
        if getattr(group,'title',None)==title and getattr(group,'forum',False) and getattr(group,'creator',False):
            matches.append(group)
    if len(matches)>1:
        raise UserError('Найдено несколько собственных групп с таким названием. Укажите точный существующий chat_id.')
    group=matches[0] if matches else None
    if record.get('chat_id') and group is None:
        raise UserError('Созданная группа больше не видна. Новую группу автоматически не создаём.')
    if group is None:
        if record['state']!='prepared':
            raise UserError('Создание группы осталось без подтверждения. Сначала сверка Telegram; повторное создание запрещено.')
        record['state']='creating';write_private(path,json.dumps(record))
        result=await client(functions.channels.CreateChannelRequest(title=title,about='Видео и публикации канала '+title,megagroup=True,forum=True))
        group=next((g for g in result.chats if getattr(g,'forum',False) and g.title==title),None)
        if group is None:raise UserError('Telegram не подтвердил созданную форумную группу.')
    record.update(chat_id=utils.get_peer_id(group),state='group_verified');write_private(path,json.dumps(record))
    await group_bot(config,vault,record['chat_id'],client)
    topics=await forum_topics(client,group)
    matches=[t for t in topics if getattr(t,'title',None)==topic_title]
    if len(matches)>1:raise UserError('Несколько топиков публикации с одним названием. Сначала выберите точный ID.')
    if not matches:
        if record.get('topic_attempted'):
            raise UserError('Создание топика не подтверждено. Сначала сверка, без повторного createForumTopic.')
        record['topic_attempted']=time.time();write_private(path,json.dumps(record))
        bot=Bot(vault.get('telegram-bot-token'))
        topic=bot.call('createForumTopic',{'chat_id':record['chat_id'],'name':topic_title})
        record['publication_topic']=topic['message_thread_id']
    else:
        record['publication_topic']=matches[0].id
    record.update(state='verified',general_topic=1);write_private(path,json.dumps(record))
    return {k:record[k] for k in ('chat_id','publication_topic','general_topic','state')}


def provision_group(config, channel_id, title, topic_title='Публикация'):
    if not re.fullmatch(r'[a-z0-9-]{1,40}',channel_id) or not 1<=len(title.strip())<=80 or not 1<=len(topic_title.strip())<=80:
        raise UserError('Нужны slug канала и непустые названия группы/топика до 80 символов.')
    with provisioning_lock(config):
        vault=Vault(config.keychain_service)
        return asyncio.run(authorized_client(config,vault,lambda client:create_forum(config,vault,channel_id,title.strip(),topic_title.strip(),client)))
