"""MTProto topic reader and original document downloads, independent of Bot API limits."""
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from .config import UserError
from .provision import authorized_client
from .vault import Vault

MAX_VIDEO = 2 * 1024 ** 3


def topic_id(message):
    reply = getattr(message, 'reply_to', None)
    return (getattr(reply, 'reply_to_top_id', None) or getattr(reply, 'reply_to_msg_id', None) or 1) if reply else 1


def normalize(message, chat_id):
    document = getattr(message, 'document', None)
    data = {'message_id': message.id, 'date': message.date.timestamp(), 'chat': {'id': chat_id},
            'message_thread_id': topic_id(message), 'text': message.raw_text or '',
            'caption': message.raw_text or '', 'from': {'id': message.sender_id},
            'source_revision': str(getattr(message, 'edit_date', None) or message.date), 'outgoing': bool(getattr(message, 'out', False))}
    reply = getattr(message, 'reply_to', None)
    if reply:
        data['reply_to_message'] = {'message_id': reply.reply_to_msg_id}
    if document:
        filename = next((a.file_name for a in document.attributes if hasattr(a, 'file_name')), 'video-' + str(message.id) + '.mp4')
        data['document'] = {'document_id': document.id, 'file_id': 'mtproto:' + str(document.id),
                            'file_unique_id': str(document.id), 'mime_type': document.mime_type,
                            'file_size': document.size, 'file_name': filename}
    return data


