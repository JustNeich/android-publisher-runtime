"""One notification per incident transition, with durable delivery and scoped destinations."""
from .engine import uid


def report(config, db, key, reason=None, channel_id=None):
    old = db.one('SELECT * FROM incidents WHERE key=?', (key,))
    now = db.clock()
    state = 'open' if reason else 'resolved'
    if (not old and not reason) or (old and old['state'] == state and (not reason or old['reason'] == reason)):
        return False
    reason = reason or old['reason']
    db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET reason=excluded.reason,state=excluded.state,updated=excluded.updated',
               (key, channel_id, reason, state, now, now))
    channel = db.one('SELECT * FROM channels WHERE id=?', (channel_id,)) if channel_id else None
    chat = channel['chat_id'] if channel else config.settings.get('operator_chat')
    if chat is None:
        return False  # Persisted incident remains visible in status before operator authorization.
    text = ('Восстановлено: ' if state == 'resolved' else 'Нужна проверка: ') + reason
    db.execute('INSERT INTO outbox(id,channel_id,text,created,chat_id,topic_id) VALUES(?,?,?,?,?,?)',
               ('incident-' + uid(), channel_id or '', text, now, chat, channel['general_topic'] if channel else None))
    return True
