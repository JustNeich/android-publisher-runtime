"""Consistent backup, import, restore, update and rollback with unknown-effect fencing."""
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import contextmanager, closing
from pathlib import Path
from .config import Config, UserError, write_private
from .db import Store
from .runtime import runtime_home, verify_package, checked


@contextmanager
def stopped_state(config):
    with (config.data / 'lifecycle.lock').open('a+') as lifecycle, (config.data / 'service.lock').open('a+') as lock:
        try:
            fcntl.flock(lifecycle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UserError('Сначала остановите службу и освободите ручной Android. Изменение runtime/восстановление во время работы запрещено.') from None
        db = Store(config.database)
        try:
            if db.one("SELECT phone_id FROM phone_locks WHERE job_id LIKE 'manual-%' AND until>? LIMIT 1", (db.clock(),)):
                raise UserError('Телефон сейчас используется ручным чатом. Сначала android_release.')
        finally:
            db.close()
        yield


def snapshot(config, destination=None):
    target = Path(destination or config.data / 'backups' / ('manual-' + str(time.time_ns()))).resolve()
    if target == config.data or config.data.is_relative_to(target):
        raise UserError('Резервная копия должна находиться в отдельном каталоге.')
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    db = Store(config.database)
    try:
        with db.transaction():
            # SQLite backup from a separate reader avoids waiting on our own write transaction.
            with closing(sqlite3.connect(config.database)) as source, closing(sqlite3.connect(target / 'publisher.sqlite3')) as dest:
                source.backup(dest)
                dest.execute('PRAGMA journal_mode=DELETE')
                if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise UserError('Резервная копия SQLite не прошла проверку.')
            for name in ('settings.json', 'installation.json', 'deployment.json', 'bot-provision.json'):
                if (config.data / name).exists():
                    write_private(target / name, (config.data / name).read_bytes())
    finally:
        db.close()
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir() if p.is_file()}
    write_private(target / 'backup.json', json.dumps({'schema': 1, 'installation': config.instance, 'files': files, 'created': time.time(),
                                                    'keychain_service': config.keychain_service, 'runtime': str(config.root)}, indent=2))
    return {'backup_verified': True, 'directory': str(target), 'keychain_exported': False}


def verify_snapshot(directory):
    directory = Path(directory).resolve()
    record = json.loads((directory / 'backup.json').read_text())
    for name, sha in record['files'].items():
        if name not in ('publisher.sqlite3', 'settings.json', 'installation.json', 'deployment.json', 'bot-provision.json') or hashlib.sha256((directory / name).read_bytes()).hexdigest() != sha:
            raise UserError('Резервная копия повреждена или имеет неизвестный состав.')
    if 'publisher.sqlite3' not in record['files'] or 'installation.json' not in record['files']:
        raise UserError('Неполная резервная копия.')
    with sqlite3.connect('file:' + str(directory / 'publisher.sqlite3') + '?mode=ro', uri=True) as source:
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or source.execute('PRAGMA foreign_key_check').fetchone():
            raise UserError('База резервной копии не прошла проверку связей.')
    return record


def restore(config, directory):
    directory = Path(directory).resolve()
    record = verify_snapshot(directory)
    if record['installation'] != config.instance:
        raise UserError('Снимок относится к другой установке. Перенос активного контура между Mac вне v1.')
    with stopped_state(config):
        preserved = automatic_snapshot(config)
        for name in record['files']:
            if name == 'publisher.sqlite3':
                # SQLite atomically replaces pages in the existing database. Open idle
                # foreground readers keep the same inode and see the restored commit.
                with closing(sqlite3.connect(directory / name)) as source, closing(sqlite3.connect(config.database)) as target:
                    source.backup(target)
            else:
                write_private(config.data / name, (directory / name).read_bytes())
        config.settings = json.loads(config.settings_path.read_text()) if config.settings_path.exists() else {}
        db = Store(config.database)
        try:
            with db.transaction():
                db.set('paused', True); db.set('maintenance', True); db.set('restored_at', db.clock())
                db.execute("UPDATE jobs SET state='uncertain',lease=NULL,lease_until=NULL,due=?,error='Восстановлен снимок; сначала сверка внешнего результата' WHERE state IN ('running','queued','uncertain','blocked') AND kind IN ('upload','publish_now','reschedule','cancel','verify','reconcile')", (db.clock(),))
                db.execute('DELETE FROM phone_locks')
                # A receipt absent from this older snapshot may already exist on Telegram.
                db.execute('UPDATE videos SET card_unknown=1,card_attempted=? WHERE card_message IS NULL', (db.clock(),))
                db.execute("UPDATE outbox SET state='unknown',attempted=? WHERE state IN ('queued','sending')", (db.clock(),))
                db.set('restore_reconciliation_required', True)
        finally:
            db.close()
    return {'restored': True, 'paused': True, 'external_reconciliation_required': True, 'before_restore': preserved['directory']}


