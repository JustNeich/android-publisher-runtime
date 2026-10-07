"""Scoped stdio MCP tools for a native Codex agent loop."""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from .adb import WirelessADB
from .config import Config, UserError
from .db import Store
from .engine import Engine, PHONE_KINDS, uid
from .phone import Phone, redact
from .schedule import parse_iso
from .telegram import Bot
from .vault import Vault


def schema(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}


def tool(name, description, properties=None, required=None):
    return {'name': name, 'description': description, 'inputSchema': schema(properties, required),
            'annotations': {'readOnlyHint': name in ('channel_context','queue','history','phone_tools'), 'destructiveHint': False, 'openWorldHint': name in ('phone_call','unlock_phone','quiet_phone','download_video')}}


ALLOWED_PHONE = {'android_get_screen_state','android_press_back','android_press_home','android_dismiss_keyboard','android_tap','android_long_press','android_double_tap','android_swipe','android_scroll','android_pinch','android_find_nodes','android_click_node','android_long_click_node','android_tap_node','android_scroll_to_node','android_type_append_text','android_type_insert_text','android_type_replace_text','android_type_clear_text','android_press_key','android_wait_for_node','android_wait_for_idle','android_get_node_details','android_list_storage_locations','android_list_files','android_open_app','android_close_app','android_open_uri'}
INSPECT_PHONE = {'android_get_screen_state','android_find_nodes','android_get_node_details','android_wait_for_idle','android_press_home','android_press_back','android_open_app','android_close_app'}
PACKAGES = {'com.google.android.apps.youtube.creator','com.google.android.youtube','com.android.chrome'}
S = {'type': 'string'}
N = {'type': 'number'}
TOOLS = [
    tool('channel_context', 'Текущий канал, очередь, история разговора и назначенная задача.'),
    tool('queue', 'Показать текущие ролики и их точные даты.'),
    tool('history', 'Последние подтверждённые действия и ошибки.'),
    tool('move_video', 'Назначить ролику новое будущее время. Загруженный ролик будет проверен в YouTube.', {'video_id': S, 'iso_time': S}, ['video_id', 'iso_time']),
    tool('reorder_queue', 'Переставить все ожидающие ролики и перераспределить будущие слоты.', {'video_ids': {'type': 'array', 'items': S}}, ['video_ids']),
    tool('change_schedule', 'Изменить ежедневные часы и часовой пояс этого канала.', {'timezone': S, 'times': {'type': 'array', 'items': S}}, ['timezone', 'times']),
    tool('cancel_video', 'Отменить ожидающий ролик; загруженный сохранить Private и сдвинуть следующих.', {'video_id': S}, ['video_id']),
    tool('pause_channel', 'Остановить или продолжить новые задачи этого канала.', {'paused': {'type': 'boolean'}}, ['paused']),
    tool('save_metadata', 'Сохранить явно приложенные или уточнённые данные ролика.', {'video_id': S, 'metadata': {'type': 'object'}}, ['video_id', 'metadata']),
    tool('reply', 'Ответить в General либо уточнить данные возле назначенного видео.', {'text': S}, ['text']),
    tool('phone_tools', 'Фактические имена и схемы Android MCP для назначенного телефона.'),
    tool('phone_call', 'Вызвать Android-инструмент назначенного телефона. После действия нужно свежее чтение экрана.', {'name': S, 'arguments': {'type': 'object'}}, ['name', 'arguments']),
    tool('unlock_phone', 'Разбудить телефон; при защищённой блокировке использовать уже разрешённый PIN из Keychain.'),
    tool('record_reconciliation', 'После неизвестной загрузки: сохранить свежую проверку Studio и причину, почему назначенного ролика там нет. Только после неё доступен новый download.', {'evidence_id': S, 'reason': S}, ['evidence_id','reason']),
    tool('record_channel_identity', 'Подтвердить точный UC ID канала свежим экраном перед загрузкой.', {'youtube_id': S, 'evidence_id': S}, ['youtube_id','evidence_id']),
    tool('download_video', 'Получить назначенный MP4 через Telegram и передать его по Wi-Fi ADB с проверкой SHA-256 и пригодности Shorts.'),
    tool('quiet_phone', 'Закрыть использованные приложения и проверить погашенный экран.'),
    tool('finish_job', 'Сохранить проверенный результат задачи, а не предположение.',
         {'outcome': {'type': 'string', 'enum': ['scheduled', 'published', 'private', 'awaiting_decision', 'done', 'absent']},
          'evidence_id': S, 'youtube_id': S, 'observed_slot': N, 'url': S, 'error': S}, ['outcome']),
]


