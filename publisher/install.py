import json
import fcntl
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from .config import UserError, write_private
from .db import Store
from .engine import Engine
from .phone import Phone, endpoint
from .schedule import validate
from .telegram import Bot
from .vault import Vault
from . import __version__


def initialize(config):
    Store(config.database).close()
    codex = config.settings.get('codex_binary') or shutil.which('codex')
    if not codex:
        raise UserError('Codex CLI не найден. Установите официальный CLI и войдите с ChatGPT.')
    result = subprocess.run([codex, 'login', 'status'], capture_output=True, text=True, timeout=30)
    if result.returncode or 'ChatGPT' not in result.stdout + result.stderr:
        raise UserError('Для фоновой работы нужен вход Codex CLI с ChatGPT: codex login.')
    config.save(codex_binary=codex, python=sys.executable, version=__version__)
    return {'initialized': True, 'data': str(config.data), 'auth': 'ChatGPT', 'python': sys.executable}


def bind(config, phone_id, channel_id, name, youtube_id, chat_id, general_topic, publication_topic, timezone_name, times, defaults=None):
    import re
    times = validate(timezone_name, times)
    from .engine import clean_metadata
    defaults = clean_metadata(defaults or {}, complete=False)
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', youtube_id):
        raise UserError('Нужен точный YouTube channel ID, начинающийся с UC.')
    if general_topic == publication_topic:
        raise UserError('General и публикация должны быть разными топиками.')
    db = Store(config.database)
    try:
        phone = db.one('SELECT * FROM phones WHERE id=?', (phone_id,))
        if not phone:
            raise UserError('Сначала подключите телефон.')
        conflict = db.one('SELECT id FROM channels WHERE id<>? AND (phone_id=? OR youtube_id=? OR chat_id=?)', (channel_id, phone_id, youtube_id, int(chat_id)))
        if conflict:
            raise UserError('Телефон, YouTube-канал или группа уже входят в связку ' + conflict['id'] + '. Нужна явная миграция, в v1 автоматическое объединение запрещено.')
        vault = Vault(config.keychain_service)
        bot = Bot(vault.get('telegram-bot-token'), config.settings.get('bot_api', 'https://api.telegram.org'), config.settings.get('bot_api_download_root'))
        me, chat = bot.call('getMe'), bot.call('getChat', {'chat_id': chat_id})
        if not chat.get('is_forum'):
            raise UserError('Группа должна поддерживать топики.')
        if not bot.member(chat_id, me['id']):
            raise UserError('Бот ещё не добавлен в эту группу.')
        existing = db.one('SELECT * FROM channels WHERE id=?', (channel_id,))
        values = (name, youtube_id, phone_id, int(chat_id), int(general_topic), int(publication_topic), timezone_name, json.dumps(times))
        if existing:
            if (existing['phone_id'], existing['chat_id'], existing['youtube_id']) != (phone_id, int(chat_id), youtube_id):
                raise UserError('Привязка уже существует с другими устройством, группой или каналом; нужна явная миграция.')
            # Rerun setup preserves queue, pause, initial event boundary and existing schedule.
            return {'already_bound': True, 'channel': existing}
        with db.transaction():
            db.execute('INSERT INTO channels(id,name,youtube_id,phone_id,chat_id,general_topic,publication_topic,timezone,times,bound_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (channel_id, *values, db.clock()))
            db.execute('UPDATE channels SET defaults=? WHERE id=?', (json.dumps(defaults), channel_id))
            db.history(channel_id, 'bound', {'phone_id': phone_id, 'chat_id': chat_id, 'publication_topic': publication_topic})
        return {'bound': True, 'channel': channel_id, 'bot': me['username'], 'live_phone_identity_check_required': True, 'history_import': 'offer selection with import-preview'}
    finally:
        db.close()


def install_service(config):
    if sys.platform != 'darwin':
        raise UserError('Установка фоновой службы поддерживается на macOS.')
    label = 'org.androidpublisher.' + config.instance
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    logdir = config.data / 'logs'
    logdir.mkdir(exist_ok=True, mode=0o700)
    spec = {'Label': label, 'ProgramArguments': [sys.executable, '-m', 'publisher.cli', '--root', str(config.root), 'serve'],
            'WorkingDirectory': str(config.root), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
            'ProcessType': 'Background', 'ExitTimeOut': 1500,
            'EnvironmentVariables': {'PYTHONPATH': str(config.root), 'ANDROID_PUBLISHER_DATA': str(config.data), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
            'StandardOutPath': str(logdir / 'service.log'), 'StandardErrorPath': str(logdir / 'service-error.log')}
    domain = f'gui/{os.getuid()}'
    present = subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True).returncode == 0
    raw = plistlib.dumps(spec)
    if present and path.exists() and path.read_bytes() != raw:
        raise UserError('Служба уже работает с другой конфигурацией. Сначала остановите её штатной командой.')
    write_private(path, raw)
    db = Store(config.database); db.set('maintenance', False); db.close()
    started = time.time()
    if not present:
        result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True)
        if result.returncode:
            raise UserError('launchd не принял службу; проверьте вход пользователя и разрешения.')
    for _ in range(30):
        heartbeat = config.data / 'heartbeat.json'
        if heartbeat.exists():
            current = json.loads(heartbeat.read_text())
            if current.get('heartbeat', 0) > started - 3:
                install_observer(config)
                return {'installed': True, 'label': label, 'heartbeat': current, 'lifecycle': 'after_user_login'}
        time.sleep(.3)
    raise UserError('Job создан, но свежий прикладной heartbeat не получен; службу ещё нельзя считать рабочей.')


