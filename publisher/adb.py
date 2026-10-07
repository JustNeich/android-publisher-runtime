"""Paired Android wireless ADB for screen power and private PIN entry only."""
import re
import subprocess
from urllib.parse import urlsplit
from .config import UserError


class WirelessADB:
    def __init__(self, config, phone):
        self.config, self.phone = config, phone
        self.binary = config.settings.get('adb_binary')
        self.address = config.settings.get('wireless_adb', {}).get(phone['id'])
        if not self.binary or not self.address:
            raise UserError('Codex должен завершить usb-setup: Wi-Fi ADB ещё не настроен.')

    def run(self, args, input_text=None, timeout=15):
        try:
            result = subprocess.run([self.binary, *args], input=input_text, text=True, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise UserError('Wireless ADB не отвечает.') from None
        if result.returncode:
            raise UserError('Wireless ADB отклонил команду. Проверьте пару и текущую сеть.')
        return result.stdout

    def resolve(self, discover=True):
        import ipaddress
        from .db import Store
        old_ip = urlsplit(self.phone['endpoint']).hostname
        candidates = [self.address]
        try:
            for line in (self.run(['mdns', 'services']).splitlines() if discover else []):
                parts = line.split()
                if len(parts) >= 3 and '_adb-tls-connect._tcp' in line and self.phone['serial'] in parts[0]:
                    candidates.insert(0, parts[-1])
        except UserError:
            pass
        # Legacy trusted tcpip has no Bonjour identity. Only try recent private ARP peers.
        try:
            if discover:
                arp = subprocess.run(['/usr/sbin/arp','-an'],capture_output=True,text=True,timeout=5)
                candidates.extend(ip + ':5555' for ip in re.findall(r'\((\d+\.\d+\.\d+\.\d+)\)', arp.stdout)[:32])
        except (OSError,subprocess.TimeoutExpired):
            pass
        for address in dict.fromkeys(candidates):
            match = re.fullmatch(r'(\d+\.\d+\.\d+\.\d+):(\d{1,5})', address)
            if not match:
                continue
            try:
                ip = ipaddress.ip_address(match[1])
                if not ip.is_private or ip.is_loopback or ip.is_unspecified or not 0 < int(match[2]) < 65536:
                    continue
                self.run(['connect', address], timeout=3)
                serial = self.run(['-s', address, 'shell', 'getprop', 'ro.serialno'], timeout=3).strip()
                if serial != self.phone['serial']:
                    continue
                self.address = address
                if match[1] != old_ip:
                    updated = self.phone['endpoint'].replace(old_ip, match[1], 1)
                    db = Store(self.config.database)
                    try:
                        db.execute('UPDATE phones SET endpoint=? WHERE id=? AND serial=?', (updated, self.phone['id'], serial))
                    finally:
                        db.close()
                    self.phone['endpoint'] = updated
                if self.config.settings.get('wireless_adb', {}).get(self.phone['id']) != address:
                    self.config.save(wireless_adb={self.phone['id']: address})
                return address
            except (UserError, ValueError):
                continue
        raise UserError('Доверенный Wi-Fi ADB недоступен. После reboot может требоваться первое разблокирование и USB-восстановление; USB не подменяет фоновую LAN-проверку.')

    def shell(self, command, timeout=15):
        # Script arrives over stdin. PIN is never placed in process arguments.
        return self.run(['-s', self.address, 'shell'], input_text=command + '\n', timeout=timeout)

    def transfer(self, source, video_id, sha256):
        if not re.fullmatch(r'[a-f0-9]{16}', video_id) or not re.fullmatch(r'[a-f0-9]{64}', sha256):
            raise UserError('Некорректный идентификатор временного MP4.')
        self.resolve()
        directory = '/sdcard/Movies/AndroidPublisher/' + self.config.instance
        target = directory + '/' + video_id + '.mp4'
        self.shell('mkdir -p ' + directory)
        self.run(['-s', self.address, 'push', str(source), target], timeout=900)
        observed = self.shell('toybox sha256sum ' + target, timeout=180).split()[0]
        if observed != sha256:
            raise UserError('SHA-256 MP4 на телефоне не совпал с оригиналом; загрузка запрещена.')
        self.shell('am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://' + target)
        return target

    def remove_media(self, video_id):
        if not re.fullmatch(r'[a-f0-9]{16}', video_id):
            raise UserError('Недопустимое имя временного файла.')
        self.resolve(discover=False)
        target = '/sdcard/Movies/AndroidPublisher/' + self.config.instance + '/' + video_id + '.mp4'
        self.shell('rm -f ' + target, timeout=5)
        if self.shell('if [ -e ' + target + ' ]; then echo PRESENT; else echo REMOVED; fi', timeout=5).strip() != 'REMOVED':
            raise UserError('Удаление своего временного MP4 с телефона не подтверждено.')

    def wake(self):
        self.resolve()
        self.shell('input keyevent KEYCODE_WAKEUP')

    def enter_pin(self, pin):
        if not re.fullmatch(r'\d{4,16}', pin):
            raise UserError('Для автоматической разблокировки нужен сохранённый цифровой PIN.')
        self.shell('input text ' + pin + '\ninput keyevent KEYCODE_ENTER')

    def sleep(self):
        self.resolve()
        self.shell('input keyevent KEYCODE_SLEEP')
        state = self.shell('dumpsys power')
        if not re.search(r'mWakefulness=(Asleep|Dozing)', state):
            raise UserError('Android не подтвердил погашенный экран.')

    def locked(self):
        state = self.shell('dumpsys window policy')
        values = re.findall(r'(?:mIsShowing|showing)=(true|false)', state)
        if not values:
            raise UserError('Android не подтвердил состояние блокировки.')
        return any(value == 'true' for value in values)

    def secure(self):
        state = self.shell('dumpsys window policy')
        values = re.findall(r'(?:mSecure|secure)=(true|false)', state)
        if not values:
            raise UserError('Android не подтвердил наличие защищённой блокировки; Codex должен проверить телефон.')
        return any(value == 'true' for value in values)
