import hashlib
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from .config import UserError, write_private
from .engine import uid, metadata_ready
from .phone import NoRedirect
from .schedule import label


class TelegramError(UserError):
    def __init__(self, message, retry_after=None, uncertain=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.uncertain = uncertain


class Bot:
    def __init__(self, token, base='https://api.telegram.org', download_root=None):
        self.token = token
        parsed = urlsplit(base)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise UserError('Некорректный адрес Bot API.')
        if base != 'https://api.telegram.org' and (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')):
            raise UserError('Допустим официальный Bot API или локальный сервер на этом Mac.')
        self.base = base.rstrip('/')
        self.download_root = Path(download_root).resolve() if download_root else None
        self.opener = build_opener(NoRedirect())

    def call(self, method, data=None, timeout=45):
        try:
            request = Request(self.base + '/bot' + self.token + '/' + method,
                              json.dumps(data or {}).encode(), {'Content-Type': 'application/json'}, method='POST')
            with self.opener.open(request, timeout=timeout) as response:
                result = json.loads(response.read(8 * 1024 * 1024))
        except HTTPError as error:
            try:
                payload = json.loads(error.read(65536))
            except ValueError:
                payload = {}
            code = error.code
            error.close()
            if code == 400 and 'message is not modified' in payload.get('description', ''):
                return True
            retry = payload.get('parameters', {}).get('retry_after')
            raise TelegramError(f'Telegram отклонил {method} (HTTP {code}).', retry_after=retry) from None
        except (URLError, OSError, TimeoutError, ValueError):
            raise TelegramError('Ответ Telegram не получен; результат отправки неизвестен.', uncertain=True) from None
        if not result.get('ok'):
            raise TelegramError('Telegram отклонил запрос ' + method, retry_after=result.get('parameters', {}).get('retry_after'))
        return result['result']

    def member(self, chat, user):
        value = self.call('getChatMember', {'chat_id': chat, 'user_id': user})
        return value['status'] in ('creator', 'administrator', 'member') or (value['status'] == 'restricted' and value.get('is_member', False))



def card(video, channel):
    metadata = json.loads(video['metadata'])
    title = metadata.get('title', video['filename'])
    when = label(video['slot'], channel['timezone'])
    state = video['state']
    if state == 'review':
        text = f'{title}\nОпубликовать?'
        choices = [('Да', 'yes'), ('Нет', 'no')]
    elif state in ('pending', 'scheduled'):
        stage = 'Отложенный выход проверен в YouTube' if state == 'scheduled' and video['observed_slot'] == video['slot'] else 'Загрузка поставлена на исполнение; проверим сохранённое время в YouTube'
        text = f'{title}\nЗапланировано (pending): {when}\n{stage}'
        choices = []
    elif state == 'published':
        text = f"{title}\nОпубликовано. Подтверждено: {label(video['published_at'], channel['timezone'])}\n{video['url']}"
        choices = []
    elif state == 'awaiting_decision':
        text = f"{title}\nОшибка. План: {when}\n{video['error'] or 'Нужно решение участника'}"
        choices = [('Опубликовать сейчас', 'now'), ('Вернуть в конец очереди', 'requeue'), ('Отменить', 'cancel')]
    else:
        names = {'rejected': 'Отклонено', 'cancel_requested': 'Отмена: проверяем Private в YouTube',
                 'cancelled': 'Отменено; загруженный ролик оставлен Private' if video['youtube_video_id'] else 'Отменено', 'publish_requested': 'Публикуем сейчас по решению участника'}
        text = title + '\n' + names.get(state, state)
        choices = []
    if video['error'] and state not in ('awaiting_decision', 'published', 'cancelled', 'rejected'):
        text += '\nОшибка: ' + video['error'] + '\nПовторим после восстановления; сначала проверим YouTube.'
    if not metadata_ready(metadata) and state not in ('cancelled', 'rejected'):
        text += '\nУточняем недостающие данные возле этого видео; загрузка начнётся после ответа.'
    if not choices:
        choices = [('Обновить статус', 'status')]
    keyboard = {'inline_keyboard': [[{'text': name, 'callback_data': f"{video['id']}:{action}:{video['version']}"}] for name, action in choices]}
    text = text[:3900]
    signature = hashlib.sha256(json.dumps([text, keyboard], sort_keys=True).encode()).hexdigest()
    return text, keyboard, signature


class TelegramLoop:
    def __init__(self, engine, bot):
        self.engine, self.db, self.bot = engine, engine.db, bot
        self.me = bot.call('getMe')

    def ingest_update(self, update):
        with self.db.transaction():
            self.db.execute('INSERT OR IGNORE INTO updates(id,payload,received) VALUES(?,?,?)', (update['update_id'], json.dumps(update, ensure_ascii=False), self.db.clock()))
            self.db.set('telegram_offset', max(self.db.setting('telegram_offset', 0), update['update_id'] + 1))

    def process(self, update):
        callback = update.get('callback_query')
        if callback:
            message = callback.get('message') or {}
            chat = message.get('chat', {}).get('id')
            notice = 'Готово'
            try:
                video_id, action, version = callback.get('data', '').split(':')
                video = self.engine.video(video_id)
                channel = self.engine.channel(video['channel_id'])
                if chat != channel['chat_id'] or message.get('message_id') != video['card_message'] or not self.bot.member(chat, callback['from']['id']):
                    raise UserError('Эта кнопка доступна участникам привязанной группы.')
                if action == 'status':
                    self.db.execute('UPDATE videos SET card_dirty=1 WHERE id=?', (video_id,))
                    notice = 'Карточка показывает проверенное состояние'
                else:
                    self.engine.decision(video_id, action, int(version), callback['from']['id'])
            except (UserError, ValueError) as error:
                notice = str(error)[:180]
            self.bot.call('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': notice})
            return
        message = update.get('message')
        if not message or message.get('from', {}).get('is_bot'):
            return
        channel = self.db.one('SELECT * FROM channels WHERE chat_id=?', (message['chat']['id'],))
        if not channel or message.get('date', 0) < channel['bound_at']:
            return
        topic = message.get('message_thread_id', 1)
        if topic == channel['publication_topic']:
            # Bot API cards/control; MTProto owns media ingress and source identity.
            reply_id = message.get('reply_to_message', {}).get('message_id')
            video = self.db.one('SELECT * FROM videos WHERE channel_id=? AND (source_message=? OR card_message=?)', (channel['id'], reply_id, reply_id)) if reply_id else None
            if not video and reply_id:
                out = self.db.one('SELECT * FROM outbox WHERE channel_id=? AND message_id=? AND topic_id=?', (channel['id'], reply_id, topic))
                if out and out['reply_to']:
                    video = self.db.one('SELECT * FROM videos WHERE channel_id=? AND source_message=?', (channel['id'], out['reply_to']))
            if video and message.get('text') and not message.get('document') and not message.get('video'):
                known = self.db.one("SELECT id FROM conversation WHERE channel_id=? AND role='user' AND message_id=?", (channel['id'], message['message_id']))
                if not known and self.bot.member(channel['chat_id'], message['from']['id']):
                    with self.db.transaction():
                        self.db.execute('INSERT INTO conversation(channel_id,role,text,message_id,created) VALUES(?,?,?,?,?)', (channel['id'], 'user', message['text'], message['message_id'], self.db.clock()))
                        self.engine.enqueue(channel['id'], 'general', args={'text': message['text'], 'video_id': video['id'], 'message_id': message['message_id'], 'actor': message['from']['id']})
        if topic == channel['general_topic']:
            text = message.get('text', '')
            # Telegram entity offsets use UTF-16; do not slice Python strings with them.
            mentioned = any(e['type'] == 'mention' and text.encode('utf-16-le')[e['offset'] * 2:(e['offset'] + e['length']) * 2].decode('utf-16-le').lower() == '@' + self.me['username'].lower() for e in message.get('entities', []))
            reply = message.get('reply_to_message', {})
            called = mentioned or reply.get('from', {}).get('id') == self.me['id']
            if not called or not self.bot.member(channel['chat_id'], message['from']['id']):
                return
            with self.db.transaction():
                known = self.db.one("SELECT id FROM conversation WHERE channel_id=? AND role='user' AND message_id=?", (channel['id'], message['message_id']))
                if known:
                    return
                # Retain reply text/media reference for natural follow-up and metadata clarification.
                context = text + ('\nОтвет на: ' + (reply.get('text') or reply.get('caption') or '') if reply else '')
                self.db.execute('INSERT INTO conversation(channel_id,role,text,message_id,created) VALUES(?,?,?,?,?)', (channel['id'], 'user', context, message['message_id'], self.db.clock()))
                self.engine.enqueue(channel['id'], 'general', args={'text': context, 'message_id': message['message_id'], 'actor': message['from']['id']})

    def poll(self):
        offset = self.db.setting('telegram_offset', 0)
        updates = self.bot.call('getUpdates', {'offset': offset, 'timeout': 1, 'allowed_updates': ['message', 'callback_query']}, timeout=10)
        for update in updates:
            self.ingest_update(update)
        for row in self.db.all('SELECT * FROM updates WHERE processed=0 ORDER BY id LIMIT 100'):
            self.process(json.loads(row['payload']))
            self.db.execute('UPDATE updates SET processed=1 WHERE id=?', (row['id'],))

    def deliver(self):
        for video in self.db.all('SELECT * FROM videos WHERE card_unknown=0 AND card_dirty=1 ORDER BY created LIMIT 100'):
            channel = self.engine.channel(video['channel_id'])
            text, markup, signature = card(video, channel)
            if str(video['card_version']) == signature:
                self.db.execute('UPDATE videos SET card_dirty=0 WHERE id=? AND version=? AND error IS ?', (video['id'], video['version'], video['error']))
                continue
            if video['card_message']:
                self.bot.call('editMessageText', {'chat_id': channel['chat_id'], 'message_id': video['card_message'], 'text': text, 'reply_markup': markup, 'link_preview_options': {'is_disabled': True}})
                self.db.execute('UPDATE videos SET card_version=?,card_dirty=0 WHERE id=? AND version=? AND error IS ?', (signature, video['id'], video['version'], video['error']))
            else:
                # Durable pre-send marker: crash or unknown reply cannot resend blindly.
                self.db.execute('UPDATE videos SET card_unknown=1,card_attempted=? WHERE id=?', (self.db.clock(), video['id']))
                try:
                    result = self.bot.call('sendMessage', {'chat_id': channel['chat_id'], 'message_thread_id': channel['publication_topic'], 'text': text,
                                                         'reply_parameters': {'message_id': video['source_message'], 'allow_sending_without_reply': False}, 'reply_markup': markup})
                except TelegramError as error:
                    if not error.uncertain:
                        self.db.execute('UPDATE videos SET card_unknown=0 WHERE id=?', (video['id'],))
                    raise
                self.db.execute('UPDATE videos SET card_message=?,card_unknown=0,card_version=? WHERE id=?', (result['message_id'], signature, video['id']))
                self.db.execute('UPDATE videos SET card_dirty=0 WHERE id=? AND version=? AND error IS ?', (video['id'], video['version'], video['error']))
        self.deliver_outbox()

    def deliver_outbox(self, incidents_only=False):
        for item in self.db.all("SELECT * FROM outbox WHERE state='queued'" + (" AND id LIKE 'incident-%'" if incidents_only else '') + ' ORDER BY created LIMIT 20'):
            channel = self.engine.channel(item['channel_id']) if item['channel_id'] else None
            if not self.db.execute("UPDATE outbox SET state='sending',attempted=? WHERE id=? AND state='queued'", (self.db.clock(), item['id'])).rowcount:
                continue
            data = {'chat_id': item['chat_id'] or channel['chat_id'], 'text': item['text'][:3900]}
            topic = item['topic_id'] if item['topic_id'] is not None else (channel['general_topic'] if channel else None)
            if topic is not None:
                data['message_thread_id'] = topic
            if item['reply_to']:
                data['reply_parameters'] = {'message_id': item['reply_to'], 'allow_sending_without_reply': False}
            try:
                receipt = self.bot.call('sendMessage', data)
            except TelegramError as error:
                self.db.execute('UPDATE outbox SET state=?,error=? WHERE id=?', ('unknown' if error.uncertain else 'queued', str(error), item['id']))
                raise
            with self.db.transaction():
                self.db.execute("UPDATE outbox SET state='sent',message_id=? WHERE id=?", (receipt['message_id'], item['id']))
                self.db.execute('INSERT INTO conversation(channel_id,role,text,message_id,created) VALUES(?,?,?,?,?)', (item['channel_id'], 'assistant', item['text'], receipt['message_id'], self.db.clock()))
