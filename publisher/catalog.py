"""Persistent native plugin source; no service, queue or credential mutation."""
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from .config import UserError, write_private
from .runtime import runtime_home, verify_package

MARKETPLACE = 'android-publisher-local'
PLUGIN = 'android-publisher'
MARKER = '.publisher-catalog.json'


def owned_catalog(root):
    """Refuse to replace another source or move unexpected user files."""
    if root.is_symlink():
        raise UserError('Символьная ссылка не заменяется каталогом плагина.')
    marker = root / MARKER
    try:
        owner = json.loads(marker.read_text())
        files = json.loads((root / 'PACKAGE.sha256.json').read_text())
    except (OSError, ValueError):
        raise UserError('Папка каталога не принадлежит этой установке. Сначала сохраните и исследуйте её.') from None
    if owner.get('plugin') != PLUGIN or owner.get('marketplace') != MARKETPLACE:
        raise UserError('Другой каталог не заменяется.')
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != set(files) | {'PACKAGE.sha256.json', MARKER}:
        raise UserError('В каталоге есть посторонние файлы; автоматическая замена остановлена.')
    return owner


def prepare(config):
    home = runtime_home()
    if not (home / 'active.json').exists():
        raise UserError('Сначала install.sh: каталог готовится из активного проверенного runtime.')
    catalog = home.parent / 'catalog'
    with (home / 'install.lock').open('a+') as install_lock, (home / 'catalog.lock').open('a+') as lock:
        fcntl.flock(install_lock, fcntl.LOCK_SH)
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = json.loads((home / 'active.json').read_text())
        if Path(active['data']).resolve() != config.data.resolve():
            raise UserError('Активный runtime относится к другой установке; каталог не изменён.')
        source = Path(active['app']).resolve()
        files = verify_package(source)
        plugin = json.loads((source / '.codex-plugin/plugin.json').read_text())
        market = json.loads((source / '.agents/plugins/marketplace.json').read_text())
        entries = market.get('plugins', [])
        if plugin.get('name') != PLUGIN or market.get('name') != MARKETPLACE or len(entries) != 1 or entries[0].get('name') != PLUGIN:
            raise UserError('Пакет должен содержать единственный существующий Android Publisher.')
        package_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        result = {'marketplace_name': MARKETPLACE, 'plugin_id': PLUGIN + '@' + MARKETPLACE,
                  'catalog_root': str(catalog), 'version': plugin['version'],
                  'prepared': True, 'installed_by_this_command': False, 'live_ui_verified': False}
        old = owned_catalog(catalog) if catalog.exists() or catalog.is_symlink() else None
        if old and old.get('installation_id') != config.instance:
            raise UserError('Каталог принадлежит другой установке; он сохранён без изменений.')
        if old and old.get('package_hash') == package_hash:
            verify_package(catalog)
            return {**result, 'reused': True}
        stage = Path(tempfile.mkdtemp(prefix='.publisher-catalog-', dir=home.parent))
        backup = None
        try:
            for name in [*files, 'PACKAGE.sha256.json']:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, target)
            verify_package(stage)
            write_private(stage / MARKER, json.dumps({'plugin': PLUGIN, 'marketplace': MARKETPLACE,
                          'package_hash': package_hash, 'installation_id': config.instance}))
            if old:
                recovery = home.parent / 'catalog-recovery'
                recovery.mkdir(exist_ok=True, mode=0o700)
                backup = recovery / (old['package_hash'][:12] + '-' + str(time.time_ns()))
                os.replace(catalog, backup)
            try:
                os.replace(stage, catalog)
            except OSError:
                if backup is not None:
                    os.replace(backup, catalog)
                raise
            return {**result, 'reused': False, 'previous_catalog_saved': backup is not None}
        finally:
            if stage.exists():
                shutil.rmtree(stage)
