import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phones(id TEXT PRIMARY KEY,name TEXT NOT NULL,endpoint TEXT NOT NULL,serial TEXT,added REAL NOT NULL);
CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY,name TEXT NOT NULL,youtube_id TEXT NOT NULL,phone_id TEXT NOT NULL UNIQUE REFERENCES phones(id),chat_id INTEGER UNIQUE NOT NULL,general_topic INTEGER NOT NULL,publication_topic INTEGER NOT NULL,timezone TEXT NOT NULL,times TEXT NOT NULL,paused INTEGER NOT NULL DEFAULT 0,bound_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY,channel_id TEXT NOT NULL REFERENCES channels(id),source_message INTEGER NOT NULL,file_id TEXT NOT NULL,file_unique_id TEXT NOT NULL,filename TEXT NOT NULL,file_size INTEGER,caption TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT '{}',path TEXT,sha256 TEXT,state TEXT NOT NULL DEFAULT 'review',version INTEGER NOT NULL DEFAULT 0,approved_at REAL,approval_seq INTEGER,slot REAL,observed_slot REAL,youtube_video_id TEXT,published_at REAL,url TEXT,error TEXT,card_message INTEGER,card_unknown INTEGER NOT NULL DEFAULT 0,card_version INTEGER NOT NULL DEFAULT -1,card_dirty INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,UNIQUE(channel_id,source_message));
CREATE TRIGGER IF NOT EXISTS videos_card_dirty AFTER UPDATE OF metadata,state,version,slot,observed_slot,error,published_at,url ON videos BEGIN UPDATE videos SET card_dirty=1 WHERE id=NEW.id; END;
CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,processed INTEGER NOT NULL DEFAULT 0,received REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,channel_id TEXT NOT NULL REFERENCES channels(id),video_id TEXT REFERENCES videos(id),kind TEXT NOT NULL,revision INTEGER NOT NULL,args TEXT NOT NULL DEFAULT '{}',due REAL NOT NULL,state TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,lease TEXT,lease_until REAL,error TEXT,created REAL NOT NULL,completed REAL);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,due);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_revision ON jobs(video_id,kind,revision) WHERE video_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS phone_locks(phone_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,lease TEXT NOT NULL,until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),tool TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id TEXT NOT NULL,video_id TEXT,event TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversation(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id TEXT NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,message_id INTEGER,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,reply_to INTEGER,text TEXT NOT NULL,message_id INTEGER,state TEXT NOT NULL DEFAULT 'queued',error TEXT,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,started REAL NOT NULL,ended REAL,result TEXT);
CREATE TABLE IF NOT EXISTS ingress(channel_id TEXT PRIMARY KEY REFERENCES channels(id),cursor INTEGER NOT NULL DEFAULT 0,boundary INTEGER NOT NULL DEFAULT 0,import_state TEXT NOT NULL DEFAULT 'offered',updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(key TEXT PRIMARY KEY,channel_id TEXT,reason TEXT NOT NULL,state TEXT NOT NULL,opened REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_peers(chat_id INTEGER PRIMARY KEY,access_hash TEXT NOT NULL,kind TEXT NOT NULL,account_id INTEGER NOT NULL);
PRAGMA user_version=3;
'''


class Store:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        version = self.conn.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2, 3):
            raise RuntimeError('Несовместимая версия базы; сначала нужна миграция.')
        if version == 1 and 'card_dirty' not in {r[1] for r in self.conn.execute('PRAGMA table_info(videos)')}:
            self.conn.execute('ALTER TABLE videos ADD COLUMN card_dirty INTEGER NOT NULL DEFAULT 1')
        self.conn.executescript(SCHEMA)
        additions = {'channels': {'defaults': "TEXT NOT NULL DEFAULT '{}'"},
                     'videos': {'source_chat': 'INTEGER', 'source_topic': 'INTEGER', 'source_document': 'TEXT',
                                'source_revision': 'TEXT', 'device_path': 'TEXT', 'media_validated': 'TEXT',
                                'reconciliation_required': 'INTEGER NOT NULL DEFAULT 0', 'card_attempted': 'REAL',
                                'source_sender': 'INTEGER', 'source_sender_bot': 'INTEGER', 'source_outgoing': 'INTEGER'},
                     'outbox': {'chat_id': 'INTEGER', 'topic_id': 'INTEGER', 'attempted': 'REAL'}}
        for table, columns in additions.items():
            present = {r[1] for r in self.conn.execute('PRAGMA table_info(' + table + ')')}
            for name, spec in columns.items():
                if name not in present:
                    self.conn.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' ' + spec)
        self.conn.execute('UPDATE videos SET source_chat=(SELECT chat_id FROM channels WHERE id=videos.channel_id),source_topic=(SELECT publication_topic FROM channels WHERE id=videos.channel_id) WHERE source_chat IS NULL')
        try:
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS physical_phone ON phones(serial) WHERE serial IS NOT NULL AND serial<>''")
            self.conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS youtube_channel ON channels(youtube_id)')
        except sqlite3.IntegrityError:
            self.conn.close()
            from .config import UserError
            raise UserError('В прежней базе один физический телефон или UC ID записан несколько раз. Резервную копию сохраните, затем согласуйте единственную связку до миграции.') from None
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def transaction(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield self
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.conn.execute(sql, args)]

    def execute(self, sql, args=()):
        return self.conn.execute(sql, args)

    def setting(self, key, default=None):
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        return json.loads(row['value']) if row else default

    def set(self, key, value):
        self.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))

    def history(self, channel, event, payload, video=None):
        self.execute('INSERT INTO history(channel_id,video_id,event,payload,created) VALUES(?,?,?,?,?)',
                     (channel, video, event, json.dumps(payload, ensure_ascii=False), self.clock()))

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(destination) as target:
            self.conn.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Проверка резервной копии не пройдена.')
        os.chmod(destination, 0o600)

    def close(self):
        self.conn.close()
