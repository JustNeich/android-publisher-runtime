"""macOS Keychain without secret-bearing command arguments."""
import ctypes
import sys
from .config import UserError


class Vault:
    def __init__(self, service, interactive=False):
        if sys.platform != 'darwin':
            raise UserError('Для рабочего хранилища секретов нужен macOS Keychain.')
        self.service = service.encode()
        self.lib = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        # A launchd service must fail promptly instead of waiting on a hidden
        # access dialog. Only the explicit foreground authorization command
        # permits a system prompt, and never returns the stored values.
        self.lib.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
        self.lib.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
        status = self.lib.SecKeychainSetUserInteractionAllowed(bool(interactive))
        if status:
            raise UserError(f'Режим доступа Keychain не установлен (код {status}).')
        u = ctypes.c_uint32
        p = ctypes.c_void_p
        self.lib.SecKeychainFindGenericPassword.argtypes = [p, u, p, u, p, ctypes.POINTER(u), ctypes.POINTER(p), ctypes.POINTER(p)]
        self.lib.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.lib.SecKeychainAddGenericPassword.argtypes = [p, u, p, u, p, u, p, ctypes.POINTER(p)]
        self.lib.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.lib.SecKeychainItemModifyAttributesAndData.argtypes = [p, p, u, p]
        self.lib.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.lib.SecKeychainItemFreeContent.argtypes = [p, p]
        self.lib.SecKeychainItemDelete.argtypes = [p]
        self.lib.SecKeychainItemDelete.restype = ctypes.c_int32
        self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.cf.CFRelease.argtypes = [p]

    def _find(self, name):
        account = name.encode()
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self.lib.SecKeychainFindGenericPassword(None, len(self.service), self.service, len(account), account,
                                                       ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        if status == -25300:
            return None, None
        if status in (-25293, -25308, -25315):
            raise UserError('Keychain требует системное разрешение. Codex должен подготовить keychain-authorize на экране и затем повторить background-preflight.')
        if status:
            raise UserError(f'Keychain недоступен (код {status}).')
        try:
            value = ctypes.string_at(data, length.value).decode()
        finally:
            self.lib.SecKeychainItemFreeContent(None, data)
        return value, item

    def get(self, name):
        value, item = self._find(name)
        if item:
            self.cf.CFRelease(item)
        if value is None:
            raise UserError('Нужный доступ ещё не сохранён в Keychain: ' + name)
        return value

    def has(self, name):
        value, item = self._find(name)
        if item:
            self.cf.CFRelease(item)
        return value is not None

    def put(self, name, value):
        if not isinstance(value, str) or not value or len(value) > 8192:
            raise UserError('Нужен непустой секрет допустимого размера.')
        _, item = self._find(name)
        raw = value.encode()
        try:
            if item:
                code = self.lib.SecKeychainItemModifyAttributesAndData(item, None, len(raw), raw)
            else:
                account = name.encode()
                code = self.lib.SecKeychainAddGenericPassword(None, len(self.service), self.service,
                                                            len(account), account, len(raw), raw, None)
            if code:
                raise UserError(f'Не удалось сохранить доступ в Keychain (код {code}).')
        finally:
            if item:
                self.cf.CFRelease(item)

    def delete(self, name):
        _, item = self._find(name)
        if item:
            try:
                code = self.lib.SecKeychainItemDelete(item)
                if code:
                    raise UserError(f'Не удалось удалить собственный тестовый доступ (код {code}).')
            finally:
                self.cf.CFRelease(item)


def configured_names(config):
    from .db import Store
    db = Store(config.database)
    try:
        phones = db.all('SELECT id FROM phones')
    finally:
        db.close()
    return ['telegram-bot-token', 'telegram-api-id', 'telegram-api-hash', 'telegram-provision-session'] + [
        'phone-' + phone['id'] + '-' + suffix for phone in phones for suffix in ('token', 'pin')]


def authorize(config, vault=None):
    vault = vault or Vault(config.keychain_service, interactive=True)
    readable, absent = [], []
    for name in configured_names(config):
        (readable if vault.has(name) else absent).append(name)
    return {'authorized_names': readable, 'absent_names': absent, 'secrets_returned': False,
            'next': 'background-preflight; doctor'}