def import_legacy(config, source_root, source_data=None):
    source_root = Path(source_root).resolve()
    legacy_id = hashlib.sha256(str(source_root).encode()).hexdigest()[:12]
    source = Path(source_data or Path.home() / 'Library/Application Support/AndroidPublisher' / legacy_id).resolve()
    if not (source / 'publisher.sqlite3').exists() or source == config.data:
        raise UserError('Прежняя очередь не найдена либо выбран текущий каталог.')
    if config.settings.get('legacy_source') == str(source):
        with closing(sqlite3.connect('file:' + str(source / 'publisher.sqlite3') + '?mode=ro', uri=True)) as old:
            marker=old.execute('SELECT value FROM settings WHERE key=?',('superseded_by',)).fetchone()
        if marker and json.loads(marker[0])==str(config.data):
            return {'imported':True,'reused':True,'history_preserved':True,'keychain_retained':True,'verification_required':True}
    with stopped_state(config):
        with (source / 'service.lock').open('a+') as old_lock:
            try:
                fcntl.flock(old_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UserError('Прежний Publisher ещё работает. Сначала его штатная остановка.') from None
            db = Store(config.database)
            try:
                if db.one('SELECT id FROM phones LIMIT 1') or db.one('SELECT id FROM videos LIMIT 1'):
                    raise UserError('Новая установка уже содержит данные; автоматическое объединение запрещено.')
            finally:
                db.close()
            automatic_snapshot(config)
            with sqlite3.connect(source / 'publisher.sqlite3') as old, sqlite3.connect(config.database) as new:
                old.backup(new)
            settings = json.loads((source / 'settings.json').read_text()) if (source / 'settings.json').exists() else {}
            settings.pop('bot_api', None); settings.pop('bot_api_download_root', None)
            for name in ('python', 'codex_binary', 'version'):
                settings.pop(name, None)
            config.save(**settings, keychain_service='org.android-publisher.' + legacy_id,
                        legacy_source=str(source), legacy_root=str(source_root))
            for name in ('deployment.json', 'bot-provision.json'):
                if (source / name).exists():
                    write_private(config.data / name, (source / name).read_bytes())
            new = Store(config.database)
            try:
                from telethon import utils, types
                for video in new.all('SELECT * FROM videos WHERE source_document IS NULL'):
                    try:
                        original = utils.resolve_bot_file_id(video['file_id'])
                    except (ValueError, TypeError, IndexError):
                        original = None
                    if isinstance(original, types.Document):
                        new.execute('UPDATE videos SET source_document=? WHERE id=?', (str(original.id), video['id']))
                    elif not video['sha256']:
                        new.execute('UPDATE videos SET reconciliation_required=1,error=? WHERE id=?', ('Прежний file_id не подтверждает оригинал. До новой загрузки нужен повторно одобренный исходник в Telegram.', video['id']))
                new.set('paused', True); new.set('maintenance', True)
                new.execute("UPDATE jobs SET state='uncertain',lease=NULL,lease_until=NULL WHERE state='running'")
                new.execute('DELETE FROM phone_locks')
                new.set('legacy_migration_pending', True)
            finally:
                new.close()
            # A retained legacy install cannot resume accidentally after the migration.
            with sqlite3.connect(source / 'publisher.sqlite3') as old:
                old.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('paused', 'true'))
                old.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('superseded_by', json.dumps(str(config.data))))
    return {'imported': True, 'paused': True, 'history_preserved': True, 'keychain_retained': True, 'verification_required': True}


