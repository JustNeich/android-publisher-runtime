"""Foreground plugin MCP. Shares phone exclusion with the background publisher."""
import argparse
import fcntl
import json
import secrets
import sys
import time
from pathlib import Path
from .config import Config, UserError
from .db import Store
from .engine import Engine, clean_metadata
from .mcp import Server as ScopedServer, tool, S
from .phone import Phone, redact
from .vault import Vault

TOOLS = [
    tool('publisher_status', 'Состояние установки, телефоны, каналы, инциденты и задания. Не запускает настройку.'),
    tool('publisher_doctor', 'Проверить реальные доступы, Wi-Fi и фоновой запуск.'),
    tool('android_acquire', 'Выбрать физический телефон и получить общую блокировку на время ручного поручения.', {'phone_id': S}, ['phone_id']),
    tool('android_tools', 'Фактические имена и схемы инструментов выбранного Android.'),
    tool('android_call', 'Выполнить порученное человеком действие на выбранном Android. Получать свежий экран между действиями.', {'name': S, 'arguments': {'type': 'object'}}, ['name','arguments']),
    tool('android_unlock', 'Разбудить и разблокировать выбранный Android разрешённым PIN из Keychain.'),
    tool('android_quiet', 'Закрыть приложения публикации, вернуться домой и погасить экран.'),
    tool('android_release', 'Освободить телефон для фонового исполнителя.'),
    tool('publisher_queue', 'Очередь только выбранного канала.', {'channel_id': S}, ['channel_id']),
    tool('publisher_move', 'Перенести одобренный ролик, затем применить перенос в YouTube.', {'channel_id': S,'video_id': S,'iso_time': S}, ['channel_id','video_id','iso_time']),
    tool('publisher_cancel', 'Отменить ожидающий ролик. Уже загруженный сначала перевести в Private.', {'channel_id': S,'video_id': S}, ['channel_id','video_id']),
    tool('publisher_schedule', 'Изменить согласованные часы и часовой пояс канала.', {'channel_id': S,'timezone': S,'times': {'type':'array','items':S}}, ['channel_id','timezone','times']),
    tool('publisher_import_preview', 'Предложить выбор старых MP4 из привязанного топика. Ничего не одобряет.', {'channel_id': S,'before_id': {'type':'integer'},'limit': {'type':'integer'}}, ['channel_id']),
    tool('publisher_import_selected', 'Импортировать согласованные исходные сообщения. Каждый всё равно получает Да/Нет.', {'channel_id': S,'message_ids': {'type':'array','items':{'type':'integer'}}}, ['channel_id','message_ids']),
]


