import json
import re
import secrets
import uuid
from .config import UserError
from .schedule import slots, validate, label

ACTIVE = ('pending', 'scheduled')
PHONE_KINDS = {'upload', 'verify', 'reschedule', 'cancel', 'publish_now', 'reconcile', 'inspect'}


def metadata_from_caption(caption):
    try:
        parsed = json.loads(caption)
        if isinstance(parsed, dict):
            return clean_metadata(parsed, complete=False)
    except (ValueError, UserError):
        pass
    pattern = re.compile(r'(?im)^\s*(title|название|description|описание|tags|теги|audience|аудитория)\s*:\s*')
    found = list(pattern.finditer(caption))
    result = {}
    aliases = {'название': 'title', 'описание': 'description', 'теги': 'tags', 'аудитория': 'audience'}
    for i, match in enumerate(found):
        key = aliases.get(match[1].lower(), match[1].lower())
        text = caption[match.end():found[i + 1].start() if i + 1 < len(found) else len(caption)].strip()
        result[key] = [v.strip() for v in text.split(',') if v.strip()] if key == 'tags' else text
    try:
        return clean_metadata(result, complete=False)
    except UserError:
        return {}


def clean_metadata(data, complete=True):
    if not isinstance(data, dict):
        raise UserError('Метаданные должны быть объектом.')
    result = {}
    for key, limit in (('title', 100), ('description', 5000)):
        if key in data:
            if not isinstance(data[key], str) or len(data[key]) > limit or (key == 'title' and not data[key].strip()):
                raise UserError('Некорректное поле ' + key + '.')
            result[key] = data[key].strip() if key == 'title' else data[key]
    if 'tags' in data:
        tags = data['tags']
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags) or sum(len(t) + 1 for t in tags) > 500:
            raise UserError('Нужен список тегов суммарно до 500 символов, включая пустой список.')
        result['tags'] = tags
    if 'audience' in data:
        if data['audience'] not in ('kids', 'not_kids'):
            raise UserError('Аудитория: kids или not_kids; при неоднозначности уточните.')
        result['audience'] = data['audience']
    if complete and set(result) != {'title', 'description', 'tags', 'audience'}:
        raise UserError('Нужны явные название, описание, теги и аудитория (либо согласованные defaults канала).')
    return result


def metadata_ready(data):
    try:
        clean_metadata(data)
        return True
    except UserError:
        return False


def uid():
    return uuid.uuid4().hex[:16]


