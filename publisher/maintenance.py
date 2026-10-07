import json
import shutil
import time
from .config import write_private
from .lifecycle import snapshot, verify_snapshot, prune_automatic_snapshots
from .retention import clean_media


def maintain(config, db):
    now = db.clock()
    if now - db.setting('last_housekeeping', 0) < 60:
        return
    db.set('last_housekeeping', now)
    if now - db.setting('last_backup', 0) >= 86400:
        target = config.data / 'backups' / ('daily-' + time.strftime('%Y-%m-%d', time.gmtime(now)))
        if target.exists():
            verify_snapshot(target)
        else:
            snapshot(config, target)
        db.set('last_backup', now)
        prune_automatic_snapshots(config)
    for video in db.all("SELECT id FROM videos WHERE youtube_video_id IS NOT NULL AND state IN ('scheduled','published','cancelled') AND (path IS NOT NULL OR device_path IS NOT NULL) LIMIT 3"):
        clean_media(config, db, video['id'])
    for path in (config.data / 'logs').glob('*.log'):
        if path.stat().st_size > 5 * 1024 * 1024:
            with path.open('rb+') as stream:
                stream.seek(-1024 * 1024, 2); tail = stream.read(); stream.seek(0); stream.write(tail); stream.truncate()
    completed = db.all('SELECT id FROM runs WHERE ended IS NOT NULL ORDER BY ended DESC')
    for row in completed[50:]:
        path = config.data / 'runs' / row['id']
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
