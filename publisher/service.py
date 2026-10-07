import fcntl
import hashlib
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from .agent import AgentRunner
from .config import UserError, write_private
from .db import Store
from .engine import Engine
from .telegram import Bot, TelegramLoop, TelegramError
from .vault import Vault
from .maintenance import maintain
from .inbox import Inbox
from .incidents import report


def serve(config, stop_after=None):
    lock_file = (config.data / 'service.lock').open('a+')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise UserError('Публикатор уже запущен; второй экземпляр не создан.') from None
    db = Store(config.database)
    engine = Engine(db)
    vault = Vault(config.keychain_service)
    runner = AgentRunner(config)
    stop = False

    def stopping(*args):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    workers = max(1, min(8, int(config.settings.get('workers', 2))))
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = set()
    transport = None
    bot_owner_lock = None
    failures = 0
    next_transport = 0
    next_inbox = 0
    inbox_failures = 0
    started = time.monotonic()
    state = {'pid': os.getpid(), 'started': time.time(), 'last_useful': None, 'transport': 'not_configured', 'error': None}
    if db.setting('superseded_by'):
        pool.shutdown(wait=True)
        db.close(); lock_file.close()
        raise UserError('Этот прежний контур выведен из эксплуатации. Используйте активный плагин; восстановление выполняется отдельно.')
    engine.recover_leases()
    db.execute("UPDATE outbox SET state='unknown',error='Перезапуск между отправкой и получением подтверждения; нужна сверка' WHERE state='sending'")
    awake = None
    try:
        if sys.platform == 'darwin':
            awake = subprocess.Popen(['/usr/bin/caffeinate','-i','-w',str(os.getpid())],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        while not stop:
            if stop_after is not None and time.monotonic() - started >= stop_after:
                break
            engine.recover_leases()
            try:
                maintain(config, db)
                report(config, db, 'maintenance')
            except (UserError, OSError) as error:
                report(config, db, 'maintenance', 'Резервирование/очистка не завершены: ' + type(error).__name__)
            if config.settings_path.exists():
                config.settings = json.loads(config.settings_path.read_text())
            now = time.time()
            if now >= next_transport:
                try:
                    if transport is None and vault.has('telegram-bot-token'):
                        token = vault.get('telegram-bot-token')
                        if bot_owner_lock is None:
                            bot_owner_lock = polling_lock(token)
                        bot = Bot(token, config.settings.get('bot_api', 'https://api.telegram.org'), config.settings.get('bot_api_download_root'))
                        transport = TelegramLoop(engine, bot)
                        webhook = bot.call('getWebhookInfo')
                        if webhook.get('url'):
                            raise UserError('Этот бот уже имеет webhook. Нужен отдельный бот или явно согласованное переключение.')
                    if transport:
                        before = db.setting('telegram_offset', 0)
                        transport.poll()
                        transport.deliver()
                        if db.setting('telegram_offset', 0) != before:
                            state['last_useful'] = now
                        state.update(transport='connected', error=None)
                        report(config, db, 'telegram')
                    failures = 0
                    next_transport = now + 2
                except (UserError, OSError) as error:
                    failures += 1
                    retry = getattr(error, 'retry_after', None) or min(120, 2 ** min(failures, 7))
                    next_transport = now + retry
                    state.update(transport='degraded', error=str(error)[:250])
                    report(config, db, 'telegram', 'Telegram Bot API недоступен: ' + str(error)[:180])
                    # Reload connection on the next retry, preserving offsets and unknown receipts.
                    transport = None
            if now >= next_inbox:
                try:
                    if vault.has('telegram-provision-session'):
                        inbox = Inbox(config, engine, transport, vault)
                        inbox.poll()
                        if transport:
                            inbox.reconcile_deliveries(transport.me['id'])
                        state.update(inbox='connected')
                    else:
                        state.update(inbox='not_configured')
                    inbox_failures = 0
                    report(config, db, 'inbox')
                    next_inbox = time.time() + 10
                except (UserError, OSError) as error:
                    inbox_failures += 1
                    next_inbox = time.time() + min(300, 5 * 2 ** min(inbox_failures, 6))
                    state.update(inbox='degraded')
                    report(config, db, 'inbox', 'Чтение исходных MP4 Telegram не подтверждено: ' + str(error)[:180])
            pending = {future for future in pending if not future.done()}
            while len(pending) < workers and db.clock() >= db.setting('model_suspended_until',0):
                job = engine.claim()
                if not job:
                    break
                pending.add(pool.submit(runner.run, job))
            state.update(heartbeat=time.time(), workers=len(pending), paused=db.setting('paused', False),
                         queued=db.one("SELECT count(*) AS n FROM jobs WHERE state IN ('queued','uncertain')")['n'],
                         unknown_cards=db.one('SELECT count(*) AS n FROM videos WHERE card_unknown=1')['n'])
            write_private(config.data / 'heartbeat.json', json.dumps(state, ensure_ascii=False))
            # Code timer only; no empty model calls.
            time.sleep(.5)
    finally:
        pool.shutdown(wait=True)
        db.close()
        lock_file.close()
        if bot_owner_lock is not None:
            bot_owner_lock.close()
        if awake:
            if awake.poll() is None:
                awake.terminate()
                try:
                    awake.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    awake.kill()
                    awake.wait()


def polling_lock(token, directory=None):
    """Shared between all project instances on this Mac. Never stores the token."""
    directory = Path(directory or Path.home() / 'Library/Application Support/AndroidPublisher/bot-owners')
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = directory / (hashlib.sha256(token.encode()).hexdigest() + '.lock')
    path.touch(mode=0o600, exist_ok=True)
    stream = path.open('r+')
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise UserError('Этот bot token уже обслуживает другой публикатор на Mac. Codex должен сохранить единственного polling-владельца.') from None
    return stream