def stop_service(config, remove=False):
    label = 'org.androidpublisher.' + config.instance
    db = Store(config.database)
    db.set('paused', True)
    db.set('maintenance', True)
    db.close()
    target = f'gui/{os.getuid()}/{label}'
    subprocess.run(['launchctl', 'bootout', target], capture_output=True, timeout=1550)
    health_label = label + '.health'
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{health_label}'], capture_output=True, timeout=30)
    # launchctl may return before the process releases its application lock.
    with (config.data / 'service.lock').open('a+') as lock:
        for _ in range(100):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(.2)
        else:
            raise UserError('Служба ещё завершает операцию. Обновление не начато; продолжите после сверки активного задания.')
        db = Store(config.database)
        try:
            with db.transaction():
                db.execute("UPDATE jobs SET state='uncertain',lease=NULL,lease_until=NULL,due=?,error='Исполнитель остановлен; сначала сверка результата' WHERE state='running'", (db.clock(),))
                db.execute("DELETE FROM phone_locks WHERE job_id NOT LIKE 'manual-%'")
        finally:
            db.close()
    if remove:
        (Path.home() / 'Library/LaunchAgents' / (health_label + '.plist')).unlink(missing_ok=True)
        (Path.home() / 'Library/LaunchAgents' / (label + '.plist')).unlink(missing_ok=True)
    return {'stopped': True, 'data_preserved': True}


