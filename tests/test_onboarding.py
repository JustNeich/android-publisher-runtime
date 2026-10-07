import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from publisher.config import UserError
from publisher.provision import botfather, import_secret, provisioning_lock
from publisher.bootstrap import wifi_adb
from publisher.onboard import verify_phone, deploy, validate_manifest
from publisher.db import Store
from publisher.service import polling_lock

TOKEN = '123456789:' + 'x' * 35


class Config:
    def __init__(self, directory):
        self.data = Path(directory)
        self.root = self.data
        self.database = self.data / 'queue.sqlite3'
        self.keychain_service = 'test-only'
        self.settings = {'adb_binary': 'adb', 'wireless_adb': {'first': '192.168.1.2:5555'}}
    def save(self, **updates):
        self.settings.update(updates)


class Vault:
    def __init__(self): self.values = {}
    def has(self, key): return key in self.values
    def get(self, key): return self.values[key]
    def put(self, key, value): self.values[key] = value


class Bot:
    def __init__(self, *args): pass
    def call(self, method, data=None):
        return {'id': 123456789, 'username': 'sample_bot', 'can_read_all_group_messages': True}


class Conversation:
    def __init__(self, replies):
        self.replies, self.sent, self.sequence = list(replies), [], 100
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def send_message(self, text):
        self.sent.append(text)
        self.sequence += 1
        return SimpleNamespace(id=self.sequence)
    async def get_response(self, sent):
        value = self.replies.pop(0)
        if isinstance(value, Exception): raise value
        self.sequence += 1
        text, buttons = value if isinstance(value, tuple) else (value, [])
        return SimpleNamespace(id=self.sequence, sender_id=93372553, raw_text=text,
                               buttons=[[SimpleNamespace(text=x) for x in buttons]] if buttons else [])


class Client:
    def __init__(self, replies): self.conv = Conversation(replies)
    async def get_entity(self, name):
        return SimpleNamespace(id=93372553, username='BotFather', verified=True, bot=True)
    def conversation(self, *args, **kwargs): return self.conv


