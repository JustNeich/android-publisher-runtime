"""Repeatable, isolated acceptance rehearsal. No Telegram or YouTube network calls."""
import json
import tempfile
from pathlib import Path
from .db import Store
from .engine import Engine
from .schedule import parse_iso
from .telegram import TelegramLoop


class SimulatedTelegram:
    def __init__(self):
        self.messages = {}; self.next_id = 100
    def call(self, method, data=None, **kwargs):
        if method == 'getMe': return {'id': 1, 'username': 'demo_publisher_bot'}
        if method == 'sendMessage':
            self.next_id += 1; self.messages[self.next_id] = dict(data)
            return {'message_id': self.next_id}
        if method == 'editMessageText':
            self.messages[data['message_id']].update(data); return True
        if method == 'answerCallbackQuery': return True
        raise RuntimeError('Demo forbids this transport method')
    def member(self, *args): return True


def simulate():
    now = [parse_iso('2026-10-05T18:30:00+03:00')]
    with tempfile.TemporaryDirectory(prefix='android-publisher-demo-') as temp:
        db = Store(Path(temp) / 'queue.sqlite3', lambda: now[0]); engine = Engine(db)
        db.execute('INSERT INTO phones VALUES(?,?,?,?,?)', ('demo', 'Simulated phone', 'http://192.168.1.2:8080/mcp', 'demo', now[0]))
        db.execute('INSERT INTO channels(id,name,youtube_id,phone_id,chat_id,general_topic,publication_topic,timezone,times,bound_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('demo', 'Демонстрация', 'UC' + 'd' * 22, 'demo', -1, 1, 2, 'Europe/Moscow', json.dumps(['19:00','20:00','21:00']), 0))
        telegram = SimulatedTelegram(); loop = TelegramLoop(engine, telegram)
        timeline = []
        ids = []
        for n in range(1, 4):
            ids.append(engine.ingest('demo', {'message_id': n, 'date': int(now[0]), 'caption': f'Название: Демо {n}\nОписание: Имитация\nТеги: demo\nАудитория: not_kids',
                                            'video': {'file_id': 'demo-' + str(n), 'file_unique_id': 'demo-' + str(n), 'mime_type': 'video/mp4', 'file_name': 'demo.mp4', 'file_size': 1024}}))
        loop.deliver(); first = ids[0]; card_id = engine.video(first)['card_message']
        timeline.append({'step': 'MP4 → отдельный ответ с Да/Нет', 'card': telegram.messages[card_id]['text'], 'reply_to': telegram.messages[card_id]['reply_parameters']['message_id']})
        for video in ids:
            engine.decision(video, 'yes', 0, 7)
            now[0] += .001
        loop.deliver(); timeline.append({'step': 'Да → FIFO и pending', 'queue': [{'title': v['metadata']['title'], 'time': v['time_label']} for v in engine.queue('demo')]})
        def finish(video_id, kind, outcome, youtube_id):
            job = engine.claim()
            assert job and (job['video_id'], job['kind']) == (video_id, kind), 'Unexpected demo job'
            evidence = 'simulation-' + job['id']
            # Simulated external evidence is only in this disposable database.
            db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)', (evidence, job['id'], 'android_get_screen_state', json.dumps({'SIMULATION': True}), now[0]))
            engine.complete(job['id'], job['lease'], outcome, evidence, youtube_id,
                            engine.video(video_id)['slot'] if outcome == 'scheduled' else None,
                            url='https://www.youtube.com/shorts/' + youtube_id if outcome == 'published' else None)

        for video_id, youtube_id in zip(ids, ('D' * 11, 'E' * 11, 'F' * 11)):
            finish(video_id, 'upload', 'scheduled', youtube_id)
        assert engine.claim() is None, 'An empty wait must not execute a job'
        loop.deliver(); timeline.append({'step': 'Все три загрузки сразу; выходы сохранены на будущее', 'card': telegram.messages[card_id]['text']})
        now[0] = engine.video(first)['slot']
        finish(first, 'verify', 'published', 'D' * 11)
        loop.deliver(); timeline.append({'step': 'Имитация опубликовано; та же карточка', 'card_id_preserved': engine.video(first)['card_message'] == card_id, 'card': telegram.messages[card_id]['text']})
        engine.cancel('demo', ids[1])
        finish(ids[1], 'cancel', 'private', 'E' * 11)
        finish(ids[2], 'reschedule', 'scheduled', 'F' * 11)
        loop.deliver()
        assert engine.video(ids[1])['state'] == 'cancelled' and engine.video(ids[2])['observed_slot'] == engine.video(ids[2])['slot']
        timeline.append({'step': 'Отмена второго → Private; перенос третьего подтверждён', 'queue': [{'title': v['metadata']['title'], 'time': v['time_label'], 'state': v['state']} for v in engine.queue('demo')]})
        db.close()
        return {'simulation': True, 'external_uploads': 0, 'external_messages': 0, 'real_queue_changes': 0, 'timeline': timeline}