class Engine:
    def __init__(self, store):
        self.db = store

    def video(self, video_id, channel=None):
        row = self.db.one('SELECT * FROM videos WHERE id=?', (video_id,))
        if not row or (channel and row['channel_id'] != channel):
            raise UserError('Ролик не найден в этом канале.')
        return row

    def channel(self, channel_id):
        row = self.db.one('SELECT * FROM channels WHERE id=?', (channel_id,))
        if not row:
            raise UserError('Канал ещё не подключён.')
        return row

    def enqueue(self, channel, kind, video=None, due=None, args=None):
        job = uid()
        revision = video['version'] if video else 0
        self.db.execute('INSERT OR IGNORE INTO jobs(id,channel_id,video_id,kind,revision,args,due,created) VALUES(?,?,?,?,?,?,?,?)',
                        (job, channel, video['id'] if video else None, kind, revision,
                         json.dumps(args or {}, ensure_ascii=False), due if due is not None else self.db.clock(), self.db.clock()))
        return job

    def ingest(self, channel_id, message, imported=False):
        channel = self.channel(channel_id)
        if not imported and message.get('date', self.db.clock()) < channel['bound_at']:
            return None
        media = message.get('video') or message.get('document')
        if not media:
            return None
        filename = media.get('file_name', f"video-{message['message_id']}.mp4")
        if media.get('mime_type') != 'video/mp4' and not filename.lower().endswith('.mp4'):
            return None
        caption = message.get('caption', '')
        metadata = {**json.loads(channel['defaults']), **metadata_from_caption(caption)}
        with self.db.transaction():
            existing = self.db.one('SELECT id FROM videos WHERE channel_id=? AND source_message=?', (channel_id, message['message_id']))
            if existing:
                return existing['id']
            import hashlib
            video_id = hashlib.sha256(f"{channel['youtube_id']}:{channel['chat_id']}:{message['message_id']}".encode()).hexdigest()[:16]
            self.db.execute('INSERT INTO videos(id,channel_id,source_message,file_id,file_unique_id,filename,file_size,caption,metadata,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (video_id, channel_id, message['message_id'], media['file_id'], media.get('file_unique_id', media['file_id']),
                             filename, media.get('file_size'), caption, json.dumps(metadata, ensure_ascii=False), self.db.clock()))
            self.db.execute('UPDATE videos SET source_chat=?,source_topic=?,source_document=?,source_revision=? WHERE id=?', (channel['chat_id'], channel['publication_topic'], str(media.get('document_id', '')), message.get('source_revision'), video_id))
            recovered = bool(self.db.setting('restored_at', 0) >= message.get('date', self.db.clock()))
            self.db.execute('UPDATE videos SET reconciliation_required=?,card_unknown=?,card_attempted=?,source_sender=?,source_sender_bot=?,source_outgoing=? WHERE id=?',
                            (recovered, recovered, self.db.clock() if recovered else None, message.get('from',{}).get('id'), message.get('from',{}).get('is_bot'), message.get('outgoing'), video_id))
            self.db.history(channel_id, 'imported' if imported else 'received', {'message_id': message['message_id']}, video_id)
            if not metadata_ready(metadata):
                self.enqueue(channel_id, 'metadata', self.video(video_id))
            return video_id

    def queue_work(self, video):
        if not metadata_ready(json.loads(video['metadata'])):
            self.enqueue(video['channel_id'], 'metadata', video)
            return
        if video['state'] in ACTIVE:
            if video['youtube_video_id']:
                if video['observed_slot'] != video['slot']:
                    self.enqueue(video['channel_id'], 'reschedule', video)
                else:
                    self.enqueue(video['channel_id'], 'verify', video, due=video['slot'])
            else:
                previous = self.db.one("SELECT id FROM jobs WHERE video_id=? AND kind IN ('upload','publish_now','reschedule','reconcile') AND attempts>0 LIMIT 1", (video['id'],))
                self.enqueue(video['channel_id'], 'upload', video, due=self.db.clock(), args={'reconcile_first': bool(previous) or bool(video['reconciliation_required'])})

    def next_slot(self, channel_id):
        channel = self.channel(channel_id)
        occupied = self.db.all("SELECT slot FROM videos WHERE channel_id=? AND state IN ('pending','scheduled','cancel_requested') AND slot IS NOT NULL", (channel_id,))
        after = max([self.db.clock()] + [v['slot'] for v in occupied])
        return next(slots(after, channel['timezone'], json.loads(channel['times'])))

    def decision(self, video_id, action, expected_version, actor):
        with self.db.transaction():
            video = self.video(video_id)
            if video['version'] != expected_version:
                raise UserError('Карточка уже обновилась; решение не повторено.')
            if action in ('yes', 'no') and video['state'] != 'review':
                raise UserError('Решение по ролику уже принято.')
            if action in ('now', 'requeue', 'cancel') and video['state'] != 'awaiting_decision':
                raise UserError('Эта кнопка больше не актуальна.')
            if action == 'no':
                self.db.execute("UPDATE videos SET state='rejected',version=version+1 WHERE id=?", (video_id,))
            elif action in ('yes', 'requeue'):
                if not video['youtube_video_id'] and (video['file_size'] is None or not 0 < video['file_size'] <= 2 * 1024 ** 3):
                    raise UserError('Для нового ролика нужен MP4 с подтверждённым размером до 2 ГБ. Пришлите подходящий исходник; слот не занят.')
                slot = self.next_slot(video['channel_id'])
                seq = self.db.one('SELECT coalesce(max(approval_seq),0)+1 AS n FROM videos')['n']
                self.db.execute("UPDATE videos SET state='pending',version=version+1,slot=?,approved_at=?,approval_seq=?,error=NULL WHERE id=?",
                                (slot, self.db.clock(), seq, video_id))
                self.queue_work(self.video(video_id))
            elif action == 'now':
                self.db.execute("UPDATE videos SET state='publish_requested',version=version+1,error=NULL WHERE id=?", (video_id,))
                self.enqueue(video['channel_id'], 'publish_now', self.video(video_id), args={'human_requested': True})
            elif action == 'cancel':
                self.cancel_in_transaction(video_id)
            else:
                raise UserError('Неизвестное действие.')
            self.db.history(video['channel_id'], 'decision', {'action': action, 'actor': actor}, video_id)
        return self.video(video_id)

    def cancel_in_transaction(self, video_id):
        video = self.video(video_id)
        if video['state'] == 'published':
            raise UserError('Ролик уже опубликован; отмена отложенного выхода недоступна.')
        if video['state'] in ('cancelled', 'rejected'):
            return
        started = self.db.one("SELECT id FROM jobs WHERE video_id=? AND kind IN ('upload','publish_now','reschedule','reconcile') AND (state IN ('running','uncertain') OR attempts>0) LIMIT 1", (video_id,))
        if video['youtube_video_id'] or started:
            self.db.execute("UPDATE videos SET state='cancel_requested',version=version+1 WHERE id=?", (video_id,))
            self.enqueue(video['channel_id'], 'cancel', self.video(video_id))
        else:
            self.db.execute("UPDATE videos SET state='cancelled',version=version+1,error=NULL WHERE id=?", (video_id,))
            self.repack_in_transaction(video['channel_id'])

    def cancel(self, channel_id, video_id):
        self.video(video_id, channel_id)
        with self.db.transaction():
            self.cancel_in_transaction(video_id)
            self.db.history(channel_id, 'cancel_requested', {}, video_id)
        return self.video(video_id)

    def repack_in_transaction(self, channel_id, ordered_ids=None):
        channel = self.channel(channel_id)
        videos = self.db.all("SELECT * FROM videos WHERE channel_id=? AND state IN ('pending','scheduled') ORDER BY approval_seq,id", (channel_id,))
        if ordered_ids is not None:
            if set(ordered_ids) != {v['id'] for v in videos} or len(ordered_ids) != len(videos):
                raise UserError('Для перестановки нужен каждый ожидающий ролик ровно один раз.')
            lookup = {v['id']: v for v in videos}
            videos = [lookup[i] for i in ordered_ids]
        # Keep overdue videos under their own one-hour policy; don't silently give them new slots.
        videos = [v for v in videos if v['slot'] is None or v['slot'] > self.db.clock()]
        reserved = {v['slot'] for v in self.db.all("SELECT slot FROM videos WHERE channel_id=? AND state='cancel_requested'", (channel_id,))}
        available = (s for s in slots(self.db.clock(), channel['timezone'], json.loads(channel['times'])) if s not in reserved)
        for i, video in enumerate(videos):
            slot = next(available)
            if slot != video['slot']:
                self.db.execute("UPDATE videos SET slot=?,state='pending',version=version+1 WHERE id=?", (slot, video['id']))
                self.queue_work(self.video(video['id']))
            if ordered_ids is not None:
                self.db.execute('UPDATE videos SET approval_seq=? WHERE id=?', (i + 1, video['id']))

    def reorder(self, channel_id, ordered_ids):
        with self.db.transaction():
            self.repack_in_transaction(channel_id, ordered_ids)
            self.db.history(channel_id, 'reordered', {'ids': ordered_ids})
        return self.queue(channel_id)

    def change_schedule(self, channel_id, timezone_name, times):
        times = validate(timezone_name, times)
        with self.db.transaction():
            self.db.execute('UPDATE channels SET timezone=?,times=? WHERE id=?', (timezone_name, json.dumps(times), channel_id))
            self.repack_in_transaction(channel_id)
            self.db.history(channel_id, 'schedule_changed', {'timezone': timezone_name, 'times': times})
        return self.queue(channel_id)

    def move(self, channel_id, video_id, stamp):
        video = self.video(video_id, channel_id)
        if video['state'] not in ACTIVE or stamp <= self.db.clock():
            raise UserError('Перенести можно ожидающий ролик на будущее время.')
        with self.db.transaction():
            conflict = self.db.one("SELECT id FROM videos WHERE channel_id=? AND id<>? AND slot=? AND state IN ('pending','scheduled','cancel_requested')", (channel_id, video_id, stamp))
            if conflict:
                raise UserError('Это время занято другим роликом; сначала измените порядок или время.')
            self.db.execute("UPDATE videos SET slot=?,state='pending',version=version+1 WHERE id=?", (stamp, video_id))
            self.queue_work(self.video(video_id))
            self.db.history(channel_id, 'moved', {'slot': stamp}, video_id)
        return self.video(video_id)

    def queue(self, channel_id):
        channel = self.channel(channel_id)
        result = self.db.all('SELECT * FROM videos WHERE channel_id=? ORDER BY slot IS NULL,slot,approval_seq,created', (channel_id,))
        for video in result:
            video['metadata'] = json.loads(video['metadata'])
            video['time_label'] = label(video['slot'], channel['timezone'])
        return result

    def set_metadata(self, channel_id, video_id, metadata):
        metadata = clean_metadata(metadata)
        with self.db.transaction():
            video = self.video(video_id, channel_id)
            if video['state'] not in ('review', 'pending') or video['youtube_video_id']:
                raise UserError('Данные уже загруженного ролика меняются только с проверкой YouTube.')
            self.db.execute('UPDATE videos SET metadata=?,version=version+1 WHERE id=?', (json.dumps(metadata, ensure_ascii=False), video_id))
            self.queue_work(self.video(video_id))
            self.db.history(channel_id, 'metadata_saved', {}, video_id)

    def recover_leases(self):
        now = self.db.clock()
        with self.db.transaction():
            self.db.execute("UPDATE jobs SET state='uncertain',lease=NULL,lease_until=NULL,due=?,error='Исполнитель прерван; сначала сверка результата' WHERE state='running' AND lease_until<?", (now, now))
            self.db.execute('DELETE FROM phone_locks WHERE until<?', (now,))

    def claim(self, lease_seconds=1800):
        now = self.db.clock()
        paused = self.db.setting('paused', False)
        with self.db.transaction():
            candidates = self.db.all("SELECT j.*,c.phone_id FROM jobs j JOIN channels c ON c.id=j.channel_id WHERE j.state IN ('queued','uncertain') AND j.due<=? AND ((c.paused=0 AND ?=0) OR j.kind IN ('general','metadata')) AND (j.kind IN ('general','metadata') OR NOT EXISTS(SELECT 1 FROM phone_locks l WHERE l.phone_id=c.phone_id AND l.until>?)) ORDER BY j.due,j.created LIMIT 1000", (now, paused, now))
            for job in candidates:
                if job['kind'] == 'general' and self.db.one("SELECT id FROM jobs WHERE channel_id=? AND kind='general' AND state='running' LIMIT 1", (job['channel_id'],)):
                    continue
                video = self.video(job['video_id']) if job['video_id'] else None
                if video and video['version'] != job['revision']:
                    self.db.execute("UPDATE jobs SET state='obsolete',completed=? WHERE id=?", (now, job['id']))
                    continue
                if video and job['kind'] in ('upload', 'reschedule') and now > video['slot'] + 3600:
                    if job['state'] == 'uncertain' or video['youtube_video_id'] or json.loads(job['args']).get('reconcile_first'):
                        job['kind'] = 'reconcile'
                    else:
                        self.db.execute("UPDATE videos SET state='awaiting_decision',version=version+1,error='Опоздание больше часа; нужно решение участника' WHERE id=?", (video['id'],))
                        self.db.execute("UPDATE jobs SET state='expired',completed=? WHERE id=?", (now, job['id']))
                        continue
                if job['kind'] in PHONE_KINDS:
                    lock = self.db.one('SELECT * FROM phone_locks WHERE phone_id=? AND until>?', (job['phone_id'], now))
                    if lock:
                        continue
                nonce = secrets.token_hex(24)
                job['reconcile_first'] = job['state'] == 'uncertain' or json.loads(job['args']).get('reconcile_first', False)
                self.db.execute("UPDATE jobs SET state='running',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?", (nonce, now + lease_seconds, job['id']))
                if job['kind'] in PHONE_KINDS:
                    self.db.execute('INSERT INTO phone_locks VALUES(?,?,?,?) ON CONFLICT(phone_id) DO UPDATE SET job_id=excluded.job_id,lease=excluded.lease,until=excluded.until',
                                    (job['phone_id'], job['id'], nonce, now + lease_seconds))
                job.update(lease=nonce, lease_until=now + lease_seconds)
                return job
        return None

    def valid_job(self, job_id, nonce):
        job = self.db.one("SELECT * FROM jobs WHERE id=? AND state='running' AND lease=? AND lease_until>?", (job_id, nonce, self.db.clock()))
        if not job:
            raise UserError('Задача завершена или право её выполнения истекло.')
        if job['video_id'] and self.video(job['video_id'])['version'] != job['revision']:
            raise UserError('Решение или время изменилось; прекратите прежнее действие и сохраните доказательства.')
        return job

    def complete(self, job_id, nonce, outcome, evidence_id=None, youtube_id=None, observed_slot=None, url=None, error=None):
        with self.db.transaction():
            job = self.valid_job(job_id, nonce)
            video = self.video(job['video_id']) if job['video_id'] else None
            if job['kind'] in PHONE_KINDS:
                proof = self.db.one('SELECT * FROM evidence WHERE id=? AND job_id=?', (evidence_id, job_id))
                if not proof or not proof['tool'].endswith('_get_screen_state') or self.db.clock() - proof['created'] > 300:
                    raise UserError('Нужно свежее чтение экрана как доказательство результата.')
            allowed = {'inspect': {'done'}, 'general': {'done'}, 'metadata': {'done'}, 'cancel': {'private','absent','awaiting_decision'}, 'verify': {'published','awaiting_decision','scheduled'}, 'upload': {'scheduled','published','awaiting_decision'}, 'reschedule': {'scheduled','published','awaiting_decision'}, 'reconcile': {'scheduled','published','awaiting_decision'}, 'publish_now': {'published','awaiting_decision'}}
            if outcome not in allowed.get(job['kind'], set()):
                raise UserError('Этот результат не подходит назначенной задаче.')
            if outcome in ('scheduled', 'published', 'private') and (not youtube_id or not re.fullmatch(r'[A-Za-z0-9_-]{11}', youtube_id)):
                raise UserError('Нужен проверенный идентификатор видео YouTube.')
            if outcome == 'scheduled':
                if not video or video['state'] not in ACTIVE or observed_slot != video['slot']:
                    raise UserError('Время YouTube должно совпасть с назначенным временем.')
                if observed_slot <= self.db.clock():
                    raise UserError('Время уже наступило; проверьте фактическую публикацию.')
                self.db.execute("UPDATE videos SET state='scheduled',youtube_video_id=?,observed_slot=?,error=NULL,version=version+1 WHERE id=?", (youtube_id, observed_slot, video['id']))
                self.queue_work(self.video(video['id']))
            elif outcome == 'published':
                expected = 'https://www.youtube.com/shorts/' + youtube_id
                if url not in (expected, 'https://www.youtube.com/watch?v=' + youtube_id):
                    raise UserError('Нужна зрительская ссылка на этот ролик.')
                self.db.execute("UPDATE videos SET state='published',youtube_video_id=?,published_at=?,url=?,error=NULL,version=version+1 WHERE id=?", (youtube_id, self.db.clock(), url, video['id']))
            elif outcome == 'private':
                if video['state'] != 'cancel_requested':
                    raise UserError('Private ожидается для отменяемого ролика.')
                self.db.execute("UPDATE videos SET state='cancelled',youtube_video_id=?,observed_slot=NULL,error=NULL,version=version+1 WHERE id=?", (youtube_id, video['id']))
                self.repack_in_transaction(video['channel_id'])
            elif outcome == 'absent':
                if video['state'] != 'cancel_requested' or video['youtube_video_id'] or not json.loads(job['args']).get('reconciliation_checked'):
                    raise UserError('Отсутствие ролика требует свежей сверки Studio; известный YouTube ID отменяется через Private.')
                self.db.execute("UPDATE videos SET state='cancelled',error=NULL,version=version+1 WHERE id=?", (video['id'],))
                self.repack_in_transaction(video['channel_id'])
            elif outcome == 'awaiting_decision':
                self.db.execute("UPDATE videos SET state='awaiting_decision',error=?,version=version+1 WHERE id=?", (error or 'Нужно решение участника', video['id']))
            elif outcome != 'done':
                raise UserError('Неизвестный результат задачи.')
            self.db.execute("UPDATE jobs SET state='done',completed=?,lease=NULL,lease_until=NULL WHERE id=?", (self.db.clock(), job_id))
            if video and outcome in ('scheduled','published','private','absent'):
                self.db.execute('UPDATE videos SET reconciliation_required=0 WHERE id=?', (video['id'],))
            self.db.execute('DELETE FROM phone_locks WHERE job_id=? AND lease=?', (job_id, nonce))
            self.db.history(job['channel_id'], 'job_completed', {'kind': job['kind'], 'outcome': outcome, 'evidence': evidence_id}, job['video_id'])

    def fail(self, job, reason):
        with self.db.transaction():
            current = self.db.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
            if not current or current['state'] != 'running' or current['lease'] != job['lease']:
                return
            if current['video_id'] and self.video(current['video_id'])['version'] != current['revision']:
                self.db.execute("UPDATE jobs SET state='obsolete',completed=?,error=?,lease=NULL,lease_until=NULL WHERE id=?", (self.db.clock(), reason, job['id']))
                self.db.execute('DELETE FROM phone_locks WHERE job_id=? AND lease=?', (job['id'], job['lease']))
                return
            due = self.db.clock() + min(900, 15 * 2 ** min(current['attempts'], 6))
            exhausted = current['attempts'] >= 5
            self.db.execute("UPDATE jobs SET state=?,due=?,error=?,lease=NULL,lease_until=NULL WHERE id=?", ('blocked' if exhausted else 'uncertain', due, reason, job['id']))
            self.db.execute('DELETE FROM phone_locks WHERE job_id=? AND lease=?', (job['id'], job['lease']))
            if job['video_id']:
                self.db.execute('UPDATE videos SET error=? WHERE id=?', (reason, job['video_id']))
            self.db.history(job['channel_id'], 'job_failed', {'kind': job['kind'], 'reason': reason}, job['video_id'])