def update(config, package):
    package = Path(package).resolve(); verify_package(package)
    from .install import stop_service, doctor
    from .preflight import run as background_preflight
    db = Store(config.database)
    was_paused = db.setting('paused', False)
    db.set('paused', True); db.close()
    stop_service(config)
    backup = automatic_snapshot(config)
    old_active = json.loads((runtime_home() / 'active.json').read_text())
    try:
        # The supplied installer verifies its pinned runtime and exact packaged file set.
        checked(['/bin/sh', str(package / 'install.sh')], timeout=600)
        active = json.loads((runtime_home() / 'active.json').read_text())
        fresh = Config(active['app'])
        Store(fresh.database).close()
        fresh.save(python=active['python'], codex_binary=active['codex'], version=active['version'])
        service = checked([active['python'], '-m', 'publisher.cli', '--root', active['app'], 'install-service'], timeout=45,
                          env={**os.environ, 'PYTHONPATH': active['app'], 'ANDROID_PUBLISHER_DATA': str(config.data)})
        proof = background_preflight(fresh)
        if not proof['passed']:
            raise UserError('Новая версия не прошла фоновую проверку импортов/Keychain/ChatGPT.')
        health = doctor(fresh)
        if not health['core_ready'] or (health['channel_count'] and not health['ready']):
            raise UserError('Новая версия не прошла живую проверку; публикации остаются на паузе.')
        db = Store(fresh.database); db.set('paused', was_paused); db.close()
        return {'updated': True, 'version': active['version'], 'backup': backup['directory'], 'doctor': health}
    except Exception:
        active_file = runtime_home() / 'active.json'
        write_private(active_file, json.dumps(old_active))
        config.save(python=old_active['python'], codex_binary=old_active['codex'], version=old_active['version'])
        # Keep the migrated queue; reverting it would lose decisions made after backup.
        stop_service(config)
        checked([old_active['python'], '-m', 'publisher.cli', '--root', old_active['app'], 'install-service'], timeout=45,
                env={**os.environ, 'PYTHONPATH': old_active['app'], 'ANDROID_PUBLISHER_DATA': str(config.data)})
        db = Store(config.database); db.set('paused', True); db.close()
        raise UserError('Обновление не подтверждено. Прежний runtime возвращён, очередь сохранена на паузе; Codex должен выполнить doctor и сверку. Backup: ' + backup['directory']) from None


def rollback(config):
    path = runtime_home() / 'previous.json'
    if not path.exists():
        raise UserError('Предыдущая совместимая версия runtime не сохранена.')
    previous = json.loads(path.read_text())
    from .install import stop_service
    stop_service(config)
    backup = automatic_snapshot(config)
    db = Store(config.database); db.set('paused', True); db.close()
    checked([previous['python'], '-c', 'from publisher.db import Store; import os; Store(os.environ["ANDROID_PUBLISHER_DATA"]).close()'],
            env={**os.environ, 'PYTHONPATH': previous['app'], 'ANDROID_PUBLISHER_DATA': str(config.database)})
    current = json.loads((runtime_home() / 'active.json').read_text())
    write_private(runtime_home() / 'active.json', json.dumps(previous))
    write_private(path, json.dumps(current))
    config.save(python=previous['python'], codex_binary=previous['codex'], version=previous['version'])
    checked([previous['python'], '-m', 'publisher.cli', '--root', previous['app'], 'install-service'], timeout=45,
            env={**os.environ, 'PYTHONPATH': previous['app'], 'ANDROID_PUBLISHER_DATA': str(config.data)})
    return {'rolled_back': True, 'version': previous['version'], 'queue_preserved': True, 'paused': True, 'backup': backup['directory'], 'doctor_required': True}


def prune_automatic_snapshots(config):
    """Keep seven daily and five lifecycle snapshots; explicit recovery copies stay."""
    for prefix, count in (('daily-', 7), ('automatic-', 5)):
        for old in sorted((config.data / 'backups').glob(prefix + '*'), reverse=True)[count:]:
            if old.is_dir() and not old.is_symlink():
                verify_snapshot(old)
                shutil.rmtree(old)


def automatic_snapshot(config):
    result = snapshot(config, config.data / 'backups' / ('automatic-' + str(time.time_ns())))
    prune_automatic_snapshots(config)
    return result
