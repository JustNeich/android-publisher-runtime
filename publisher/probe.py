"""Live phone inspection in a disposable queue; uploads are denied by scoped MCP."""
import json
import os
import tempfile
from pathlib import Path
from .agent import AgentRunner
from .config import Config, UserError
from .db import Store
from .engine import Engine


def probe_phone(config, phone_id):
    real = Store(config.database)
    phone = real.one('SELECT * FROM phones WHERE id=?', (phone_id,))
    if not phone:
        real.close()
        raise UserError('Сначала подключите телефон.')
    from .admin import ManualPhone
    from .vault import Vault
    control = ManualPhone(config, real, phone_id, Vault(config.keychain_service))
    real.execute('UPDATE phone_locks SET until=? WHERE job_id=? AND lease=?', (real.clock()+1800, control.id, control.lease))
    previous = os.environ.get('ANDROID_PUBLISHER_DATA')
    try:
        with tempfile.TemporaryDirectory(prefix='android-publisher-probe-') as temp:
            os.environ['ANDROID_PUBLISHER_DATA'] = temp
            isolated = Config(config.root)
            isolated.save(**{**config.settings, 'keychain_service': config.keychain_service})
            db = Store(isolated.database)
            db.execute('INSERT INTO phones VALUES(?,?,?,?,?)', tuple(phone[k] for k in ('id','name','endpoint','serial','added')))
            db.execute('INSERT INTO channels(id,name,youtube_id,phone_id,chat_id,general_topic,publication_topic,timezone,times,bound_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('inspection','Проверка без публикации','UC'+'x'*22,phone_id,-1,1,2,'Europe/Moscow','["19:00"]',db.clock()))
            engine = Engine(db)
            engine.enqueue('inspection','inspect',args={'instruction':'Это только проверка соединения. Разбуди и разблокируй телефон. Открой YouTube Studio, прочитай экран и укажи название фактически открытого канала. Не меняй канал, видео, настройки, расписание. Потом quiet_phone и finish_job done с evidence_id. Никаких загрузок или публикаций.'})
            job = engine.claim()
            AgentRunner(isolated).run(job)
            current = db.one('SELECT state,error FROM jobs WHERE id=?',(job['id'],))
            run = db.one('SELECT * FROM runs WHERE job_id=?',(job['id'],))
            evidence = db.all('SELECT tool,payload FROM evidence WHERE job_id=?',(job['id'],))
            # Safe diagnostics for CLI failures; raw UI and model event streams remain local.
            stderr = Path(temp)/'runs'/run['id']/'stderr.txt'
            result = {'inspection_only': True, 'external_uploads': 0, 'phone': phone_id, 'state': current['state'], 'error': current['error'],
                      'fresh_screens': len(evidence), 'run': json.loads(run['result']) if run['result'] else None}
            from .vault import Vault
            from .phone import redact
            vault=Vault(config.keychain_service)
            if current['state'] != 'done' and stderr.exists():
                result['diagnostic'] = stderr.read_text()[-1600:]
                answer = stderr.with_name('answer.txt')
                result['answer'] = answer.read_text()[-3000:] if answer.exists() else None
                events = stderr.with_name('events.jsonl')
                result['events'] = [json.loads(line) for line in events.read_text().splitlines() if 'error' in line or 'mcp' in line][-10:] if events.exists() else []
            from .adb import WirelessADB
            adb=WirelessADB(isolated,phone);adb.resolve()
            result['screen_off_verified'] = 'mWakefulness=Asleep' in adb.shell('dumpsys power') or 'mWakefulness=Dozing' in adb.shell('dumpsys power')
            db.close()
            pin_key = 'phone-'+phone_id+'-pin'
            return redact(result, vault.get('phone-'+phone_id+'-token'), *([vault.get(pin_key)] if vault.has(pin_key) else []))
    finally:
        control.close(); real.close()
        if previous is None: os.environ.pop('ANDROID_PUBLISHER_DATA',None)
        else: os.environ['ANDROID_PUBLISHER_DATA']=previous
