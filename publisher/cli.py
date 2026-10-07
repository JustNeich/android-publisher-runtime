import argparse
import json
import re
import sys
from pathlib import Path
from .config import Config, UserError
from .db import Store
from .engine import Engine
from .install import initialize, bind, doctor, install_service, stop_service
from .phone import Phone, endpoint
from .service import serve
from .vault import Vault


def main():
    parser = argparse.ArgumentParser(description='Android Publisher — управление проектом')
    parser.add_argument('--root')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    sub.add_parser('background-preflight')
    sub.add_parser('keychain-authorize', help='Foreground system access confirmation; never returns stored values')
    sub.add_parser('acceptance-report')
    sub.add_parser('prepare-catalog', help='Prepare the persistent private native catalog; never install a second plugin')
    support = sub.add_parser('send-report')
    support.add_argument('--recipient',required=True,type=int)
    support_read = sub.add_parser('support-inbox')
    support_read.add_argument('--recipient',required=True,type=int)
    support_read.add_argument('--after',default=0,type=int)
    support_read.add_argument('--limit',default=20,type=int)
    forum = sub.add_parser('provision-group')
    forum.add_argument('--channel',required=True)
    forum.add_argument('--title',required=True)
    forum.add_argument('--topic',default='Публикация')
    sub.add_parser('telegram-login')
    private = sub.add_parser('private-pin')
    private.add_argument('--phone', required=True)
    health = sub.add_parser('health')
    health.add_argument('--stop-after', type=float)
    legacy = sub.add_parser('import-legacy')
    legacy.add_argument('--source-root', required=True)
    legacy.add_argument('--source-data')
    restore = sub.add_parser('restore')
    restore.add_argument('--snapshot', required=True)
    update = sub.add_parser('update')
    update.add_argument('--package', required=True)
    sub.add_parser('rollback')
    preview = sub.add_parser('import-preview')
    preview.add_argument('--channel', required=True)
    preview.add_argument('--before', type=int)
    preview.add_argument('--limit', type=int, default=50)
    selected = sub.add_parser('import-videos')
    selected.add_argument('--channel', required=True)
    selected.add_argument('--messages', nargs='+', type=int, required=True)
    retry = sub.add_parser('retry-job')
    retry.add_argument('--id', required=True)
    sub.add_parser('reconcile-telegram')
    sub.add_parser('prepare-adb')
    secret = sub.add_parser('secret-import', help='Codex: private secret from stdin to Keychain; never argv')
    secret.add_argument('--name', required=True)
    provision = sub.add_parser('provision-bot', help='Codex: create/reuse bot and configure privacy')
    provision.add_argument('--username', required=True)
    provision.add_argument('--name', required=True)
    group = sub.add_parser('configure-group')
    group.add_argument('--chat', type=int, required=True)
    verify = sub.add_parser('verify-phone')
    verify.add_argument('--phone', required=True)
    deploy = sub.add_parser('deploy')
    deploy.add_argument('--manifest', required=True)
    usb = sub.add_parser('usb-setup')
    usb.add_argument('--phone', required=True)
    usb.add_argument('--serial')
    sub.add_parser('demo')
    sub.add_parser('doctor')
    sub.add_parser('status')
    sub.add_parser('install-service')
    sub.add_parser('stop')
    sub.add_parser('remove-service')
    sub.add_parser('pause')
    sub.add_parser('resume')
    server_parser = sub.add_parser('serve')
    server_parser.add_argument('--stop-after', type=float)
    probe = sub.add_parser('probe-phone')
    probe.add_argument('--phone', required=True)
    phone_parser = sub.add_parser('add-phone')
    phone_parser.add_argument('--id', required=True)
    phone_parser.add_argument('--name', required=True)
    phone_parser.add_argument('--endpoint', required=True)
    phone_parser.add_argument('--serial', required=True)
    wireless = sub.add_parser('wireless-adb')
    wireless.add_argument('--phone', required=True)
    wireless.add_argument('--address', required=True)
    wireless.add_argument('--adb', required=True)
    binding = sub.add_parser('bind-channel')
    for key in ('phone', 'id', 'name', 'youtube-id', 'chat', 'publication-topic'):
        binding.add_argument('--' + key, required=True)
    binding.add_argument('--general-topic', type=int, default=1)
    binding.add_argument('--timezone', required=True)
    binding.add_argument('--defaults-file')
    binding.add_argument('--times', required=True, nargs='+')
    queue = sub.add_parser('queue')
    queue.add_argument('channel')
    backup = sub.add_parser('backup')
    backup.add_argument('destination')
    receipt = sub.add_parser('resolve-card')
    receipt.add_argument('video')
    receipt.add_argument('--message', required=True, type=int)
    args = parser.parse_args()
    config = Config(args.root)
    try:
        if args.command == 'prepare-catalog':
            from .catalog import prepare
            result = prepare(config)
        elif args.command == 'background-preflight':
            from .preflight import run
            result = run(config)
        elif args.command == 'keychain-authorize':
            from .vault import authorize
            result = authorize(config)
        elif args.command == 'acceptance-report':
            from .acceptance import report
            result = report(config)
        elif args.command == 'send-report':
            from .support import send_report
            result = send_report(config,args.recipient)
        elif args.command == 'support-inbox':
            from .support import read_dialog
            result = read_dialog(config,args.recipient,args.after,args.limit)
        elif args.command == 'provision-group':
            from .provision import provision_group
            result = provision_group(config,args.channel,args.title,args.topic)
        elif args.command == 'telegram-login':
            from .login import login
            result = login(config)
        elif args.command == 'private-pin':
            from .login import private_secret
            result = private_secret(config, 'phone-' + args.phone + '-pin')
        elif args.command == 'health':
            from .health import serve as health_serve
            health_serve(config, args.stop_after)
            return 0
        elif args.command == 'import-legacy':
            from .lifecycle import import_legacy
            result = import_legacy(config, args.source_root, args.source_data)
        elif args.command == 'restore':
            from .lifecycle import restore
            result = restore(config, args.snapshot)
        elif args.command == 'update':
            from .lifecycle import update
            result = update(config, args.package)
        elif args.command == 'rollback':
            from .lifecycle import rollback
            result = rollback(config)
        elif args.command == 'secret-import':
            from .provision import import_secret
            if sys.stdin.isatty():
                raise UserError('Это машинная команда Codex: секрет подаётся по защищённому stdin; помощник ничего не вводит.')
            result = import_secret(config, args.name, sys.stdin.read(8193).rstrip('\n'))
        elif args.command == 'provision-bot':
            from .provision import provision_bot
            result = provision_bot(config, args.username, args.name)
        elif args.command == 'configure-group':
            from .provision import configure_group
            result = configure_group(config, args.chat)
        elif args.command == 'verify-phone':
            from .onboard import verify_phone
            result = verify_phone(config, args.phone)
        elif args.command == 'deploy':
            from .onboard import deploy
            result = deploy(config, json.loads(Path(args.manifest).read_text()))
        elif args.command == 'demo':
            from .demo import simulate
            result = simulate()
        elif args.command == 'probe-phone':
            from .probe import probe_phone
            result = probe_phone(config, args.phone)
        elif args.command == 'prepare-adb':
            from .bootstrap import prepare_adb
            result = {'adb': prepare_adb(config)}
        elif args.command == 'usb-setup':
            from .bootstrap import usb
            result = usb(config, args.phone, args.serial)
        elif args.command == 'init':
            result = initialize(config)
        elif args.command == 'serve':
            serve(config, args.stop_after)
            return 0
        elif args.command == 'doctor':
            result = doctor(config)
        elif args.command == 'install-service':
            result = install_service(config)
        elif args.command in ('stop', 'remove-service'):
            result = stop_service(config, args.command == 'remove-service')
        elif args.command == 'bind-channel':
            result = bind(config, args.phone, args.id, args.name, args.youtube_id, int(args.chat), args.general_topic, int(args.publication_topic), args.timezone, args.times, json.loads(Path(args.defaults_file).read_text()) if args.defaults_file else None)
        else:
            db = Store(config.database)
            engine = Engine(db)
            try:
                if args.command == 'add-phone':
                    if not re.fullmatch(r'[a-z0-9-]{1,40}', args.id):
                        raise UserError('Имя подключения: маленькие латинские буквы, цифры и дефис.')
                    address = endpoint(args.endpoint)
                    old = db.one('SELECT * FROM phones WHERE id=?', (args.id,))
                    if old and old['serial'] != args.serial:
                        raise UserError('Это имя уже связано с другим физическим телефоном.')
                    db.execute('INSERT INTO phones VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,endpoint=excluded.endpoint', (args.id, args.name, address, args.serial, db.clock()))
                    result = {'saved': True, 'phone': args.id, 'next': 'Codex: import authorized token through secret-import stdin, then verify-phone; connection is not verified yet'}
                elif args.command == 'wireless-adb':
                    phone = db.one('SELECT * FROM phones WHERE id=?', (args.phone,))
                    if not phone:
                        raise UserError('Телефон не найден.')
                    previous = dict(config.settings.get('wireless_adb', {}))
                    previous[args.phone] = args.address
                    config.save(adb_binary=str(Path(args.adb).resolve()), wireless_adb=previous)
                    from .adb import WirelessADB
                    WirelessADB(config, phone).resolve()
                    result = {'paired_connection_verified': True, 'phone': args.phone}
                elif args.command in ('pause', 'resume'):
                    if args.command == 'resume' and db.setting('restore_reconciliation_required', False):
                        raise UserError('После восстановления сначала reconcile-telegram и сверка неизвестных эффектов YouTube; публикации остаются на паузе.')
                    db.set('paused', args.command == 'pause')
                    result = {'paused': db.setting('paused')}
                elif args.command == 'queue':
                    result = engine.queue(args.channel)
                elif args.command == 'backup':
                    from .lifecycle import snapshot
                    result = snapshot(config, args.destination)
                elif args.command == 'resolve-card':
                    video = engine.video(args.video)
                    if not video['card_unknown']:
                        raise UserError('Для этой карточки нет неизвестной отправки.')
                    # Exact message must first be read back through the owner's Telegram connector.
                    db.execute('UPDATE videos SET card_message=?,card_unknown=0,card_dirty=1 WHERE id=?', (args.message, args.video))
                    result = {'receipt_bound': True, 'readback_required_before_command': True}
                elif args.command in ('import-preview','import-videos','reconcile-telegram'):
                    from .inbox import Inbox
                    inbox = Inbox(config, engine)
                    if args.command == 'import-preview':
                        result = inbox.preview(args.channel, args.before, args.limit)
                    elif args.command == 'import-videos':
                        result = inbox.import_selected(args.channel, args.messages)
                    else:
                        result = inbox.reconcile_deliveries(config.settings.get('bot_id'))
                        unknown = db.one("SELECT id FROM videos WHERE card_unknown=1 UNION SELECT id FROM outbox WHERE state='unknown' LIMIT 1")
                        if not unknown:
                            db.set('restore_reconciliation_required', False)
                        result['unresolved_delivery'] = bool(unknown)
                elif args.command == 'retry-job':
                    job = db.one("SELECT * FROM jobs WHERE id=? AND state='blocked'", (args.id,))
                    if not job:
                        raise UserError('Остановленное задание не найдено.')
                    db.execute("UPDATE jobs SET state='uncertain',due=?,attempts=0,error=NULL WHERE id=?", (db.clock(), args.id))
                    result = {'retry_queued': True, 'reconcile_first': True}
                elif args.command == 'status':
                    from .admin import status
                    result = status(config, db)
                else:
                    raise UserError('Команда не реализована.')
            finally:
                db.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (UserError, ValueError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