class Onboarding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(self.temp.name)
        self.vault = Vault()

    def phone(self):
        db = Store(self.config.database)
        db.execute('INSERT INTO phones VALUES(?,?,?,?,?)', ('first','phone','http://192.168.1.2:8080/mcp','SERIAL',db.clock()))
        db.close()
        self.vault.put('phone-first-token','private-phone-token')

    def test_wifi_saves_only_verified_serial(self):
        calls = []
        def run(binary, serial, args):
            calls.append((serial,args))
            return 'SERIAL' if args[-1] == 'ro.serialno' else 'ok'
        with patch('publisher.bootstrap.run',side_effect=run):
            wifi_adb(self.config,'adb','SERIAL','new','SERIAL','192.168.1.2')
        self.assertEqual(calls[0],('SERIAL',['tcpip','5555']))
        self.assertEqual(self.config.settings['wireless_adb']['new'],'192.168.1.2:5555')

    def test_wrong_wifi_device_never_committed(self):
        with patch('publisher.bootstrap.run',return_value='WRONG'),patch('publisher.bootstrap.time.sleep'):
            with self.assertRaises(UserError): wifi_adb(self.config,'adb','SERIAL','new','SERIAL','192.168.1.2')
        self.assertNotIn('new', self.config.settings['wireless_adb'])

    def test_public_adb_address_rejected_before_mutation(self):
        with patch('publisher.bootstrap.run') as run:
            with self.assertRaises(UserError): wifi_adb(self.config,'adb','SERIAL','new','SERIAL','8.8.8.8')
            run.assert_not_called()

    def test_usb_connection_is_not_wireless_acceptance(self):
        self.phone()
        with patch('publisher.onboard.WirelessADB') as adb,patch('publisher.onboard.run',return_value='List of devices attached\nSERIAL device usb:1-1'):
            with self.assertRaisesRegex(UserError,'USB ещё'): verify_phone(self.config,'first')
            adb.return_value.wake.assert_not_called()
        self.assertFalse((self.config.data/'onboarding/first/wifi-proof.json').exists())

    def test_android_usb_signal_overrules_empty_host_inventory(self):
        self.phone()
        with patch('publisher.onboard.WirelessADB') as adb,patch('publisher.onboard.run',return_value='List of devices attached\n'):
            adb.return_value.shell.return_value = 'mConnected=true'
            with self.assertRaises(UserError): verify_phone(self.config,'first')

    def test_unsecured_phone_needs_no_pin_and_is_slept(self):
        self.phone()
        with patch('publisher.onboard.WirelessADB') as adb,patch('publisher.onboard.run',return_value='List of devices attached\n192.168.1.2:5555 device'),patch('publisher.onboard.Vault',return_value=self.vault),patch('publisher.onboard.Phone') as phone:
            adb.return_value.shell.return_value = 'mConnected=false'
            adb.return_value.secure.return_value = False
            phone.return_value.__enter__.return_value.call.return_value = {}
            result=verify_phone(self.config,'first')
            adb.return_value.sleep.assert_called_once()
        self.assertTrue(result['mcp_with_screen_off_verified'])
        self.assertFalse(result['secure_lock'])
        self.assertTrue((self.config.data/'onboarding/first/wifi-proof.json').exists())

    def test_secure_phone_without_authorized_pin_blocks_and_sleeps(self):
        self.phone()
        with patch('publisher.onboard.WirelessADB') as adb,patch('publisher.onboard.run',return_value='List of devices attached\n'),patch('publisher.onboard.Vault',return_value=self.vault):
            adb.return_value.shell.return_value = 'mConnected=false'
            adb.return_value.secure.return_value = True
            with self.assertRaises(UserError): verify_phone(self.config,'first')
            adb.return_value.sleep.assert_called_once()
        self.assertFalse((self.config.data/'onboarding/first/wifi-proof.json').exists())

    def test_new_bot_token_stays_inside_vault(self):
        client=Client(['cancel','No bots','cancel','Choose a name','Choose a username',TOKEN])
        with patch('publisher.provision.Bot',Bot):
            result=asyncio.run(botfather(self.config,self.vault,'sample_bot','Publisher',client))
        self.assertEqual(client.conv.sent.count('/newbot'),1)
        self.assertEqual(self.vault.get('telegram-bot-token'),TOKEN)
        self.assertNotIn(TOKEN,json.dumps(result))
        self.assertNotIn(TOKEN,(self.config.data/'bot-provision.json').read_text())

    def test_creation_timeout_then_retry_never_creates_second_bot(self):
        client=Client(['cancel','No bots','cancel','Choose a name','Choose a username',TimeoutError()])
        with patch('publisher.provision.Bot',Bot):
            with self.assertRaises(TimeoutError):asyncio.run(botfather(self.config,self.vault,'sample_bot','Publisher',client))
            second=Client(['cancel','No bots'])
            with self.assertRaises(UserError):asyncio.run(botfather(self.config,self.vault,'sample_bot','Publisher',second))
        self.assertNotIn('/newbot',second.conv.sent)
        self.assertTrue(json.loads((self.config.data/'bot-provision.json').read_text())['creation_started'])

    def test_unknown_creation_recovers_existing_token(self):
        (self.config.data/'bot-provision.json').write_text(json.dumps({'username':'sample_bot','creation_started':True}))
        client=Client(['cancel',('Choose',['@sample_bot']),TOKEN])
        with patch('publisher.provision.Bot',Bot):
            result=asyncio.run(botfather(self.config,self.vault,'sample_bot','Publisher',client))
        self.assertTrue(result['verified'])
        self.assertNotIn('/newbot',client.conv.sent)

    def test_wrong_bot_identity_is_never_saved(self):
        class Other(Bot):
            def call(self,*args):return {'id':2,'username':'other_bot'}
        with patch('publisher.provision.Bot',Other):
            with self.assertRaises(UserError):asyncio.run(botfather(self.config,self.vault,'sample_bot','Publisher',Client(['cancel',('Choose',['@sample_bot']),TOKEN])))
        self.assertFalse(self.vault.has('telegram-bot-token'))

    def test_private_import_returns_no_value_and_rejects_unknown_name(self):
        value='private-bearer-value'
        result=import_secret(self.config,'phone-first-token',value,self.vault)
        self.assertNotIn(value,json.dumps(result))
        with self.assertRaises(UserError):import_secret(self.config,'uncontrolled-key',value,self.vault)
        self.assertFalse(self.vault.has('uncontrolled-key'))

    def test_provision_lock_excludes_parallel_creation(self):
        with provisioning_lock(self.config):
            with self.assertRaises(UserError):
                with provisioning_lock(self.config): pass

    def test_one_polling_owner_across_project_paths(self):
        directory=self.config.data/'owners'
        owner=polling_lock(TOKEN,directory)
        try:
            with self.assertRaises(UserError):polling_lock(TOKEN,directory)
            self.assertNotIn(TOKEN,next(directory.iterdir()).read_text())
        finally:owner.close()
        polling_lock(TOKEN,directory).close()

    def plan(self):
        return {'phone':'first','channel':'sample-channel','name':'Sample Channel','youtube_id':'UC'+'x'*22,
                'chat_id':-1000000000123,'publication_topic':10,'timezone':'Europe/Moscow','times':['19:00'],
                'bot_username':'sample_bot','bot_name':'Publisher'}

    def test_manifest_rejects_secrets_and_guessed_identity(self):
        for plan in ({**self.plan(),'bot_token':TOKEN},{**self.plan(),'youtube_id':'Sample Channel'}):
            with self.assertRaises(UserError):validate_manifest(plan)

    def test_deploy_stays_paused_and_never_claims_live_acceptance(self):
        self.phone()
        with patch('publisher.preflight.run',return_value={'passed':True}),patch('publisher.install.initialize',return_value={'ok':True}),patch('publisher.onboard.verify_phone',return_value={'ok':True}),patch('publisher.provision.provision_bot',return_value={'ok':True}),patch('publisher.provision.configure_group',return_value={'ok':True}),patch('publisher.inbox.Inbox.verify',return_value={'ok':True}),patch('publisher.install.bind',return_value={'ok':True}),patch('publisher.install.install_service',return_value={'ok':True}),patch('publisher.install.doctor',return_value={'ready':True}),patch('publisher.onboard.usb') as usb:
            result=deploy(self.config,self.plan())
            usb.assert_not_called()
        self.assertEqual(result['state'],'pending_live_acceptance')
        self.assertFalse(result['live_acceptance_verified'])
        db=Store(self.config.database)
        self.assertTrue(db.setting('paused'))
        db.close()

    def test_partial_usb_setup_is_retried_before_wifi_verification(self):
        self.phone();self.config.settings['wireless_adb']={}
        with patch('publisher.preflight.run',return_value={'passed':True}),patch('publisher.install.initialize',return_value={'ok':True}),patch('publisher.onboard.usb',return_value={'ok':True}) as usb,patch('publisher.onboard.verify_phone',side_effect=UserError('USB still present')):
            with self.assertRaises(UserError):deploy(self.config,self.plan())
            usb.assert_called_once()
        state=json.loads((self.config.data/'deployments/sample-channel/deployment.json').read_text())
        self.assertEqual(state['state'],'blocked')
        self.assertEqual(state['current_step'],'phone_without_usb')


if __name__=='__main__': unittest.main()
