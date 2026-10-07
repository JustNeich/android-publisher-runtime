"""Only plugin-owned temporary MP4s, after verified YouTube transfer."""
from pathlib import Path
from .adb import WirelessADB
from .config import UserError


def clean_media(config, db, video_id):
    video = db.one('SELECT * FROM videos WHERE id=?', (video_id,))
    if not video or video['state'] not in ('scheduled', 'published', 'cancelled') or not video['youtube_video_id']:
        return {'cleaned': False, 'reason': 'YouTube transfer is not confirmed'}
    pending = []
    if video['device_path']:
        channel = db.one('SELECT * FROM channels WHERE id=?', (video['channel_id'],))
        phone = db.one('SELECT * FROM phones WHERE id=?', (channel['phone_id'],))
        guard = None
        try:
            from .admin import ManualPhone
            # Every physical-phone operation, including housekeeping, uses
            # the same lease and lifecycle fence as foreground control.
            guard = ManualPhone(config, db, phone['id'], vault=None)
            WirelessADB(config, phone).remove_media(video_id)
            db.execute('UPDATE videos SET device_path=NULL WHERE id=?', (video_id,))
        except UserError:
            pending.append('phone')
        finally:
            if guard:
                guard.close()
    destination = config.data / 'media' / (video_id + '.mp4')
    # Never follow imported arbitrary media paths or remove a Telegram original.
    if not destination.is_symlink():
        try:
            destination.unlink(missing_ok=True)
            db.execute('UPDATE videos SET path=NULL WHERE id=?', (video_id,))
        except OSError:
            pending.append('mac')
    else:
        pending.append('mac')
    db.set('media_cleanup:' + video_id, pending)
    return {'cleaned': not pending, 'pending': pending, 'telegram_original_preserved': True}
