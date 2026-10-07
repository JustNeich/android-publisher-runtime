"""Check imports, ChatGPT and Keychain under a real temporary LaunchAgent."""
import argparse
import hashlib
import json
import os
import plistlib
import secrets
import subprocess
import sys
import time
from pathlib import Path
from .config import Config, UserError, write_private
from .vault import Vault, configured_names


def child(config, ticket):
    import ssl, sqlite3, pyexpat, zoneinfo, telethon
    ticket = json.loads(Path(ticket).read_text())
    vault = Vault(config.keychain_service)
    inaccessible = []
    try:
        keychain = hashlib.sha256(vault.get(ticket['key']).encode()).hexdigest() == ticket['sha256']
    except UserError:
        keychain = False
    for name in configured_names(config):
        try:
            vault.has(name)  # A missing optional key is distinct from denied access.
        except UserError:
            inaccessible.append(name)
    keychain = keychain and not inaccessible
    env = {k:v for k,v in os.environ.items() if k not in ('OPENAI_API_KEY','CODEX_API_KEY','OPENAI_BASE_URL')}
    result = subprocess.run([config.settings['codex_binary'],'login','status'],capture_output=True,text=True,timeout=20,env=env)
    proof = {'checked_at':time.time(),'python':sys.version.split()[0],'imports':True,'keychain':keychain,
             'chatgpt':result.returncode == 0 and 'ChatGPT' in result.stdout+result.stderr,'pid':os.getpid(),'launchd':True,'secrets_returned':False,
             'inaccessible_key_names': inaccessible}
    proof['passed'] = all(proof[k] for k in ('imports','keychain','chatgpt'))
    write_private(Path(ticket['proof']),json.dumps(proof,indent=2))


def run(config):
    if sys.platform != 'darwin':
        raise UserError('Фоновая проверка предназначена macOS.')
    directory = config.data / 'preflight'; directory.mkdir(exist_ok=True,mode=0o700)
    key = 'preflight-' + secrets.token_hex(10)
    label = 'org.androidpublisher.preflight.' + secrets.token_hex(10)
    value = secrets.token_urlsafe(32)
    vault = Vault(config.keychain_service); vault.put(key,value)
    ticket = directory / (label + '.json')
    proof = directory / (label + '-proof.json')
    write_private(ticket,json.dumps({'key':key,'sha256':hashlib.sha256(value.encode()).hexdigest(),'proof':str(proof)}))
    path = directory / (label + '.plist')
    write_private(path,plistlib.dumps({'Label':label,'ProgramArguments':[sys.executable,'-m','publisher.preflight','--root',str(config.root),'--ticket',str(ticket)],
        'WorkingDirectory':str(config.root),'RunAtLoad':True,'EnvironmentVariables':{'PYTHONPATH':str(config.root),'ANDROID_PUBLISHER_DATA':str(config.data)},
        'StandardOutPath':str(directory/'stdout.log'),'StandardErrorPath':str(directory/'stderr.log')}))
    domain = f'gui/{os.getuid()}'
    try:
        result = subprocess.run(['launchctl','bootstrap',domain,str(path)],capture_output=True,timeout=15)
        if result.returncode:
            raise UserError('launchd не принял проверочный job.')
        for _ in range(100):
            if proof.exists():
                result = json.loads(proof.read_text()); result['proof'] = str(proof)
                if result['passed']:
                    config.save(background_preflight={**result, 'runtime': str(config.root)})
                return result
            time.sleep(.3)
        raise UserError('Фоновый job не сохранил proof. Проверьте доступ Keychain/вход ChatGPT и локальный preflight stderr.')
    finally:
        subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True,timeout=15)
        ticket.unlink(missing_ok=True); path.unlink(missing_ok=True)
        vault.delete(key)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--root'); parser.add_argument('--ticket',required=True)
    args = parser.parse_args()
    child(Config(args.root),args.ticket)