def doctor(config):
    checks = []
    db = Store(config.database)
    try:
        checks.append({'name': 'database', 'ok': db.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'})
        proof = config.settings.get('background_preflight', {})
        checks.append({'name': 'background_preflight', 'ok': bool(proof.get('passed') and proof.get('runtime') == str(config.root))})
        codex = config.settings.get('codex_binary', shutil.which('codex'))
        try:
            result = subprocess.run([codex, 'login', 'status'], capture_output=True, text=True, timeout=15) if codex else None
            checks.append({'name': 'codex_chatgpt', 'ok': bool(result and result.returncode == 0 and 'ChatGPT' in result.stdout + result.stderr)})
        except (OSError, subprocess.TimeoutExpired):
            checks.append({'name': 'codex_chatgpt', 'ok': False})
        vault = Vault(config.keychain_service)
        try:
            bot = Bot(vault.get('telegram-bot-token'), config.settings.get('bot_api', 'https://api.telegram.org'), config.settings.get('bot_api_download_root'))
            me = bot.call('getMe')
            checks.append({'name': 'telegram_bot', 'ok': bool(me.get('can_read_all_group_messages')), 'username': me['username'], 'reads_group_messages': bool(me.get('can_read_all_group_messages'))})
        except UserError as error:
            checks.append({'name': 'telegram_bot', 'ok': False, 'reason': str(error)})
        for phone in db.all('SELECT * FROM phones'):
            try:
                with Phone(phone['endpoint'], vault.get('phone-' + phone['id'] + '-token')) as remote:
                    state = remote.call('android_get_screen_state', {})
                    if state.get('isError'):
                        raise UserError('Accessibility не готова.')
                checks.append({'name': 'phone:' + phone['id'], 'ok': True, 'transport': 'LAN'})
            except UserError as error:
                checks.append({'name': 'phone:' + phone['id'], 'ok': False, 'reason': str(error)})
            from .adb import WirelessADB
            try:
                adb = WirelessADB(config, phone)
                adb.resolve()
                secure = adb.secure()
                checks.append({'name': 'unlock:' + phone['id'], 'ok': not secure or vault.has('phone-' + phone['id'] + '-pin'), 'secure_lock': secure})
                checks.append({'name': 'screen_control:' + phone['id'], 'ok': True})
            except UserError as error:
                checks.append({'name': 'screen_control:' + phone['id'], 'ok': False, 'reason': str(error)})
        try:
            from .inbox import Inbox
            access = Inbox(config, Engine(db), vault=vault).verify()
            checks.append({'name': 'telegram_user', 'ok': True, **access})
        except UserError as error:
            checks.append({'name': 'telegram_user', 'ok': False, 'reason': str(error)})
        channel_count = db.one('SELECT count(*) AS n FROM channels')['n']
        checks.append({'name': 'channels', 'ok': bool(channel_count)})
        checks.append({'name': 'uncertain_delivery', 'ok': not db.one("SELECT id FROM videos WHERE card_unknown=1 UNION SELECT id FROM outbox WHERE state IN ('sending','unknown') LIMIT 1")})
        heartbeat_path = config.data / 'heartbeat.json'
        heartbeat = json.loads(heartbeat_path.read_text()) if heartbeat_path.exists() else {}
        checks.append({'name': 'service_process', 'ok': time.time() - heartbeat.get('heartbeat', 0) < 30})
        checks.append({'name': 'service', 'ok': time.time() - heartbeat.get('heartbeat', 0) < 30 and heartbeat.get('transport') == 'connected' and heartbeat.get('inbox') == 'connected', 'transport': heartbeat.get('transport')})
        checks.append({'name': 'observer', 'ok': observer_fresh(config)})
        checks.append({'name': 'restore_reconciliation', 'ok': not db.setting('restore_reconciliation_required',False)})
        core = {'database', 'codex_chatgpt', 'background_preflight', 'service_process', 'observer', 'restore_reconciliation'}
        return {'ready': all(c['ok'] for c in checks), 'core_ready': all(c['ok'] for c in checks if c['name'] in core), 'channel_count': channel_count,
                'checks': checks, 'data': str(config.data), 'paused': db.setting('paused', False)}
    finally:
        db.close()


def install_observer(config):
    label = 'org.androidpublisher.' + config.instance + '.health'
    path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    spec = {'Label': label, 'ProgramArguments': [config.settings.get('python', sys.executable), '-m', 'publisher.cli', '--root', str(config.root), 'health'],
            'WorkingDirectory': str(config.root), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30, 'ProcessType': 'Background',
            'EnvironmentVariables': {'PYTHONPATH': str(config.root), 'ANDROID_PUBLISHER_DATA': str(config.data)},
            'StandardOutPath': str(config.data / 'logs/health.log'), 'StandardErrorPath': str(config.data / 'logs/health-error.log')}
    domain = f'gui/{os.getuid()}'
    raw = plistlib.dumps(spec)
    present = subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True).returncode == 0
    if present and (not path.exists() or path.read_bytes() != raw):
        subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True,timeout=30)
        present = False
    write_private(path, raw)
    if not present and subprocess.run(['launchctl','bootstrap',domain,str(path)],capture_output=True).returncode:
        raise UserError('Наблюдатель launchd не запустился; установка ещё не проверена.')


def observer_fresh(config):
    path = config.data / 'health-heartbeat.json'
    try:
        return time.time() - json.loads(path.read_text()).get('heartbeat',0) < 60
    except (OSError,ValueError):
        return False