class ManualPhone(ScopedServer):
    def __init__(self, config, db, phone_id, vault):
        self.config, self.db, self.engine, self.vault = config, db, Engine(db), vault
        self.phone_id = phone_id
        self.phone = None
        self.clean = self.finished = self.unlock_attempted = False
        self.id = 'manual-' + secrets.token_hex(12)
        self.lease = secrets.token_hex(24)
        self.scope = {'job_id': self.id, 'lease': self.lease}
        if not self.device():
            raise UserError('Телефон не найден. Сначала добавьте его через навык Android Publisher.')
        self.lifecycle = (config.data / 'lifecycle.lock').open('a+')
        try:
            fcntl.flock(self.lifecycle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with db.transaction():
                occupied = db.one('SELECT * FROM phone_locks WHERE phone_id=? AND until>?', (phone_id, db.clock()))
                if occupied:
                    raise UserError('Телефон занят другим чатом или публикацией. Дождитесь освобождения; не обходите блокировку прямым MCP/ADB.')
                db.execute('INSERT INTO phone_locks VALUES(?,?,?,?) ON CONFLICT(phone_id) DO UPDATE SET job_id=excluded.job_id,lease=excluded.lease,until=excluded.until',
                           (phone_id, self.id, self.lease, db.clock() + 600))
        except BlockingIOError:
            self.lifecycle.close()
            raise UserError('Сейчас выполняется обновление или восстановление установки. Дождитесь его завершения.') from None
        except Exception:
            self.lifecycle.close()
            raise

    def device(self):
        return self.db.one('SELECT * FROM phones WHERE id=?', (self.phone_id,))

    def need_phone(self):
        if not self.db.one('SELECT * FROM phone_locks WHERE job_id=? AND lease=? AND until>?', (self.id, self.lease, self.db.clock())):
            raise UserError('Право ручного управления истекло; выберите телефон заново.')
        self.db.execute('UPDATE phone_locks SET until=? WHERE job_id=? AND lease=?', (self.db.clock() + 600, self.id, self.lease))
        if self.phone is None:
            device = self.device()
            from .adb import WirelessADB
            if device['id'] in self.config.settings.get('wireless_adb', {}):
                try:
                    WirelessADB(self.config, device).resolve()
                except UserError:
                    pass  # Awake foreground MCP can still be read; wake/quiet require ADB.
            self.phone = Phone(device['endpoint'], self.vault.get('phone-' + self.phone_id + '-token'))
            self.phone.initialize()
        return self.phone

    def filtered_tools(self, phone):
        return phone.tools()

    def phone_result(self, name, arguments):
        phone = self.need_phone()
        if name not in {t['name'] for t in phone.tools()}:
            raise UserError('Телефон не предоставляет этот инструмент.')
        token = self.vault.get('phone-' + self.phone_id + '-token')
        result = phone.call(name, arguments)
        raw = '\n'.join(b.get('text','') for b in result.get('content',[]) if b.get('type') == 'text')
        if token in raw or 'com.danielealbano.androidremotecontrolmcp' in raw:
            result = {'isError': True, 'content': [{'type':'text','text':'Настройки секрета коннектора скрыты. Используйте защищённую настройку через USB, без снимков токена.'}]}
        return redact(result, token)

    def quiet(self):
        from .cleanup import quiet_fenced
        self.need_phone()
        return quiet_fenced(self.config, self.db, {'id': self.id, 'lease': self.lease})

    def close(self):
        if self.phone:
            self.phone.close()
        self.db.execute('DELETE FROM phone_locks WHERE job_id=? AND lease=?', (self.id, self.lease))
        self.lifecycle.close()


class AdminServer:
    def __init__(self, config, vault=None):
        self.config = config
        self.db = Store(config.database)
        self.engine = Engine(self.db)
        self.vault = vault or Vault(config.keychain_service)
        self.manual = None

    def call(self, name, args):
        if name == 'publisher_status':
            return status(self.config, self.db)
        if name == 'publisher_doctor':
            from .install import doctor
            return doctor(self.config)
        if name == 'android_acquire':
            if self.manual:
                self.manual.close(); self.manual = None
            self.manual = ManualPhone(self.config, self.db, args['phone_id'], self.vault)
            return {'selected': self.manual.device(), 'lease_seconds': 600, 'shared_exclusion': True}
        if name == 'android_release':
            if self.manual:
                self.manual.close(); self.manual = None
            return {'released': True}
        if name.startswith('android_'):
            if not self.manual:
                raise UserError('Сначала android_acquire с точным phone_id из publisher_status.')
            if name == 'android_tools':
                return self.manual.need_phone().tools()
            if name == 'android_call':
                return self.manual.phone_result(args['name'], args.get('arguments', {}))
            if name == 'android_unlock':
                return self.manual.unlock()
            if name == 'android_quiet':
                return self.manual.quiet()
        if name == 'publisher_queue':
            return self.engine.queue(args['channel_id'])
        if name == 'publisher_move':
            from .schedule import parse_iso
            return self.engine.move(args['channel_id'], args['video_id'], parse_iso(args['iso_time']))
        if name == 'publisher_cancel':
            return self.engine.cancel(args['channel_id'], args['video_id'])
        if name == 'publisher_schedule':
            return self.engine.change_schedule(args['channel_id'], args['timezone'], args['times'])
        if name.startswith('publisher_import_'):
            from .inbox import Inbox
            inbox = Inbox(self.config, self.engine, vault=self.vault)
            if name == 'publisher_import_preview':
                return inbox.preview(args['channel_id'], args.get('before_id'), args.get('limit',50))
            if name == 'publisher_import_selected':
                return inbox.import_selected(args['channel_id'], args['message_ids'])
        raise UserError('Неизвестный инструмент.')

    def close(self):
        if self.manual:
            self.manual.close()
        self.db.close()


def status(config, db):
    heartbeat = config.data / 'heartbeat.json'
    current = json.loads(heartbeat.read_text()) if heartbeat.exists() else None
    return {'version': config.settings.get('version'), 'installation_id': config.instance, 'data': str(config.data),
            'runtime': str(config.root), 'heartbeat': current,
            'heartbeat_fresh': bool(current and time.time() - current.get('heartbeat',0) < 60),
            'phones': db.all('SELECT * FROM phones'), 'channels': db.all('SELECT * FROM channels'),
            'jobs': db.all('SELECT kind,state,count(*) AS count FROM jobs GROUP BY kind,state'),
            'incidents': db.all("SELECT * FROM incidents WHERE state='open'"),
            'paused': db.setting('paused',False), 'restore_reconciliation_required': db.setting('restore_reconciliation_required',False),
            'deployment': json.loads((config.data/'deployment.json').read_text()) if (config.data/'deployment.json').exists() else None,
            'live_acceptance_verified': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root')
    args = parser.parse_args()
    server = AdminServer(Config(args.root))
    try:
        for line in sys.stdin:
            message = None
            try:
                message = json.loads(line)
                if 'id' not in message:
                    continue
                if message['method'] == 'initialize':
                    from . import __version__
                    result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}}, 'serverInfo': {'name':'android-publisher','version':__version__}}
                elif message['method'] == 'ping':
                    result = {}
                elif message['method'] == 'tools/list':
                    result = {'tools': TOOLS}
                elif message['method'] == 'tools/call':
                    try:
                        value = server.call(message['params']['name'], message['params'].get('arguments',{}))
                        result = value if isinstance(value,dict) and 'content' in value else {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}
                    except (UserError, ValueError, KeyError) as error:
                        result = {'isError':True,'content':[{'type':'text','text':str(error)}]}
                else:
                    raise UserError('Метод MCP не поддерживается.')
                response = {'jsonrpc':'2.0','id':message['id'],'result':result}
            except Exception as error:
                response = {'jsonrpc':'2.0','id':message.get('id') if isinstance(message,dict) else None,'error':{'code':-32603,'message':type(error).__name__}}
            print(json.dumps(response,ensure_ascii=False),flush=True)
    finally:
        server.close()


if __name__ == '__main__':
    main()
