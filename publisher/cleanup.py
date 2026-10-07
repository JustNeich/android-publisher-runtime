"""Cleanup remains possible after a concurrent queue revision, while the lease is ours."""
from .adb import WirelessADB
from .config import UserError
from .phone import Phone
from .vault import Vault


def quiet_fenced(config, db, job):
    lock = db.one('SELECT * FROM phone_locks WHERE job_id=? AND lease=? AND until>?', (job['id'], job['lease'], db.clock()))
    if not lock:
        raise UserError('Очистка телефона не выполнена: право управления уже закончилось.')
    phone = db.one('SELECT * FROM phones WHERE id=?', (lock['phone_id'],))
    failures = []
    try:
        with Phone(phone['endpoint'], Vault(config.keychain_service).get('phone-' + phone['id'] + '-token')) as remote:
            for package in ('com.google.android.apps.youtube.creator', 'com.google.android.youtube', 'com.android.chrome'):
                if remote.call('android_close_app', {'package_id': package}).get('isError'):
                    failures.append('Приложение не закрыто')
            remote.call('android_press_home', {})
    except UserError:
        failures.append('MCP недоступен при закрытии приложений')
    try:
        WirelessADB(config, phone).sleep()
    except UserError:
        failures.append('Погашенный экран не подтверждён')
    if failures:
        raise UserError('; '.join(failures))
    return {'apps_closed': True, 'screen_off': True}
