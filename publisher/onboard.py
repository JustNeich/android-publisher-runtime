"""Resumable Codex deployment, with an explicit boundary before live acceptance."""
import json
import re
import time
from .adb import WirelessADB
from .bootstrap import run, usb
from .config import UserError, write_private
from .db import Store
from .phone import Phone
from .vault import Vault


def verify_phone(config, phone_id):
    db = Store(config.database)
    manual = None
    try:
        phone = db.one('SELECT * FROM phones WHERE id=?', (phone_id,))
        if not phone:
            raise UserError('Сначала Codex должен выполнить usb-setup.')
        vault = Vault(config.keychain_service)
        from .admin import ManualPhone
        manual = ManualPhone(config, db, phone_id, vault)
        adb = WirelessADB(config, phone)
        adb.resolve()
        devices = run(adb.binary, None, ['devices', '-l'])
        if any(line.split() and line.split()[0] == phone['serial'] for line in devices.splitlines()[1:]):
            raise UserError('USB ещё подключён; приёмка без кабеля не выполнена.')
        usb_state = adb.shell('dumpsys usb')
        matches = re.findall(r'\bmConnected=(true|false)', usb_state)
        if not matches or any(value == 'true' for value in matches):
            raise UserError('Android не подтвердил физическое отключение USB.')
        adb.wake()
        try:
            secure = adb.secure()
            if secure and not vault.has('phone-'+phone_id+'-pin'):
                raise UserError('Защищённая блокировка требует разрешённого секрета для фоновой работы. Codex должен решить доступ; ручную форму помощнику не назначаем.')
        finally:
            adb.sleep()
        with Phone(phone['endpoint'], vault.get('phone-'+phone_id+'-token')) as remote:
            if remote.call('android_get_screen_state', {}).get('isError'):
                raise UserError('MCP не отвечает с погашенным экраном.')
        result = {'phone': phone_id, 'serial': phone['serial'], 'checked_at': time.time(),
                  'usb_disconnected_verified': True, 'wifi_adb_verified': True,
                  'mcp_with_screen_off_verified': True, 'secure_lock': secure,
                  'studio_inspection_required': True}
        write_private(config.data / 'onboarding' / phone_id / 'wifi-proof.json', json.dumps(result))
        return result
    finally:
        if manual:
            manual.close()
        db.close()


def validate_manifest(plan):
    # No secrets, executable commands, paths, or invented implicit destinations.
    required = {'phone', 'channel', 'name', 'youtube_id', 'chat_id', 'publication_topic',
                'timezone', 'times', 'bot_username', 'bot_name'}
    optional = {'serial', 'general_topic', 'defaults'}
    if not isinstance(plan, dict) or not required <= plan.keys() or plan.keys() - required - optional:
        raise UserError('Deployment manifest должен содержать только предусмотренные несекретные поля; см. docs/setup.md.')
    if not re.fullmatch(r'[a-z0-9-]{1,40}', plan['phone']) or not re.fullmatch(r'[a-z0-9-]{1,40}', plan['channel']):
        raise UserError('Неверное имя телефона или канала.')
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', plan['youtube_id']):
        raise UserError('Codex должен установить точный YouTube ID до развёртывания.')
    if not isinstance(plan['chat_id'], int) or not str(plan['chat_id']).startswith('-100'):
        raise UserError('Нужен точный ID существующей Telegram-группы.')
    if not isinstance(plan['publication_topic'], int) or plan['publication_topic'] <= 1 or plan.get('general_topic', 1) != 1:
        raise UserError('Укажите существующий топик публикации и General 1.')
    from .schedule import validate
    validate(plan['timezone'], plan['times'])
    return plan


def deploy(config, plan):
    """Repeat verified operations; never trust a previous completed-step flag."""
    from .install import initialize, bind, install_service, doctor
    from .provision import provision_bot, configure_group
    plan = validate_manifest(plan)
    path = config.data / 'deployments' / plan['channel'] / 'deployment.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    if previous.get('plan') and previous['plan'] != plan:
        raise UserError('Существует другой deployment manifest; Codex должен сверить миграцию.')
    record = {'plan': plan, 'state': 'configuring', 'steps': previous.get('steps', {})}
    def step(name, operation):
        record['current_step'] = name
        write_private(path, json.dumps(record, ensure_ascii=False))
        try:
            result = operation()
        except UserError as error:
            record.update(state='blocked', reason=str(error))
            write_private(path, json.dumps(record, ensure_ascii=False))
            raise
        record['steps'][name] = result
        write_private(path, json.dumps(record, ensure_ascii=False))
        return result
    step('runtime', lambda: initialize(config))
    from .preflight import run as background_preflight
    step('background_runtime', lambda: background_preflight(config))
    db = Store(config.database)
    try:
        phone = db.one('SELECT * FROM phones WHERE id=?', (plan['phone'],))
        if not previous and not db.all('SELECT id FROM channels'):
            db.set('paused', True)  # service can receive, but acceptance cannot publish
    finally:
        db.close()
    if not phone or plan['phone'] not in config.settings.get('wireless_adb', {}):
        step('usb', lambda: usb(config, plan['phone'], plan.get('serial')))
    step('phone_without_usb', lambda: verify_phone(config, plan['phone']))
    step('bot', lambda: provision_bot(config, plan['bot_username'], plan['bot_name']))
    step('group', lambda: configure_group(config, plan['chat_id']))
    from .inbox import Inbox
    from .engine import Engine
    def user_transport():
        db = Store(config.database)
        try:
            return Inbox(config, Engine(db)).verify()
        finally:
            db.close()
    step('large_video_transport', user_transport)
    step('binding', lambda: bind(config, plan['phone'], plan['channel'], plan['name'], plan['youtube_id'],
                                plan['chat_id'], plan.get('general_topic', 1), plan['publication_topic'], plan['timezone'], plan['times'], plan.get('defaults')))
    step('service', lambda: install_service(config))
    health = step('doctor', lambda: doctor(config))
    record.update(state='pending_live_acceptance' if health['ready'] else 'blocked_health', current_step=None)
    write_private(path, json.dumps(record, ensure_ascii=False))
    return {'state': record['state'], 'doctor': health, 'configured_by': 'Codex',
            'live_acceptance_verified': False, 'publication_performed': False,
            'remaining': ['probe-phone: fresh Studio identity and unlock', 'approved MP4, buttons and General',
                          'approved schedule saved in YouTube', 'service recovery and screen off', 'public release only for specifically approved MP4']}
