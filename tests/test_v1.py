import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from publisher.config import Config, UserError
from publisher.db import Store
from publisher.engine import Engine
from publisher.admin import ManualPhone
from publisher.inbox import Inbox, normalize
from publisher.incidents import report
from publisher.lifecycle import snapshot, restore, import_legacy, verify_snapshot
from publisher.login import app_keys
from publisher.login import verify_identity
from publisher.support import send_report, read_dialog
from publisher.provision import create_forum
from publisher.media import validate_shorts
from publisher.mcp import Server
from publisher.phone import redact
from publisher.telegram import TelegramLoop


class Vault:
    def __init__(self, *args): self.values = {}
    def has(self, key): return key in self.values
    def get(self, key): return self.values[key]
    def put(self, key, value): self.values[key] = value


class Bot:
    def __init__(self): self.calls=[]
    def call(self, method, data=None, **kwargs):
        self.calls.append((method,data))
        if method == 'getMe': return {'id':77,'username':'sample_bot'}
        return {'message_id':500+len(self.calls)} if method == 'sendMessage' else True
    def member(self,*args): return True


def source(mid, sender=1, bot=False, topic=5, caption=None, document=123, out=False):
    async def get_sender(): return NS(bot=bot,id=sender)
    return NS(id=mid, sender_id=sender, date=datetime.fromtimestamp(1000, timezone.utc), out=out,
              raw_text=caption or 'Название: Clip\nОписание: \nТеги: \nАудитория: not_kids', edit_date=None,
              document=NS(id=document, mime_type='video/mp4',size=1,attributes=[NS(file_name='clip.mp4')]) if document else None,
              reply_to=NS(reply_to_top_id=topic,reply_to_msg_id=topic),get_sender=get_sender,buttons=[])


class Client:
    def __init__(self,messages): self.messages=messages;self.fail_after=None
    def iter_messages(self,chat,**kwargs):
        async def iterator():
            messages=[m for m in self.messages if m.id > kwargs.get('min_id',0)]
            messages.sort(key=lambda m:m.id,reverse=not kwargs.get('reverse',False))
            for i,m in enumerate(messages[:kwargs.get('limit',500)]):
                if kwargs.get('reverse') and self.fail_after is not None and i >= self.fail_after: raise UserError('network')
                yield m
        return iterator()
    async def get_messages(self,chat,ids):
        values=[next((m for m in self.messages if m.id==mid),None) for mid in (ids if isinstance(ids,list) else [ids])]
        return values if isinstance(ids,list) else values[0]
    async def download_media(self,message,file): file.write(b'x')
    async def get_me(self): return NS(id=1)
    async def get_input_entity(self,chat): return chat