class Server:
    def __init__(self, config, scope, vault=None):
        self.config, self.scope = config, scope
        self.db = Store(config.database)
        self.engine = Engine(self.db)
        self.vault = vault or Vault(config.keychain_service)
        self.phone = None
        self.clean = False
        self.finished = False
        self.unlock_attempted = False

    def job(self):
        return self.engine.valid_job(self.scope['job_id'], self.scope['lease'])

    def channel(self):
        return self.engine.channel(self.job()['channel_id'])

    def device(self):
        channel = self.channel()
        return self.db.one('SELECT * FROM phones WHERE id=?', (channel['phone_id'],))

    def need_phone(self):
        job = self.job()
        if job['kind'] not in PHONE_KINDS and self.scope.get('effective_kind') not in PHONE_KINDS:
            raise UserError('Эта задача не даёт доступа к управлению телефоном.')
        lock = self.db.one('SELECT * FROM phone_locks WHERE job_id=? AND lease=? AND until>?', (job['id'], self.scope['lease'], self.db.clock()))
        if not lock:
            raise UserError('Телефон занят другой задачей или право выполнения истекло.')
        if not self.phone:
            device = self.device()
            WirelessADB(self.config, device).resolve()
            self.phone = Phone(device['endpoint'], self.vault.get('phone-' + device['id'] + '-token'))
            self.phone.initialize()
        return self.phone

    def phone_result(self, name, arguments):
        phone = self.need_phone()
        names = {t['name'] for t in self.filtered_tools(phone)}
        if name not in names:
            raise UserError('Телефон не предоставляет этот инструмент.')
        # Publisher does not need camera, location, arbitrary file removal, or Telegram sending.
        if any(word in name for word in ('camera', 'location', 'notification_reply', 'delete_file', 'clipboard', 'write_file', 'append_file', 'file_replace')):
            raise UserError('Инструмент не входит в полномочия публикатора.')
        job = self.job()
        video = self.engine.video(job['video_id']) if job['video_id'] else None
        effective = self.scope.get('effective_kind', job['kind'])
        readonly = any(name.endswith(suffix) for suffix in ('_get_screen_state', '_find_nodes', '_get_node_details', '_wait_for_idle', '_list_apps', '_list_files', '_read_file', '_list_storage_locations'))
        if video and effective in ('upload', 'reschedule', 'reconcile', 'verify') and video['slot'] and self.db.clock() > video['slot'] + 3600 and not readonly and not name.endswith(('_press_home', '_press_back', '_open_app', '_open_uri', '_close_app')):
            raise UserError('Опоздание больше часа. Только сверка; для публикации нужно решение участника.')
        if name in ('android_open_app','android_close_app') and arguments.get('package_id') not in PACKAGES:
            raise UserError('Разрешены только приложения публикации.')
        if name == 'android_open_uri':
            uri = urlsplit(arguments.get('uri',''))
            if uri.scheme != 'https' or uri.hostname not in ('youtube.com','www.youtube.com','youtu.be','studio.youtube.com'):
                raise UserError('Можно открыть YouTube. Назначенный MP4 доступен в Movies/AndroidPublisher.')
        result = phone.call(name, arguments)
        token = self.vault.get('phone-' + self.device()['id'] + '-token')
        raw_text = '\n'.join(b.get('text', '') for b in result.get('content', []) if b.get('type') == 'text')
        if token in raw_text or 'com.danielealbano.androidremotecontrolmcp' in raw_text:
            result = {**result, 'content': [b for b in result.get('content', []) if b.get('type') != 'image']}
        pin_name = 'phone-' + self.device()['id'] + '-pin'
        result = redact(result, token, self.vault.get(pin_name) if self.vault.has(pin_name) else None)
        if name.endswith('_get_screen_state'):
            for block in result.get('content', []):
                if block.get('type') != 'text':
                    continue
                lines = block.get('text','').splitlines(); compact = []; in_hierarchy = False
                for line in lines:
                    if line.startswith('--- window:'):
                        in_hierarchy = False
                    if line == 'hierarchy:':
                        in_hierarchy = True
                    if not in_hierarchy and '\toff,' not in line:
                        compact.append(line)
                block['text'] = '\n'.join(compact)
        evidence_id = None
        if name.endswith('_get_screen_state') and not result.get('isError'):
            evidence_id = uid()
            text_blocks = [b for b in result.get('content', []) if b.get('type') == 'text']
            self.db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)', (evidence_id, job['id'], name, json.dumps(text_blocks, ensure_ascii=False), self.db.clock()))
            result['content'].append({'type': 'text', 'text': 'evidence_id=' + evidence_id})
        return result

    def filtered_tools(self, phone):
        effective = self.scope.get('effective_kind', self.job()['kind'])
        allowed = INSPECT_PHONE if effective == 'inspect' else ALLOWED_PHONE
        return [item for item in phone.tools() if item['name'] in allowed]

    def unlock(self):
        phone = self.need_phone()
        adb = WirelessADB(self.config, self.device())
        adb.wake()
        state = phone.call('android_get_screen_state', {})
        text = '\n'.join(b.get('text', '') for b in state.get('content', []) if b.get('type') == 'text')
        screen = re.search(r'screen:(\d+)x(\d+)', text)
        if screen:
            width, height = map(int, screen.groups())
            phone.call('android_swipe', {'x1': width / 2, 'x2': width / 2, 'y1': height * .85, 'y2': height * .2, 'duration': 350})
        state = phone.call('android_get_screen_state', {})
        text = '\n'.join(b.get('text', '') for b in state.get('content', []) if b.get('type') == 'text')
        if any(marker in text for marker in ('pinEntry', 'keyguard_pin', 'Enter PIN', 'Введите PIN', 'passwordEntry')):
            if self.unlock_attempted:
                raise UserError('PIN уже проверяли в этой задаче; повторный ввод запрещён.')
            self.unlock_attempted = True
            if self.db.setting('unlock_failures:' + self.device()['id'], 0) >= 3:
                raise UserError('После неудачных попыток PIN нужна ручная проверка владельца.')
            pin_key = 'phone-' + self.device()['id'] + '-pin'
            if not self.vault.has(pin_key):
                raise UserError('Защищённая блокировка требует разрешённого секрета. Codex должен решить доступ; помощника не просим вводить PIN в форму.')
            pin = self.vault.get(pin_key)
            adb.enter_pin(pin)
            time.sleep(.6)
            state = phone.call('android_get_screen_state', {})
            text = '\n'.join(b.get('text', '') for b in state.get('content', []) if b.get('type') == 'text')
            if any(marker in text for marker in ('pinEntry', 'keyguard_pin', 'Wrong PIN', 'Неверный PIN')):
                key = 'unlock_failures:' + self.device()['id']
                self.db.set(key, self.db.setting(key, 0) + 1)
                raise UserError('Телефон не подтвердил разблокировку. PIN не повторяем.')
            self.db.set('unlock_failures:' + self.device()['id'], 0)
        if adb.locked():
            raise UserError('Телефон остался заблокирован; нужна ручная проверка.')
        return {'unlocked': True, 'transport': 'paired-wireless-adb'}

    def quiet(self):
        from .cleanup import quiet_fenced
        job = self.db.one('SELECT * FROM jobs WHERE id=?', (self.scope['job_id'],))
        result = quiet_fenced(self.config, self.db, {**job, 'lease': self.scope['lease']})
        self.clean = True
        return result

    def call(self, name, arguments):
        if self.finished:
            raise UserError('Задача уже завершена.')
        if name == 'quiet_phone':
            return self.quiet()
        job = self.job()
        channel = self.engine.channel(job['channel_id'])
        video = self.engine.video(job['video_id']) if job['video_id'] else (self.engine.video(json.loads(job['args'])['video_id'], channel['id']) if json.loads(job['args']).get('video_id') else None)
        if name == 'channel_context':
            return {'channel': channel, 'job': {**job, 'effective_kind': self.scope.get('effective_kind', job['kind'])},
                    'video': video, 'queue': self.engine.queue(channel['id']),
                    'conversation': self.db.all('SELECT role,text,message_id,created FROM conversation WHERE channel_id=? ORDER BY id DESC LIMIT 40', (channel['id'],))[::-1]}
        if name == 'queue':
            return self.engine.queue(channel['id'])
        if name == 'history':
            return self.db.all('SELECT event,payload,video_id,created FROM history WHERE channel_id=? ORDER BY id DESC LIMIT 100', (channel['id'],))
        if name == 'reply':
            text = arguments['text'].strip()
            if not text or len(text) > 3900:
                raise UserError('Ответ должен быть непустым и не длиннее 3900 символов.')
            args = json.loads(job['args'])
            key = 'reply-' + job['id']
            self.db.execute('INSERT OR IGNORE INTO outbox(id,channel_id,reply_to,text,created) VALUES(?,?,?,?,?)', (key, channel['id'], args.get('message_id') or (video['source_message'] if video and job['kind'] == 'metadata' else None), text, self.db.clock()))
            self.db.execute('UPDATE outbox SET topic_id=? WHERE id=?', (channel['publication_topic'] if job['kind'] == 'metadata' or args.get('video_id') else channel['general_topic'], key))
            return {'saved_for_delivery': True, 'outbox_id': key}
        if name in ('move_video', 'reorder_queue', 'change_schedule', 'cancel_video', 'pause_channel') and job['kind'] != 'general':
            raise UserError('Изменение очереди выполняется по обращению участника в General.')
        if name == 'move_video':
            return self.engine.move(channel['id'], arguments['video_id'], parse_iso(arguments['iso_time']))
        if name == 'reorder_queue':
            return self.engine.reorder(channel['id'], arguments['video_ids'])
        if name == 'change_schedule':
            return self.engine.change_schedule(channel['id'], arguments['timezone'], arguments['times'])
        if name == 'cancel_video':
            return self.engine.cancel(channel['id'], arguments['video_id'])
        if name == 'pause_channel':
            self.db.execute('UPDATE channels SET paused=? WHERE id=?', (bool(arguments['paused']), channel['id']))
            self.db.history(channel['id'], 'pause_changed', arguments)
            return {'paused': arguments['paused'], 'active_task_may_finish': True}
        if name == 'save_metadata':
            if job['kind'] not in ('general', 'metadata'):
                raise UserError('В этой задаче нельзя менять исходные данные.')
            if video and job['kind'] == 'metadata' and arguments['video_id'] != video['id']:
                raise UserError('В этой задаче уточняются данные только назначенного видео.')
            self.engine.set_metadata(channel['id'], arguments['video_id'], arguments['metadata'])
            if job['kind'] == 'metadata':
                self.db.execute("UPDATE jobs SET state='done',completed=?,lease=NULL,lease_until=NULL WHERE id=? AND lease=?", (self.db.clock(), job['id'], job['lease']))
                self.finished = True
            return {'saved': True}
        if name == 'phone_tools':
            return self.filtered_tools(self.need_phone())
        if name == 'phone_call':
            return self.phone_result(arguments['name'], arguments['arguments'])
        if name == 'unlock_phone':
            return self.unlock()
        if name == 'quiet_phone':
            return self.quiet()
        if name == 'record_reconciliation':
            self.need_phone()
            proof = self.db.one('SELECT * FROM evidence WHERE id=? AND job_id=?', (arguments['evidence_id'], job['id']))
            if not proof or self.db.clock() - proof['created'] > 300 or len(arguments['reason'].strip()) < 15:
                raise UserError('Нужна свежая проверка назначенного ролика в Studio и конкретная причина отсутствия.')
            args = json.loads(job['args']); args['reconciliation_checked'] = True
            self.db.execute('UPDATE jobs SET args=? WHERE id=? AND lease=?', (json.dumps(args),job['id'],self.scope['lease']))
            self.db.history(channel['id'], 'reconciliation_checked', arguments, job['video_id'])
            return {'checked': True}
        if name == 'record_channel_identity':
            self.need_phone()
            proof = self.db.one('SELECT * FROM evidence WHERE id=? AND job_id=?', (arguments['evidence_id'], job['id']))
            if not proof or self.db.clock() - proof['created'] > 300 or arguments['youtube_id'] != channel['youtube_id']:
                raise UserError('Свежий экран и точный UC ID должны подтверждать назначенный канал.')
            args = json.loads(job['args']); args['channel_identity_checked'] = self.db.clock()
            self.db.execute('UPDATE jobs SET args=? WHERE id=? AND lease=?', (json.dumps(args), job['id'], job['lease']))
            return {'channel_confirmed': channel['youtube_id']}
        if name == 'download_video':
            if self.scope.get('effective_kind', job['kind']) not in ('upload','publish_now','reconcile') or not video or video['state'] not in ('pending','scheduled','publish_requested'):
                raise UserError('Получение MP4 разрешено только для явно одобренной загрузки.')
            self.need_phone()
            if self.scope.get('reconcile_first') and not json.loads(job['args']).get('reconciliation_checked'):
                raise UserError('Сначала сверка в Studio и record_reconciliation; новый MP4 пока недоступен.')
            destination = self.config.data / 'media' / (video['id'] + '.mp4')
            if video['path'] and destination.exists() and video['sha256']:
                import hashlib
                with destination.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                if digest != video['sha256']:
                    raise UserError('Сохранённый MP4 изменился; нужно восстановить оригинал.')
            else:
                from .inbox import Inbox
                digest = Inbox(self.config, self.engine, vault=self.vault).download(video, destination)
                self.db.execute('UPDATE videos SET path=?,sha256=? WHERE id=?', (str(destination), digest, video['id']))
            from .media import validate_shorts
            validation = validate_shorts(destination)
            if not json.loads(job['args']).get('channel_identity_checked'):
                raise UserError('Перед передачей MP4 подтвердите точный канал через record_channel_identity.')
            target = WirelessADB(self.config, self.device()).transfer(destination, video['id'], digest)
            self.db.execute('UPDATE videos SET device_path=?,media_validated=? WHERE id=?', (target, json.dumps(validation), video['id']))
            return {'device_path': target, 'filename': destination.name, 'size': destination.stat().st_size,
                    'sha256': digest, 'integrity_verified': True, 'shorts': validation}
        if name == 'finish_job':
            effective = self.scope.get('effective_kind', job['kind'])
            if effective in PHONE_KINDS and not self.clean:
                raise UserError('Сначала закройте приложения и погасите экран через quiet_phone.')
            if effective in PHONE_KINDS and arguments['outcome'] in ('scheduled','published','private'):
                if not json.loads(job['args']).get('channel_identity_checked'):
                    raise UserError('Точный канал ещё не подтверждён свежим экраном.')
            self.engine.complete(job['id'], job['lease'], **arguments)
            if video and arguments['outcome'] in ('scheduled','published','private'):
                from .retention import clean_media
                clean_media(self.config, self.db, video['id'])
            self.finished = True
            return {'verified_result_saved': True}
        raise UserError('Неизвестный инструмент.')

    def close(self):
        if self.phone:
            self.phone.close()
        self.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--scope-file', required=True)
    args = parser.parse_args()
    scope_path = Path(args.scope_file)
    if scope_path.stat().st_mode & 0o077:
        raise UserError('Файл права выполнения должен быть доступен только владельцу.')
    scope = json.loads(scope_path.read_text())
    server = Server(Config(args.root), scope)
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if 'id' not in message:
                continue
            try:
                method = message['method']
                if method == 'initialize':
                    result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'android-publisher-scoped', 'version': '0.1.0'}}
                elif method == 'ping':
                    result = {}
                elif method == 'tools/list':
                    result = {'tools': TOOLS}
                elif method == 'tools/call':
                    try:
                        payload = server.call(message['params']['name'], message['params'].get('arguments', {}))
                        result = payload if isinstance(payload, dict) and 'content' in payload else {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}]}
                    except (UserError, KeyError, ValueError) as error:
                        result = {'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}
                else:
                    raise UserError('Метод MCP не поддерживается.')
                response = {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
            except Exception as error:
                response = {'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32603, 'message': type(error).__name__}}
            print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        server.close()


if __name__ == '__main__':
    main()
