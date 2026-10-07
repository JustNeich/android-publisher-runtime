"""USB onboarding with explicit device selection and verified official downloads."""
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.request import urlopen
from .config import UserError, write_private
from .db import Store
from .phone import Phone
from .vault import Vault

PACKAGE = 'com.danielealbano.androidremotecontrolmcp'
COMPONENT = PACKAGE + '/' + PACKAGE + '.services.accessibility.McpAccessibilityService'


def asset(config, name):
    entry = json.loads((config.root/'assets.json').read_text())[name]
    directory = config.data/'dependencies';directory.mkdir(mode=0o700,exist_ok=True)
    target = directory/entry['url'].rsplit('/',1)[1]
    if target.exists():
        if hashlib.new(entry['algorithm'],target.read_bytes()).hexdigest()==entry['checksum']: return target
        raise UserError('Сохранённая зависимость не прошла проверку; проверьте локальный файл.')
    with urlopen(entry['url'],timeout=60) as response:
        data=response.read(150*1024*1024+1)
    if len(data)>150*1024*1024 or hashlib.new(entry['algorithm'],data).hexdigest()!=entry['checksum']:
        raise UserError('Зависимость не прошла проверку официальной контрольной суммы.')
    write_private(target,data);return target


def prepare_adb(config):
    binary=config.settings.get('adb_binary')
    if binary and Path(binary).is_file() and Path(binary).resolve().is_relative_to((config.data/'dependencies').resolve()) and config.settings.get('adb_sha256'):
        if hashlib.sha256(Path(binary).read_bytes()).hexdigest() != config.settings['adb_sha256']:
            raise UserError('Собственный ADB изменился после установки; нужна проверка зависимости.')
        return binary
    archive=asset(config,'platform_tools_macos');directory=archive.parent
    with zipfile.ZipFile(archive) as zip:
        for info in zip.infolist():
            target=(directory/info.filename).resolve()
            if not target.is_relative_to(directory.resolve()):raise UserError('Некорректный архив Android tools.')
        zip.extractall(directory)
        for info in zip.infolist():
            mode=(info.external_attr>>16)&0o777
            if mode:os.chmod(directory/info.filename,mode)
    binary=str(directory/'platform-tools/adb');os.chmod(binary,0o700)
    config.save(adb_binary=binary, adb_sha256=hashlib.sha256(Path(binary).read_bytes()).hexdigest());return binary


def run(binary,serial,args,script=None):
    try:
        value=subprocess.run([binary,*(['-s',serial] if serial else []),*args],input=script,capture_output=True,text=True,timeout=60)
    except (OSError,subprocess.TimeoutExpired):raise UserError('ADB недоступен; результат настройки нужно сверить.') from None
    if value.returncode:raise UserError('ADB отклонил настройку; подробности проверьте на телефоне.')
    return value.stdout.strip()