class Inbox:
    def __init__(self, config, engine, telegram=None, vault=None):
        self.config, self.engine, self.db, self.telegram = config, engine, engine.db, telegram
        self.vault = vault or Vault(config.keychain_service)

    def run(self, operation):
        return asyncio.run(authorized_client(self.config, self.vault, operation))

    async def peer(self, client, chat_id):
        from telethon import utils, types
        account = await client.get_me()
        if chat_id == account.id:
            return types.InputPeerSelf()
        cached = self.db.one('SELECT * FROM telegram_peers WHERE chat_id=? AND account_id=?', (chat_id, account.id))
        if cached and cached['kind'] == 'channel':
            return types.InputPeerChannel(utils.resolve_id(chat_id)[0], int(cached['access_hash']))
        try:
            entity = await client.get_input_entity(chat_id)
        except (ValueError, TypeError):
            entity = None
            async for dialog in client.iter_dialogs():
                if utils.get_peer_id(dialog.entity) == chat_id:
                    entity = dialog.input_entity
                    break
        if entity is None:
            raise UserError('Разрешённый Telegram-аккаунт не видит назначенную группу. Codex должен проверить членство.')
        if isinstance(entity, types.InputPeerChannel):
            self.db.execute('INSERT INTO telegram_peers VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET access_hash=excluded.access_hash,account_id=excluded.account_id',
                            (chat_id, str(entity.access_hash), 'channel', account.id))
        return entity

    def verify(self):
        async def operation(client):
            me = await client.get_me()
            return {'authorized': True, 'account_id': me.id, 'video_limit': MAX_VIDEO, 'transport': 'MTProto'}
        return self.run(operation)

    def poll(self):
        async def operation(client):
            count = 0
            channels = self.db.all('SELECT * FROM channels ORDER BY id')
            for channel in channels:
                peer = await self.peer(client, channel['chat_id'])
                record = self.db.one('SELECT * FROM ingress WHERE channel_id=?', (channel['id'],))
                if not record:
                    # Choose the last message before binding; a message arriving during
                    # setup stays after the cursor. Large old topics don't delay live intake.
                    cursor = 0
                    async for old in client.iter_messages(peer, reply_to=channel['publication_topic'], limit=200):
                        if old.date.timestamp() < channel['bound_at']:
                            cursor = old.id
                            break
                    self.db.execute('INSERT INTO ingress(channel_id,cursor,boundary,updated) VALUES(?,?,?,?)', (channel['id'], cursor, cursor, self.db.clock()))
                else:
                    cursor = record['cursor']
                async for message in client.iter_messages(peer, reply_to=channel['publication_topic'], min_id=cursor, reverse=True, limit=100):
                    payload = normalize(message, channel['chat_id'])
                    if payload['message_thread_id'] != channel['publication_topic']:
                        # Replies to the topic root are verified by the server's reply_to filter.
                        payload['message_thread_id'] = channel['publication_topic']
                    if message.date.timestamp() >= channel['bound_at']:
                        sender = await message.get_sender()
                        payload['from']['is_bot'] = bool(getattr(sender, 'bot', False))
                        self.engine.ingest(channel['id'], payload)
                        if self.telegram and not payload.get('document') and message.sender_id != self.telegram.me['id']:
                            self.telegram.process({'message': payload})
                    # Only ack after the video/clarification is committed. Replay is deduplicated.
                    self.db.execute("INSERT INTO ingress(channel_id,cursor,boundary,updated) VALUES(?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET cursor=excluded.cursor,updated=excluded.updated",
                                    (channel['id'], message.id, message.id, self.db.clock()))
                    count += 1
            return {'events_committed': count}
        return self.run(operation)

    def preview(self, channel_id, before=None, limit=50):
        channel = self.engine.channel(channel_id)
        async def operation(client):
            peer = await self.peer(client, channel['chat_id'])
            items = []
            last = None
            async for message in client.iter_messages(peer, reply_to=channel['publication_topic'], offset_id=before or 0, limit=max(1, min(200, limit))):
                last = message.id
                data = normalize(message, channel['chat_id'])
                media = data.get('document')
                if not media or not (media['mime_type'] == 'video/mp4' or media['file_name'].lower().endswith('.mp4')):
                    continue
                existing = self.db.one('SELECT id FROM videos WHERE channel_id=? AND source_message=?', (channel_id, message.id))
                items.append({'message_id': message.id, 'filename': media['file_name'], 'size': media['file_size'],
                              'caption': data['caption'][:500], 'already_imported': bool(existing)})
            return {'channel': channel_id, 'videos': items, 'next_before': last, 'approval_required': True}
        return self.run(operation)

    def import_selected(self, channel_id, message_ids):
        channel = self.engine.channel(channel_id)
        if not message_ids or len(set(message_ids)) != len(message_ids) or len(message_ids) > 200:
            raise UserError('Выберите 1–200 уникальных сообщений из проверенного топика.')
        async def operation(client):
            messages = await client.get_messages(await self.peer(client, channel['chat_id']), ids=message_ids)
            payloads = []
            for message in messages:
                if not message or topic_id(message) != channel['publication_topic']:
                    raise UserError('Исходное сообщение не найдено в назначенном топике. Импорт остановлен.')
                data = normalize(message, channel['chat_id'])
                if not data.get('document'):
                    raise UserError('Выбрано сообщение без MP4.')
                sender = await message.get_sender()
                data['from']['is_bot'] = bool(getattr(sender, 'bot', False))
                payloads.append(data)
            return {'imported': [self.engine.ingest(channel_id, data, imported=True) for data in payloads], 'publication_approved': False}
        return self.run(operation)

    def download(self, video, destination):
        if video['file_size'] is None or not 0 < video['file_size'] <= MAX_VIDEO:
            raise UserError('Для загрузки нужен проверенный размер MP4 от 1 байта до 2 ГБ.')
        if not video.get('source_document') and not video.get('sha256'):
            raise UserError('Прежний оригинал не имеет проверяемого document ID или SHA-256. Сначала сверка YouTube; для новой загрузки пришлите и одобрите исходник повторно, старую карточку отмените.')
        destination = Path(destination); destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = destination.with_suffix('.part')
        async def operation(client):
            source = await client.get_messages(await self.peer(client, video['source_chat']), ids=video['source_message'])
            if not source or topic_id(source) != video['source_topic'] or not source.document:
                raise UserError('Оригинал MP4 больше не доступен в назначенном Telegram-топике.')
            if video.get('source_document') and str(source.document.id) != video['source_document']:
                raise UserError('Документ в исходном сообщении изменился; нужно новое одобрение.')
            if source.document.size != video['file_size']:
                raise UserError('Размер оригинала изменился; загрузка остановлена.')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                await client.download_media(source, file=stream)
                stream.flush(); os.fsync(stream.fileno())
            if temp.stat().st_size != video['file_size']:
                raise UserError('Скачанный MP4 не совпал по размеру с Telegram.')
            with temp.open('rb') as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            if video.get('sha256') and sha != video['sha256']:
                raise UserError('MP4 изменился относительно уже одобренного/скачанного оригинала.')
            os.replace(temp, destination)
            return sha
        try:
            return self.run(operation)
        finally:
            temp.unlink(missing_ok=True)

    def reconcile_deliveries(self, bot_id):
        async def operation(client):
            adopted = 0
            for channel in self.db.all('SELECT * FROM channels'):
                unknown = self.db.all('SELECT * FROM videos WHERE channel_id=? AND card_unknown=1', (channel['id'],))
                if not unknown:
                    continue
                candidates = []
                oldest = None
                async for message in client.iter_messages(await self.peer(client, channel['chat_id']), reply_to=channel['publication_topic'], limit=500):
                    oldest = message.id
                    if message.sender_id == bot_id:
                        candidates.append(message)
                for video in unknown:
                    matches = []
                    for m in candidates:
                        if getattr(getattr(m, 'reply_to', None), 'reply_to_msg_id', None) != video['source_message']:
                            continue
                        data = [getattr(b, 'data', b'') for row in (m.buttons or []) for b in row]
                        if any(re.fullmatch(r'[a-f0-9]{16}:(yes|no|status|now|requeue|cancel):[0-9]+', d.decode(errors='ignore')) for d in data):
                            matches.append(m)
                    if len(matches) == 1:
                        self.db.execute('UPDATE videos SET card_message=?,card_unknown=0,card_dirty=1 WHERE id=?', (matches[0].id, video['id']))
                        adopted += 1
                    elif not matches and oldest is not None and oldest <= video['source_message'] and self.db.clock() - (video['card_attempted'] or self.db.clock()) > 120:
                        # Full source-to-now coverage proves absence; this is a verified retry.
                        self.db.execute('UPDATE videos SET card_unknown=0,card_dirty=1 WHERE id=?', (video['id'],))
            for item in self.db.all("SELECT * FROM outbox WHERE state='unknown'"):
                channel = self.engine.channel(item['channel_id']) if item['channel_id'] else None
                chat = item['chat_id'] or channel['chat_id']
                topic = item['topic_id'] if item['topic_id'] is not None else channel['general_topic'] if channel else None
                matches = []
                oldest = None
                account = await client.get_me()
                # Bot API addresses the recipient ID. MTProto reads that recipient's
                # dialog with the bot, rather than the recipient's Saved Messages.
                dialog = bot_id if chat == account.id else chat
                async for message in client.iter_messages(await self.peer(client, dialog), **({'reply_to': topic} if topic else {}), limit=100):
                    oldest = message.date.timestamp()
                    if message.sender_id == bot_id and message.raw_text == item['text'][:3900] and getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None) == item['reply_to']:
                        if message.date.timestamp() >= item['created'] - 5:
                            matches.append(message)
                if len(matches) == 1:
                    self.db.execute("UPDATE outbox SET state='sent',message_id=?,error=NULL WHERE id=?", (matches[0].id, item['id']))
                    adopted += 1
                elif not matches and oldest is not None and oldest <= item['created'] - 5 and self.db.clock() - (item['attempted'] or self.db.clock()) > 120:
                    self.db.execute("UPDATE outbox SET state='queued',error=NULL WHERE id=? AND state='unknown'", (item['id'],))
            return {'receipts_recovered': adopted}
        return self.run(operation)
