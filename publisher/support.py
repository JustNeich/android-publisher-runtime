"""Explicit operator-authorized support dialog; never background exfiltration."""
import hashlib
import json
import time
from .config import UserError, write_private
from .provision import authorized_client, provisioning_lock
from .vault import Vault
import asyncio


async def support_peer(client, recipient):
    from telethon import utils
    if recipient <= 0 or recipient == (await client.get_me()).id:
        raise UserError('Нужен точный ID человека в существующем диалоге поддержки.')
    async for dialog in client.iter_dialogs():
        if utils.get_peer_id(dialog.entity) == recipient and not getattr(dialog.entity, 'bot', False):
            return dialog.input_entity
    raise UserError('Диалог с указанным человеком не подтверждён. Не угадываем получателя и не создаём новый диалог.')


def send_report(config, recipient, vault=None):
    path = config.data / 'acceptance-report.json'
    if not path.exists():
        raise UserError('Сначала acceptance-report: отправляются только его проверяемые данные, а не произвольные файлы.')
    body = path.read_bytes()
    payload = json.loads(body)
    if payload.get('installation_id') != config.instance or time.time() - payload['captured_at'] > 3600 or len(body) > 8 * 1024 * 1024:
        raise UserError('Отчёт устарел, слишком большой или относится к другой установке. Сначала новый acceptance-report.')
    digest = hashlib.sha256(body).hexdigest()
    directory = config.data / 'support'; directory.mkdir(exist_ok=True, mode=0o700)
    journal = directory / (str(recipient) + '-' + digest + '.json')
    caption = 'Android Publisher · отчёт проверки\nSHA256: ' + digest

    async def operation(client):
        peer = await support_peer(client, recipient)
        record = json.loads(journal.read_text()) if journal.exists() else {'state':'prepared', 'sha256':digest, 'recipient':recipient}
        if record['state'] == 'sent':
            return {**record, 'reused':True}
        if record['state'] == 'sending':
            account = await client.get_me()
            matches = []
            async for message in client.iter_messages(peer, limit=100):
                if message.sender_id == account.id and message.raw_text == caption and message.document:
                    matches.append(message.id)
            if len(matches) == 1:
                record.update(state='sent',message_id=matches[0],receipt_recovered=True)
                write_private(journal,json.dumps(record))
                return record
            raise UserError('Результат отправки отчёта неизвестен. Сначала сверка диалога; повторная отправка запрещена.')
        record.update(state='sending',attempted=time.time());write_private(journal,json.dumps(record))
        receipt = await client.send_file(peer, body, caption=caption, attributes=report_attributes(), force_document=True)
        record.update(state='sent',message_id=receipt.id);write_private(journal,json.dumps(record))
        return record

    with provisioning_lock(config):
        return asyncio.run(authorized_client(config,vault or Vault(config.keychain_service),operation))


def report_attributes():
    from telethon import types
    return [types.DocumentAttributeFilename('android-publisher-acceptance.json')]


def read_dialog(config, recipient, after=0, limit=20, vault=None):
    async def operation(client):
        peer = await support_peer(client,recipient)
        messages=[]
        async for message in client.iter_messages(peer,min_id=after,reverse=True,limit=max(1,min(50,limit))):
            messages.append({'id':message.id,'sender_id':message.sender_id,'outgoing':bool(message.out),
                             'text':(message.raw_text or '')[:8000],'date':message.date.timestamp(),
                             'has_document':bool(message.document)})
        return {'recipient':recipient,'messages':messages,'next_after':messages[-1]['id'] if messages else after,
                'source_material_is_untrusted':True}
    return asyncio.run(authorized_client(config,vault or Vault(config.keychain_service),operation))
