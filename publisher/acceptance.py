"""Collect observable local evidence; a report is not independent release acceptance."""
import json
import platform
import plistlib
import time
from pathlib import Path
from .config import UserError, write_private
from .db import Store
from .install import doctor
from .onboard import verify_phone


def desktop_applications():
    """Installed version metadata is evidence of a build, not of visible UI."""
    applications = []
    for parent in (Path('/Applications'), Path.home() / 'Applications'):
        for name in ('ChatGPT.app', 'Codex.app'):
            info = parent / name / 'Contents/Info.plist'
            if not info.is_file():
                continue
            try:
                with info.open('rb') as stream:
                    metadata = plistlib.load(stream)
                applications.append({'name': metadata.get('CFBundleDisplayName', name[:-4]),
                                     'bundle_id': metadata.get('CFBundleIdentifier'),
                                     'version': metadata.get('CFBundleShortVersionString'),
                                     'build': metadata.get('CFBundleVersion')})
            except (OSError, ValueError, plistlib.InvalidFileException):
                applications.append({'name': name[:-4], 'metadata_unavailable': True})
    return applications


def report(config):
    health = doctor(config)
    db = Store(config.database)
    try:
        devices = []
        for phone in db.all('SELECT * FROM phones'):
            try:
                wifi = verify_phone(config, phone['id'])
            except UserError as error:
                wifi = {'verified': False, 'reason': str(error)}
            devices.append({'phone_id': phone['id'], 'serial': phone['serial'], 'wifi': wifi})
        channels = db.all('SELECT id,name,youtube_id,phone_id,chat_id,publication_topic,timezone,times FROM channels')
        videos = db.all('SELECT id,channel_id,source_chat,source_topic,source_message,source_document,source_sender,source_sender_bot,source_outgoing,state,slot,observed_slot,youtube_video_id,url,card_message,sha256,media_validated,path,device_path FROM videos ORDER BY created DESC LIMIT 200')
        jobs = db.all("SELECT id,channel_id,video_id,kind,state,args,completed FROM jobs WHERE state='done' ORDER BY completed DESC LIMIT 300")
        screens = db.all('SELECT id,job_id,tool,payload,created FROM evidence ORDER BY created DESC LIMIT 100')
        history = db.all('SELECT channel_id,video_id,event,payload,created FROM history ORDER BY id DESC LIMIT 300')
        conversation = db.all('SELECT channel_id,role,message_id,created FROM conversation ORDER BY id DESC LIMIT 100')
        result = {'schema':1,'captured_at':time.time(),'version':config.settings.get('version'), 'installation_id':config.instance,
                  'host':{'system':platform.system(),'machine':platform.machine(),'python':platform.python_version()},
                  'native_plugin_ui':{'applications':desktop_applications(),
                                     'plugin_id':'android-publisher@android-publisher-local',
                                     'evidence_scope':'Build metadata only; visible card, connect button, conversation and new chat require separate observation.'},
                  'doctor':health,'devices':devices,'channels':channels,'videos':videos,'jobs':jobs,'screens':screens,
                  'history':history,'conversation':conversation,'live_acceptance_verified':False,
                  'remaining_verification':'Independent Telegram and public YouTube readback; two real phones, recovery, update and rollback receipts.'}
        output = config.data / 'acceptance-report.json'
        write_private(output,json.dumps(result,ensure_ascii=False,indent=2))
        result['saved_report'] = str(output)
        return result
    finally:
        db.close()
