"""Versioned private runtime; plugin cache paths never own operational state."""
import argparse
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from contextlib import ExitStack
from urllib.request import urlopen
from .config import Config, UserError, write_private
from . import __version__

VERSION = __version__


def runtime_home():
    return Path(os.environ.get('ANDROID_PUBLISHER_RUNTIME', str(Path.home() / 'Library/Application Support/AndroidPublisher/runtime'))).resolve()


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def package_files(root):
    root = Path(root)
    for name in ('publisher', 'skills', 'docs', 'bin', 'scripts', 'vendor', '.codex-plugin', '.agents/plugins', 'tests'):
        directory = root / name
        if directory.exists():
            for path in directory.rglob('*'):
                if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    yield path.relative_to(root)
    for name in ('assets.json', 'pyproject.toml', 'requirements.lock', 'README.md', 'AGENTS.md', 'install.sh', '.mcp.json', 'imperium.json'):
        if (root / name).is_file():
            yield Path(name)


def verify_package(root):
    root = Path(root).resolve()
    checks = root / 'PACKAGE.sha256.json'
    if not checks.exists():
        raise UserError('Нет проверяемого PACKAGE.sha256.json; используйте подготовленный релиз.')
    files = json.loads(checks.read_text())
    actual = {str(p) for p in package_files(root)}
    if set(files) != actual:
        raise UserError('Состав пакета отличается от списка контрольных сумм.')
    for name, expected in files.items():
        if digest(root / name) != expected:
            raise UserError('Проверка файла релиза не пройдена: ' + name)
    return files


def download(entry, destination):
    destination = Path(destination)
    if destination.exists() and digest(destination) == entry['checksum']:
        return destination
    temp = destination.with_suffix('.part')
    sha = hashlib.sha256()
    try:
        with urlopen(entry['url'], timeout=60) as source, temp.open('wb') as target:
            size = 0
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > 200 * 1024 * 1024:
                    raise UserError('Зависимость превышает допустимый размер.')
                sha.update(block); target.write(block)
        if sha.hexdigest() != entry['checksum']:
            raise UserError('Контрольная сумма зависимости не совпала.')
        os.replace(temp, destination)
        return destination
    except (OSError, TimeoutError):
        raise UserError('Зависимость не получена. Установка продолжается той же командой после восстановления сети.') from None
    finally:
        temp.unlink(missing_ok=True)


def checked(command, timeout=120, env=None):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise UserError('Runtime-команда недоступна или не завершилась: ' + Path(command[0]).name) from None
    if result.returncode:
        raise UserError('Runtime-проверка не пройдена: ' + Path(command[0]).name)
    return result.stdout


def install(source):
    if sys.platform != 'darwin' or sys.version_info[:3] != (3, 12, 13):
        raise UserError('Запустите install.sh: требуется собственный Python 3.12.13 на macOS.')
    source = Path(source).resolve()
    files = verify_package(source)
    home = runtime_home(); home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home / 'install.lock').open('a+') as lock, ExitStack() as guards:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = Config(source)
        active_path = home / 'active.json'
        active = json.loads(active_path.read_text()) if active_path.exists() else None
        tree_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
        if active and active.get('package_hash') == tree_hash and active['data'] == str(config.data):
            verify_package(active['app'])
            checked([active['python'], '-c', 'import ssl,sqlite3,plistlib,pyexpat,ctypes,zoneinfo,telethon'])
            return {**active, 'installed': True, 'reused': True, 'live_acceptance_verified': False}
        from .lifecycle import stopped_state
        guards.enter_context(stopped_state(config))
        db_path = config.database
        if db_path.exists():
            from .db import Store
            db = Store(db_path)
            try:
                if db.one("SELECT id FROM jobs WHERE state='running' LIMIT 1"):
                    raise UserError('Перед обновлением завершите/сверьте активные задания: update --package.')
            finally:
                db.close()
        destination = home / (VERSION + '-' + tree_hash)
        app = destination / 'app'; app.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in files:
            target = app / name; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        shutil.copy2(source / 'PACKAGE.sha256.json', app / 'PACKAGE.sha256.json')
        venv = destination / 'venv'
        if not (venv / 'bin/python').exists():
            checked([sys.executable, '-m', 'venv', str(venv)])
        python = str(venv / 'bin/python')
        checked([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-index', '--find-links', str(app / 'vendor'), '--require-hashes', '-r', str(app / 'requirements.lock')])
        checked([python, '-c', 'import ssl,sqlite3,plistlib,pyexpat,ctypes,zoneinfo,telethon; from telethon.sessions import StringSession'])
        assets = json.loads((app / 'assets.json').read_text())
        arch = 'arm64' if platform.machine() == 'arm64' else 'x86_64'
        deps = home / 'dependencies'; deps.mkdir(exist_ok=True, mode=0o700)
        binary = deps / ('codex-0.144.1-' + arch)
        if not binary.exists():
            archive = download(assets['codex_macos_' + arch], deps / ('codex-' + arch + '.tar.gz'))
            with tarfile.open(archive) as package:
                member = next((m for m in package if m.isfile() and m.name in ('codex-aarch64-apple-darwin', 'codex-x86_64-apple-darwin')), None)
                if not member:
                    raise UserError('В официальном архиве не найден Codex CLI.')
                write_private(binary, package.extractfile(member).read())
            os.chmod(binary, 0o700)
        if '0.144.1' not in checked([str(binary), '--version']):
            raise UserError('Неверная версия закреплённого Codex CLI.')
        # Authentication is checked separately and never silently converted to an API key.
        result = {'version': VERSION, 'app': str(app), 'python': python, 'codex': str(binary), 'data': str(config.data), 'package_hash': tree_hash}
        if active and active != result:
            write_private(home / 'previous.json', json.dumps(active))
        write_private(active_path, json.dumps(result))
        config.save(python=python, codex_binary=str(binary), version=VERSION)
        return {**result, 'installed': True, 'next': 'bin/publisher init; telegram-login; doctor', 'live_acceptance_verified': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['install'])
    parser.add_argument('--source', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.source), ensure_ascii=False, indent=2))
    except UserError as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
