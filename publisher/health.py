"""Independent small launchd observer; no model calls and no bot update polling."""
import json
import time
from .config import write_private, UserError
from .db import Store
from .engine import Engine
from .incidents import report
from .telegram import Bot, TelegramLoop
from .vault import Vault


def tick(config):
    db = Store(config.database)
    try:
        path = config.data / 'heartbeat.json'
        state = json.loads(path.read_text()) if path.exists() else {}
        fresh = db.clock() - state.get('heartbeat', 0) < 90
        if not db.setting('maintenance', False):
            report(config, db, 'service', None if fresh else 'Фоновая служба Android Publisher не отвечает; launchd перезапускает её, очередь сохранена.')
        result = {'heartbeat': db.clock(), 'service_fresh': fresh, 'observer': 'code_only'}
        write_private(config.data / 'health-heartbeat.json', json.dumps(result))
        vault = Vault(config.keychain_service)
        if vault.has('telegram-bot-token'):
            bot = Bot(vault.get('telegram-bot-token'))
            # The observer owns only incident delivery; atomic claims prevent double sends.
            TelegramLoop(Engine(db), bot).deliver_outbox(incidents_only=True)
        result = {'heartbeat': db.clock(), 'service_fresh': fresh, 'observer': 'code_only'}
        write_private(config.data / 'health-heartbeat.json', json.dumps(result))
        return result
    finally:
        db.close()


def serve(config, stop_after=None):
    started = time.monotonic()
    while stop_after is None or time.monotonic() - started < stop_after:
        try:
            tick(config)
        except (UserError, OSError, ValueError):
            # A down network cannot deliver an alert; the durable incident remains queued.
            pass
        time.sleep(15)
