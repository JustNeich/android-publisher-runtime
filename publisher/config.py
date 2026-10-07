import hashlib
import json
import os
import secrets
import uuid
from pathlib import Path


class UserError(Exception):
    """Safe, owner-facing failure; never include credentials in these messages."""


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isinstance(content, str):
        content = content.encode()
    temp = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Config:
    def __init__(self, root=None):
        self.root = Path(root or Path(__file__).resolve().parents[1]).resolve()
        default = Path.home() / 'Library/Application Support/AndroidPublisher/default'
        self.data = Path(os.environ.get('ANDROID_PUBLISHER_DATA', default)).resolve()
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        self.settings_path = self.data / 'settings.json'
        self.settings = json.loads(self.settings_path.read_text()) if self.settings_path.exists() else {}
        identity = self.data / 'installation.json'
        if not identity.exists():
            # Concurrent foreground/background processes must agree on the first identity.
            import fcntl
            with (self.data / 'identity.lock').open('a+') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if not identity.exists():
                    write_private(identity, json.dumps({'id': uuid.uuid4().hex, 'schema': 1}))
        self.instance = json.loads(identity.read_text())['id']
        if not isinstance(self.instance, str) or not self.instance.isalnum():
            raise UserError('Повреждён идентификатор установки; восстановите installation.json из резервной копии.')

    def save(self, **updates):
        import fcntl
        with (self.data / 'settings.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = json.loads(self.settings_path.read_text()) if self.settings_path.exists() else {}
            for key, value in updates.items():
                if key == 'wireless_adb':
                    current[key] = {**current.get(key, {}), **value}
                else:
                    current[key] = value
            self.settings = current
            write_private(self.settings_path, json.dumps(self.settings, ensure_ascii=False, indent=2))

    @property
    def database(self):
        return self.data / 'publisher.sqlite3'

    @property
    def keychain_service(self):
        return self.settings.get('keychain_service', 'org.android-publisher.' + self.instance)