def usb(config,phone_id,serial=None):
    if not re.fullmatch(r'[a-z0-9-]{1,40}',phone_id):raise UserError('Имя подключения: латинские буквы, цифры, дефис.')
    binary=prepare_adb(config)
    rows=[line.split() for line in run(binary,None,['devices']).splitlines()[1:] if line.strip()]
    ready=[r[0] for r in rows if r[1]=='device' and ':' not in r[0] and not r[0].startswith('adb-')]
    if serial is None and len(ready)==1:serial=ready[0]
    if serial not in ready:raise UserError('Подключите выбранный телефон, включите USB debugging и подтвердите доверие этому Mac на телефоне.')
    shell=lambda args:run(binary,serial,['shell','-T'],shlex.join(args)+'\n')
    physical=shell(['getprop','ro.serialno']);model=shell(['getprop','ro.product.model'])
    sdk=int(shell(['getprop','ro.build.version.sdk']))
    if sdk<33:raise UserError('Этому коннектору нужен Android 13 или новее.')
    db=Store(config.database);vault=Vault(config.keychain_service)
    try:
        old=db.one('SELECT * FROM phones WHERE id=?',(phone_id,))
        duplicate=db.one('SELECT id FROM phones WHERE serial=? AND id<>?',(physical,phone_id))
        if duplicate:raise UserError('Этот физический Android уже зарегистрирован как '+duplicate['id']+'. Используйте существующее имя.')
        if old and old['serial']!=physical:raise UserError('Это имя уже связано с другим физическим телефоном.')
        installed='package:'+PACKAGE in shell(['pm','list','packages',PACKAGE]).splitlines()
        key='phone-'+phone_id+'-token'
        if installed and not vault.has(key):
            raise UserError('Установленный коннектор принадлежит прежнему подключению. Codex должен получить его разрешённый секрет через защищённый канал; токен не меняем и не просим у помощника.')
        if not installed:
            # Persist our credential before installation. A crash immediately after
            # pm install can then resume without treating our app as a foreign one.
            if not vault.has(key):vault.put(key,secrets.token_urlsafe(32))
            apk=asset(config,'android_mcp');result=run(binary,serial,['install',str(apk)])
            if 'Success' not in result:raise UserError('Телефон не подтвердил установку коннектора.')
        token=vault.get(key)
        previous=shell(['settings','get','secure','enabled_accessibility_services']);enabled=shell(['settings','get','secure','accessibility_enabled'])
        snapshot=config.data/'onboarding'/phone_id/'before.json'
        power_list=shell(['dumpsys','deviceidle','whitelist'])
        if not snapshot.exists():write_private(snapshot,json.dumps({'serial':physical,'accessibility':previous,'enabled':enabled,'app_existed':installed,'mcp_battery_exempt':PACKAGE in power_list}))
        services=[] if previous in ('null','') else previous.split(':')
        if COMPONENT not in services:services.append(COMPONENT)
        shell(['settings','put','secure','enabled_accessibility_services',':'.join(services)])
        shell(['settings','put','secure','accessibility_enabled','1'])
        shell(['am','broadcast','-a',PACKAGE+'.ADB_CONFIGURE','-n',PACKAGE+'/'+PACKAGE+'.services.mcp.AdbConfigReceiver','--es','bearer_token',token,'--ez','bearer_token_enabled','true','--es','binding_address','0.0.0.0','--ei','port','8080','--ez','tunnel_enabled','false','--ez','auto_start_on_boot','true'])
        shell(['am','start','-n',PACKAGE+'/'+PACKAGE+'.services.mcp.AdbServiceTrampolineActivity','--es','action','start'])
        shell(['cmd','deviceidle','whitelist','+'+PACKAGE])
        if PACKAGE not in shell(['dumpsys','deviceidle','whitelist']):
            raise UserError('Android не подтвердил исключение собственного MCP из экономии батареи. Codex должен проверить ограничения производителя.')
        routes=shell(['ip','route']);ip=re.search(r'\bwlan\d\b.*?\bsrc\s+(\d+\.\d+\.\d+\.\d+)',routes)
        if not ip:raise UserError('Телефон должен быть подключён к общей Wi-Fi-сети.')
        address='http://'+ip[1]+':8080/mcp'
        db.execute('INSERT INTO phones VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,endpoint=excluded.endpoint', (phone_id,model,address,physical,db.clock()))
        with Phone(address,token) as remote:
            state=remote.call('android_get_screen_state',{})
            if state.get('isError'):raise UserError('Codex: Accessibility не готова после USB-настройки. Проверьте ограничения производителя и разрешения через доверенный USB; не перекладывайте настройку на помощника.')
        shell(['settings','put','global','stay_on_while_plugged_in','0'])
        # The already trusted USB host enables Wi-Fi ADB; no pairing code worksheet.
        wifi_adb(config, binary, serial, phone_id, physical, ip[1])
        return {'phone':phone_id,'serial':physical,'model':model,'lan_mcp_verified':True,
                'wifi_adb_verified':True,'battery_exemption_verified':True,'usb_disconnected_verified':False,
                'next':'Codex: verify-phone after cable removal; then probe-phone. No token form or manual Wi-Fi pairing.'}
    finally:db.close()


def wifi_adb(config, binary, usb_serial, phone_id, physical, ip):
    """Commit the address only after an authenticated LAN connection to this serial."""
    import ipaddress
    address_ip = ipaddress.ip_address(ip)
    if not address_ip.is_private or address_ip.is_loopback or address_ip.is_unspecified:
        raise UserError('Wi-Fi ADB разрешён только в локальной сети.')
    address = str(address_ip) + ':5555'
    run(binary, usb_serial, ['tcpip', '5555'])
    for attempt in range(10):
        try:
            run(binary, None, ['connect', address])
            found = run(binary, address, ['shell', 'getprop', 'ro.serialno'])
            if found != physical:
                raise UserError('Wi-Fi адрес отвечает другим физическим телефоном; подключение не сохранено.')
            previous = dict(config.settings.get('wireless_adb', {}))
            previous[phone_id] = address
            config.save(wireless_adb={phone_id: address})
            return address
        except UserError:
            if attempt == 9: raise
            time.sleep(.5)