class V1(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name)/'data'
        self.environment=patch.dict(os.environ,{'ANDROID_PUBLISHER_DATA':str(self.data)})
        self.environment.start();self.addCleanup(self.environment.stop)
        self.config=Config(Path(self.temp.name)/'plugin-v1')
        self.now=1000
        self.db=Store(self.config.database,lambda:self.now);self.addCleanup(self.db.close)
        self.engine=Engine(self.db);self.vault=Vault();self.bot=Bot()
        self.channel('p','c',-100)

    def channel(self,phone,channel,chat):
        self.db.execute('INSERT INTO phones VALUES(?,?,?,?,?)',(phone,phone,'http://192.168.1.2:8080/mcp',phone,self.now))
        self.db.execute('INSERT INTO channels(id,name,youtube_id,phone_id,chat_id,general_topic,publication_topic,timezone,times,bound_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (channel,channel,'UC'+hashlib.sha256(phone.encode()).hexdigest()[:22],phone,chat,1,5,'UTC','["12:00","13:00"]',0))

    def ingest(self,n=10,channel='c',**kwargs):
        message=source(n,**kwargs)
        data=normalize(message,self.engine.channel(channel)['chat_id']);data['from']['is_bot']=kwargs.get('bot',False)
        return self.engine.ingest(channel,data)

    def inbox(self,client):
        inbox=Inbox(self.config,self.engine,vault=self.vault)
        inbox.run=lambda op:asyncio.run(op(client))
        return inbox

    def cleanup_video(self):
        video = self.engine.video(self.ingest())
        self.db.execute("UPDATE videos SET state='scheduled',youtube_video_id='abcdefghijk',device_path='owned' WHERE id=?",(video['id'],))
        path = self.config.data / 'media' / (video['id'] + '.mp4')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'original')
        self.db.execute('UPDATE videos SET path=? WHERE id=?',(str(path),video['id']))
        return video, path

    def test_cleanup_waits_for_the_shared_phone_lease(self):
        from publisher.retention import clean_media
        video, path = self.cleanup_video()
        guard = ManualPhone(self.config,self.db,'p',self.vault)
        try:
            with patch('publisher.retention.WirelessADB') as adb:
                result = clean_media(self.config,self.db,video['id'])
                adb.assert_not_called()
            self.assertEqual(result['pending'],['phone'])
            self.assertFalse(path.exists())
            self.assertEqual(self.db.one('SELECT job_id FROM phone_locks')['job_id'],guard.id)
        finally:
            guard.close()

    def test_cleanup_failure_releases_its_phone_and_lifecycle_leases(self):
        from publisher.retention import clean_media
        video, path = self.cleanup_video()
        def failed(*args):
            with self.assertRaisesRegex(UserError,'занят'):
                ManualPhone(self.config,self.db,'p',self.vault)
            raise UserError('offline')
        with patch('publisher.retention.WirelessADB') as adb:
            adb.return_value.remove_media.side_effect = failed
            result = clean_media(self.config,self.db,video['id'])
        self.assertEqual(result['pending'],['phone'])
        self.assertIsNone(self.db.one('SELECT * FROM phone_locks'))
        with (self.config.data/'lifecycle.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_cleanup_adb_resolution_does_not_scan_neighbors(self):
        from publisher.adb import WirelessADB
        self.config.save(adb_binary='adb',wireless_adb={'p':'192.168.1.2:5555'})
        adb = WirelessADB(self.config,self.db.one('SELECT * FROM phones'))
        def command(args,**kwargs):
            self.assertNotIn('mdns',args)
            return 'p' if 'getprop' in args else 'connected'
        with patch.object(adb,'run',side_effect=command),patch('publisher.adb.subprocess.run') as scan:
            self.assertEqual(adb.resolve(discover=False),'192.168.1.2:5555')
            scan.assert_not_called()
    def approve(self,video):
        return self.engine.decision(video,'yes',self.engine.video(video)['version'],1)

    def test_identity_survives_plugin_path_and_settings_writers(self):
        second=Config(Path(self.temp.name)/'plugin-v2')
        self.assertEqual(second.instance,self.config.instance)
        self.config.save(wireless_adb={'p':'192.168.1.2:5555'},a=1)
        second.save(wireless_adb={'q':'192.168.1.3:5555'},b=2)
        third=Config(Path(self.temp.name)/'plugin-v3')
        self.assertEqual(third.settings['wireless_adb'],{'p':'192.168.1.2:5555','q':'192.168.1.3:5555'})
        self.assertEqual(third.settings['a'],1)

    def test_incomplete_metadata_waits_near_its_video(self):
        video=self.ingest(caption='Название: Only title')
        self.approve(video)
        job=self.engine.claim();self.assertEqual(job['kind'],'metadata')
        server=Server(self.config,{'job_id':job['id'],'lease':job['lease']},self.vault)
        server.db.clock=lambda:self.now
        try: server.call('reply',{'text':'Для какой аудитории этот ролик? Укажите также описание и теги.'})
        finally: server.close()
        out=self.db.one('SELECT * FROM outbox')
        self.assertEqual(out['topic_id'],5);self.assertEqual(out['reply_to'],10)
        TelegramLoop(self.engine,self.bot).deliver_outbox()
        payload=next(d for m,d in self.bot.calls if m=='sendMessage')
        self.assertEqual(payload['message_thread_id'],5)

    def test_metadata_response_scoped_and_deduplicated(self):
        video=self.ingest(caption='Название: Only title')
        tg=TelegramLoop(self.engine,self.bot)
        message={'message_id':20,'date':1000,'chat':{'id':-100},'message_thread_id':5,'from':{'id':1},'text':'Описание пустое, теги пустые, не для детей','reply_to_message':{'message_id':10}}
        tg.process({'message':message});tg.process({'message':message})
        jobs=self.db.all("SELECT * FROM jobs WHERE kind='general'")
        self.assertEqual(len(jobs),1);self.assertEqual(json.loads(jobs[0]['args'])['video_id'],video)

    def test_bot_and_operator_media_and_cursor_resume(self):
        a,b=source(10,bot=True,sender=999),source(11,sender=1,out=True,document=124)
        client=Client([a,b]);client.fail_after=1
        with self.assertRaises(UserError):self.inbox(client).poll()
        self.assertEqual(self.db.one('SELECT * FROM ingress')['cursor'],10)
        client.fail_after=None;self.inbox(client).poll();self.inbox(client).poll()
        videos=self.engine.queue('c');self.assertEqual(len(videos),2)
        self.assertTrue(videos[0]['source_sender_bot']);self.assertTrue(videos[1]['source_outgoing'])

    def test_import_needs_selected_existing_topic(self):
        self.db.execute('UPDATE channels SET bound_at=2000')
        inbox=self.inbox(Client([source(10),source(11,topic=8)]))
        with self.assertRaises(UserError):inbox.import_selected('c',[10,11])
        self.assertEqual(len(self.engine.queue('c')),0)
        self.assertEqual(len(inbox.import_selected('c',[10])['imported']),1)
        self.assertEqual(self.engine.queue('c')[0]['state'],'review')

    def test_binding_boundary_keeps_arrivals_during_setup(self):
        self.db.execute('UPDATE channels SET bound_at=1500')
        old, arrival = source(10), source(11, document=124)
        arrival.date = datetime.fromtimestamp(1501, timezone.utc)
        self.inbox(Client([old, arrival])).poll()
        self.assertEqual(self.db.one('SELECT boundary FROM ingress')['boundary'], 10)
        self.assertEqual([v['source_message'] for v in self.engine.queue('c')], [11])

    def test_new_session_resolves_channel_from_dialog_inventory(self):
        from telethon import types
        class EmptySession(Client):
            async def get_input_entity(self, chat): raise ValueError('no entity cache')
            async def iter_dialogs(self):
                yield NS(entity=types.PeerChannel(123), input_entity=types.InputPeerChannel(123, 456))
        inbox = self.inbox(EmptySession([]))
        peer = asyncio.run(inbox.peer(EmptySession([]), -1000000000123))
        self.assertEqual((peer.channel_id, peer.access_hash), (123, 456))
        class CachedSession(Client):
            async def get_input_entity(self, chat): raise AssertionError('must use verified private cache')
        self.assertEqual(asyncio.run(inbox.peer(CachedSession([]), -1000000000123)).access_hash, 456)

    def test_changed_original_is_never_downloaded(self):
        video=self.ingest();inbox=self.inbox(Client([source(10,document=999)]))
        with self.assertRaises(UserError):inbox.download(self.engine.video(video),self.data/'media/x.mp4')
        self.assertFalse((self.data/'media/x.mp4').exists())

    def test_download_original_size_and_hash(self):
        video=self.ingest();target=self.data/'media/x.mp4'
        sha=self.inbox(Client([source(10)])).download(self.engine.video(video),target)
        self.assertEqual(sha,hashlib.sha256(b'x').hexdigest());self.assertEqual(target.read_bytes(),b'x')

    def test_large_rejected_before_download(self):
        video=self.ingest();self.db.execute('UPDATE videos SET file_size=?',(2*1024**3+1,))
        with self.assertRaises(UserError):self.inbox(Client([])).download(self.engine.video(video),self.data/'x.mp4')

    def test_oversized_original_never_occupies_slot_or_starts_model(self):
        video=self.ingest();self.db.execute('UPDATE videos SET file_size=?',(2*1024**3+1,))
        with self.assertRaises(UserError):self.approve(video)
        self.assertIsNone(self.engine.video(video)['slot']);self.assertIsNone(self.engine.claim())

    def test_legacy_import_preserves_decisions_history_and_is_repeatable(self):
        video=self.ingest();self.approve(video)
        source_data=self.data
        target=Path(self.temp.name)/'new-state'
        with patch.dict(os.environ,{'ANDROID_PUBLISHER_DATA':str(target)}):fresh=Config(Path(self.temp.name)/'new-plugin')
        source_root=Path(self.temp.name)/'old-plugin'
        result=import_legacy(fresh,source_root,source_data)
        self.assertTrue(result['history_preserved'])
        migrated=Store(fresh.database)
        try:
            self.assertEqual(migrated.one('SELECT state FROM videos')['state'],'pending')
            self.assertTrue(migrated.setting('paused'));self.assertGreater(len(migrated.all('SELECT * FROM history')),0)
        finally:migrated.close()
        self.assertTrue(import_legacy(fresh,source_root,source_data)['reused'])

    def test_manual_and_background_exclusion_and_stale_client(self):
        video=self.ingest();self.approve(video)
        manual=ManualPhone(self.config,self.db,'p',self.vault)
        try:
            self.assertIsNone(self.engine.claim())
            with self.assertRaises(UserError):ManualPhone(self.config,self.db,'p',self.vault)
            self.now+=601;self.engine.recover_leases();job=self.engine.claim()
            self.assertEqual(job['kind'],'upload')
            with self.assertRaises(UserError):manual.need_phone()
        finally:manual.close()
        self.assertTrue(self.db.one('SELECT * FROM phone_locks WHERE job_id=?',(job['id'],)))

    def test_incident_destination_and_no_timer_spam(self):
        self.config.save(operator_chat=1)
        report(self.config,self.db,'network','Internet down')
        report(self.config,self.db,'network','Internet down')
        report(self.config,self.db,'phone:p','Phone down','c')
        report(self.config,self.db,'network')
        items=self.db.all('SELECT * FROM outbox ORDER BY created')
        self.assertEqual(len(items),3);self.assertEqual(items[0]['chat_id'],1)
        self.assertEqual(items[1]['chat_id'],-100);self.assertEqual(items[1]['topic_id'],1)

    def test_backup_restore_fences_remote_effects_and_keeps_identity(self):
        video=self.ingest();self.approve(video);self.engine.claim()
        location=snapshot(self.config)['directory'];record=verify_snapshot(location)
        self.assertEqual(record['installation'],self.config.instance)
        result=restore(self.config,location)
        self.assertTrue(result['paused']);self.assertTrue(self.db.setting('restore_reconciliation_required'))
        # Reopen after atomic replacement; an existing connection deliberately keeps old inode.
        with sqlite3.connect(self.config.database) as restored:
            self.assertEqual(restored.execute('SELECT state FROM jobs').fetchone()[0],'uncertain')

    def test_corrupt_backup_refused_without_changing_queue(self):
        video=self.ingest();location=Path(snapshot(self.config)['directory'])
        (location/'publisher.sqlite3').write_bytes(b'bad')
        with self.assertRaises(UserError):restore(self.config,location)
        self.assertEqual(self.engine.video(video)['state'],'review')

    def test_restore_refuses_running_service(self):
        location=snapshot(self.config)['directory']
        with (self.data/'service.lock').open('a+') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(UserError):restore(self.config,location)

    def test_replayed_source_after_old_snapshot_reconciles_first(self):
        self.db.set('restored_at',1500)
        video=self.ingest();self.approve(video);job=self.engine.claim()
        self.assertTrue(job['reconcile_first']);self.assertTrue(self.engine.video(video)['card_unknown'])

    def test_unknown_card_recovered_by_author_source_and_markup(self):
        video=self.ingest();self.db.execute('UPDATE videos SET card_unknown=1,card_attempted=?',(self.now,))
        message=source(20,sender=77,document=None)
        message.reply_to.reply_to_msg_id=10;message.buttons=[[NS(data=(video+':yes:0').encode())]]
        self.inbox(Client([source(10),message])).reconcile_deliveries(77)
        self.assertEqual(self.engine.video(video)['card_message'],20)
        self.assertFalse(self.engine.video(video)['card_unknown'])

    def test_unknown_operator_notification_is_reconciled_in_bot_dialog(self):
        self.db.execute('INSERT INTO outbox(id,channel_id,text,state,created,chat_id,attempted) VALUES(?,?,?,?,?,?,?)', ('notice','','Alert','unknown',1000,1,1000))
        message=source(20,sender=77,document=None,caption='Alert');message.reply_to=None
        class ObservedClient(Client):
            def iter_messages(inner, chat, **kwargs):
                self.assertEqual(chat, 77)
                return super().iter_messages(chat, **kwargs)
        self.inbox(ObservedClient([message])).reconcile_deliveries(77)
        self.assertEqual(self.db.one('SELECT state FROM outbox')['state'], 'sent')

    def test_unknown_delivery_retry_requires_full_history_and_elapsed_time(self):
        self.db.execute('INSERT INTO outbox(id,channel_id,text,state,created,chat_id,attempted) VALUES(?,?,?,?,?,?,?)', ('notice','','Alert','unknown',800,1,800))
        old=source(10,sender=77,document=None,caption='Other');old.date=datetime.fromtimestamp(700,timezone.utc)
        self.inbox(Client([old])).reconcile_deliveries(77)
        self.assertEqual(self.db.one('SELECT state FROM outbox')['state'], 'queued')

    def test_restore_refuses_live_foreground_phone(self):
        location=snapshot(self.config)['directory']
        manual=ManualPhone(self.config,self.db,'p',self.vault)
        try:
            with self.assertRaises(UserError):restore(self.config,location)
        finally:manual.close()

    def test_empty_service_does_not_call_model(self):
        from publisher.service import serve
        with patch('publisher.service.Vault', Vault), patch('publisher.service.AgentRunner') as runner, patch('publisher.service.signal.signal'):
            serve(self.config, stop_after=.1)
            runner.return_value.run.assert_not_called()
        self.assertTrue((self.data/'heartbeat.json').exists())

    def test_denied_keychain_keeps_service_alive_without_model_calls(self):
        from publisher.service import serve
        class DeniedVault(Vault):
            def has(self, name):
                raise UserError('Keychain requires system authorization')
        with patch('publisher.service.Vault', DeniedVault), patch('publisher.service.AgentRunner') as runner, patch('publisher.service.signal.signal'):
            serve(self.config, stop_after=.1)
            runner.return_value.run.assert_not_called()
        heartbeat=json.loads((self.data/'heartbeat.json').read_text())
        self.assertEqual((heartbeat['transport'],heartbeat['inbox']),('degraded','degraded'))
        self.assertEqual({r['key'] for r in self.db.all('SELECT key FROM incidents')},{'telegram','inbox'})

    def test_preflight_reports_denied_existing_key_without_secret_values(self):
        from publisher.preflight import child
        ticket=self.data/'ticket.json';proof=self.data/'proof.json'
        ticket.write_text(json.dumps({'key':'probe','sha256':hashlib.sha256(b'probe-value').hexdigest(),'proof':str(proof)}))
        class DeniedVault(Vault):
            def get(self, name): return 'probe-value'
            def has(self, name):
                if name == 'phone-p-token':raise UserError('denied')
                return False
        self.config.save(codex_binary='fixture-codex')
        with patch('publisher.preflight.Vault',DeniedVault),patch('publisher.preflight.subprocess.run',return_value=NS(returncode=0,stdout='Logged in using ChatGPT',stderr='')):
            child(self.config,ticket)
        result=json.loads(proof.read_text())
        self.assertFalse(result['passed'])
        self.assertEqual(result['inaccessible_key_names'],['phone-p-token'])
        self.assertNotIn('probe-value',proof.read_text())

    def test_foreground_keychain_authorization_returns_names_only(self):
        from publisher.vault import authorize
        self.vault.put('phone-p-token','private-token')
        self.vault.put('telegram-bot-token','private-bot')
        result=authorize(self.config,self.vault)
        self.assertEqual(set(result['authorized_names']),{'phone-p-token','telegram-bot-token'})
        self.assertFalse(result['secrets_returned'])
        self.assertNotIn('private-',json.dumps(result))

    def test_vault_disables_system_prompts_by_default(self):
        from publisher.vault import Vault as Keychain
        with patch('publisher.vault.ctypes.CDLL') as library:
            library.return_value.SecKeychainSetUserInteractionAllowed.return_value=0
            Keychain('fixture-only')
            library.return_value.SecKeychainSetUserInteractionAllowed.assert_called_once_with(False)

    def test_subscription_limit_suspends_execution_without_api_fallback(self):
        from publisher.agent import AgentRunner
        self.now=time.time()
        self.engine.enqueue('c','general',args={'message_id':50})
        job=self.engine.claim()
        class LimitedProcess:
            def __init__(inner,command,**kwargs):
                self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
                inner.errors=kwargs['stderr'];inner.returncode=1
            def communicate(inner,*args,**kwargs):inner.errors.write(b'usage limit reached')
        with patch('publisher.agent.subprocess.Popen',LimitedProcess):AgentRunner(self.config).run(job)
        self.assertGreater(self.db.setting('model_suspended_until'),self.now+3590)
        self.assertEqual(self.db.one('SELECT state FROM jobs')['state'],'uncertain')

    def test_reauthorization_does_not_replace_operator(self):
        self.config.save(telegram_user_id=1,operator_chat=1)
        with self.assertRaises(UserError):verify_identity(self.config,NS(id=2,bot=False))
        self.assertEqual(self.config.settings['operator_chat'],1)

    def test_support_report_delivery_is_exact_and_idempotent(self):
        from telethon import types
        client=Client([]);calls=[]
        async def dialogs():
            yield NS(entity=types.User(id=77,first_name='Support',bot=False),input_entity=types.InputPeerUser(77,123))
        async def send(peer,body,**kwargs):calls.append((peer,body,kwargs));return NS(id=90)
        client.iter_dialogs=dialogs;client.send_file=send
        async def auth(config,vault,operation):return await operation(client)
        (self.data/'acceptance-report.json').write_text(json.dumps({'installation_id':self.config.instance,'captured_at':time.time(),'live_acceptance_verified':False}))
        with patch('publisher.support.authorized_client',auth):
            first=send_report(self.config,77,self.vault);again=send_report(self.config,77,self.vault)
        self.assertEqual(first['message_id'],90);self.assertTrue(again['reused']);self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0].user_id,77)

    def test_support_unknown_send_reconciles_without_repeat(self):
        from telethon import types
        client=Client([])
        async def dialogs():
            yield NS(entity=types.User(id=77,first_name='Support'),input_entity=types.InputPeerUser(77,123))
        captions=[]
        async def lost(peer,body,**kwargs):captions.append(kwargs['caption']);raise UserError('lost receipt')
        client.iter_dialogs=dialogs;client.send_file=lost
        async def auth(config,vault,operation):return await operation(client)
        (self.data/'acceptance-report.json').write_text(json.dumps({'installation_id':self.config.instance,'captured_at':time.time()}))
        with patch('publisher.support.authorized_client',auth):
            with self.assertRaises(UserError):send_report(self.config,77,self.vault)
            sent=source(90,sender=1,caption=captions[0]);client.messages=[sent]
            self.assertTrue(send_report(self.config,77,self.vault)['receipt_recovered'])
        self.assertEqual(len(captions),1)

    def test_forum_unknown_create_never_repeats_external_effect(self):
        client=Client([]);calls=[]
        async def dialogs():
            for item in []:yield item
        async def request(value):calls.append(value);raise UserError('lost acknowledgement')
        class Creating:
            iter_dialogs=staticmethod(dialogs)
            __call__=staticmethod(request)
        with self.assertRaises(UserError):asyncio.run(create_forum(self.config,self.vault,'new','Channel','Публикация',Creating()))
        with self.assertRaises(UserError):asyncio.run(create_forum(self.config,self.vault,'new','Channel','Публикация',Creating()))
        self.assertEqual(len(calls),1)

    def test_forum_resume_adopts_existing_group_and_topic(self):
        from telethon import types
        class Existing:
            async def iter_dialogs(inner):
                yield NS(entity=types.Channel(id=123,title='Channel',photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),creator=True,forum=True,megagroup=True))
        async def topics(*args):return [NS(id=5,title='Публикация')]
        with patch('publisher.provision.group_bot',return_value={'verified':True}),patch('publisher.provision.forum_topics',topics):
            result=asyncio.run(create_forum(self.config,self.vault,'new','Channel','Публикация',Existing()))
            again=asyncio.run(create_forum(self.config,self.vault,'new','Channel','Публикация',Existing()))
        self.assertEqual(result['chat_id'],-1000000000123);self.assertEqual(again['publication_topic'],5)

    def test_ambiguous_card_never_adopted_or_repeated(self):
        video=self.ingest();self.db.execute('UPDATE videos SET card_unknown=1,card_attempted=?',(self.now,))
        messages=[]
        for mid in (20,21):
            message=source(mid,sender=77,document=None);message.reply_to.reply_to_msg_id=10
            message.buttons=[[NS(data=(video+':yes:0').encode())]];messages.append(message)
        self.inbox(Client(messages)).reconcile_deliveries(77)
        self.assertTrue(self.engine.video(video)['card_unknown'])

    def test_broken_video_budget_does_not_block_other_phone(self):
        first=self.ingest();self.approve(first)
        for _ in range(5):
            job=self.engine.claim();self.engine.fail(job,'permanent failure');self.now+=1000
        self.assertEqual(self.db.one('SELECT state FROM jobs')['state'],'blocked')
        self.channel('q','d',-101);self.approve(self.ingest(20,'d'))
        self.assertEqual(self.engine.claim()['channel_id'],'d')

    def test_twenty_phones_thirty_videos_isolated_and_drained(self):
        for i in range(20):
            channel='c' if i==0 else 'c'+str(i)
            if i:self.channel('p'+str(i),channel,-100-i)
            for n in range(30):self.approve(self.ingest(100+n,channel))
        completed=0;maximum_parallel=0
        while True:
            jobs=[j for _ in range(2) if (j:=self.engine.claim()) is not None]
            if not jobs:break
            self.assertEqual(len({j['phone_id'] for j in jobs}),len(jobs))
            maximum_parallel=max(maximum_parallel,len(jobs))
            for job in jobs:
                proof='proof-'+job['id'];self.db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(proof,job['id'],'android_get_screen_state','[]',self.now))
                self.engine.complete(job['id'],job['lease'],'scheduled',proof,'x'*11,self.engine.video(job['video_id'])['slot'])
                completed+=1
        self.assertEqual(completed,600);self.assertEqual(maximum_parallel,2)
        self.assertEqual(self.db.one("SELECT count(*) AS n FROM videos WHERE state='scheduled'")['n'],600)

    def test_redaction_hides_pixels_and_text_with_credential(self):
        value={'content':[{'type':'text','text':'token=synthetic-secret'},{'type':'image','data':'synthetic-pixels'}]}
        safe=redact(value,'synthetic-secret')
        self.assertNotIn('synthetic-secret',json.dumps(safe));self.assertNotIn('synthetic-pixels',json.dumps(safe))

    def test_official_api_page_extracts_existing_application(self):
        page='App api_id:<span class="form-control"><strong>12345</strong></span>App api_hash:<span class="form-control">'+'a'*32+'</span>'
        self.assertEqual(app_keys(page),('12345','a'*32))
        self.assertIsNone(app_keys('Authentication required'))

    def test_shorts_geometry_duration_and_no_rewrite(self):
        def box(kind,payload):return struct.pack('>I4s',len(payload)+8,kind)+payload
        def mp4(seconds,width,height):
            mvhd=bytes(12)+struct.pack('>II',1000,seconds*1000)
            matrix=struct.pack('>9i',65536,0,0,0,65536,0,0,0,1073741824)
            tkhd=bytes(40)+matrix+struct.pack('>II',width*65536,height*65536)
            hdlr=bytes(8)+b'vide'+bytes(12)
            trak=box(b'tkhd',tkhd)+box(b'mdia',box(b'hdlr',hdlr))
            return box(b'ftyp',b'isom0000')+box(b'moov',box(b'mvhd',mvhd)+box(b'trak',trak))
        path=self.data/'test.mp4';good=mp4(180,1080,1920);path.write_bytes(good)
        result=validate_shorts(path);self.assertEqual(result['duration'],180)
        self.assertEqual(path.read_bytes(),good)
        for invalid in (mp4(181,1080,1920),mp4(60,1920,1080),b'bad'):
            path.write_bytes(invalid)
            with self.assertRaises(UserError):validate_shorts(path)


if __name__ == '__main__':unittest.main()
