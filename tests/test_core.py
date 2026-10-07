import json,tempfile,unittest
from pathlib import Path
from publisher.db import Store
from publisher.engine import Engine,metadata_from_caption
from publisher.config import UserError
from publisher.schedule import parse_iso
from publisher.telegram import TelegramLoop,TelegramError

class FakeBot:
 def __init__(self): self.calls=[];self.fail=None
 def member(self,*a): return True
 def call(self,n,d=None,**kw):
  self.calls.append((n,d))
  if n=='getMe': return {'id':77,'username':'Test_bot'}
  if self.fail==n: raise TelegramError('unknown',uncertain=True)
  return {'message_id':900+len(self.calls)} if n=='sendMessage' else True

class Core(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.now=parse_iso('2026-10-05T18:30:00+03:00');self.path=Path(self.t.name)/'db';self.db=Store(self.path,lambda:self.now);self.e=Engine(self.db)
  self.db.execute('INSERT INTO phones VALUES(?,?,?,?,?)',('p','Test','http://192.168.1.2:8080/mcp','s',self.now))
  self.db.execute('INSERT INTO channels(id,name,youtube_id,phone_id,chat_id,general_topic,publication_topic,timezone,times,bound_at) VALUES(?,?,?,?,?,?,?,?,?,?)',('c','Test','UC'+'a'*22,'p',-100,1,5,'Europe/Moscow',json.dumps(['19:00','20:00','21:00']),0))
  self.bot=FakeBot();self.tg=TelegramLoop(self.e,self.bot)
 def tearDown(self): self.db.close();self.t.cleanup()
 def video(self,n=1): return self.e.ingest('c',{'message_id':n,'date':int(self.now),'caption':'Название: Video '+str(n)+'\nОписание: Description\nТеги: alpha, beta\nАудитория: not_kids','video':{'file_id':'f'+str(n),'file_unique_id':'u'+str(n),'file_name':'test.mp4','mime_type':'video/mp4','file_size':1}})
 def yes(self,v): return self.e.decision(v,'yes',self.e.video(v)['version'],123)
 def proof(self,j,tool='android_get_screen_state'):
  key='proof'+j['id'];self.db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(key,j['id'],tool,'[]',self.now));return key
 def test_fifo(self):
  a,b=self.video(1),self.video(2);self.yes(b);self.yes(a);self.assertEqual(self.e.video(b)['slot'],parse_iso('2026-10-05T19:00:00+03:00'));self.assertEqual(self.e.video(a)['slot'],parse_iso('2026-10-05T20:00:00+03:00'))
 def test_next_day(self):
  for n in range(4): self.yes(self.video(n))
  self.assertEqual(self.e.queue('c')[3]['slot'],parse_iso('2026-10-06T19:00:00+03:00'))
 def test_upload_immediately_after_approval(self):
  self.now=parse_iso('2026-10-05T16:00:00+03:00');self.yes(self.video());self.assertEqual(self.e.claim()['kind'],'upload')
 def test_duplicate_yes(self):
  v=self.video();self.yes(v)
  with self.assertRaises(UserError): self.e.decision(v,'yes',0,456)
 def test_duplicate_ingest(self): self.assertEqual(self.video(),self.video());self.assertEqual(len(self.e.queue('c')),1)
 def test_cancel_advances(self):
  a,b=self.video(1),self.video(2);self.yes(a);self.yes(b);self.e.cancel('c',a);self.assertEqual(self.e.video(b)['slot'],parse_iso('2026-10-05T19:00:00+03:00'))
 def test_unknown_cancel_private(self):
  v=self.video();self.yes(v);j=self.e.claim();self.e.fail(j,'network');self.e.cancel('c',v);self.assertEqual(self.e.video(v)['state'],'cancel_requested');self.assertEqual(self.e.claim()['kind'],'cancel')
 def test_revision_invalidates_worker(self):
  v=self.video();self.yes(v);j=self.e.claim();self.e.move('c',v,self.now+9000)
  with self.assertRaises(UserError): self.e.valid_job(j['id'],j['lease'])
  self.assertIsNone(self.e.claim())
 def test_expired_lease_reconciles(self):
  self.yes(self.video());j=self.e.claim(30);self.now+=31;self.e.recover_leases();new=self.e.claim();self.assertTrue(new['reconcile_first']);self.assertNotEqual(new['lease'],j['lease'])
 def test_one_phone_worker(self):
  self.yes(self.video(1));self.yes(self.video(2));self.now+=3600;self.assertIsNotNone(self.e.claim());self.assertIsNone(self.e.claim())
 def test_paused_general(self):
  self.db.set('paused',True);self.db.execute('UPDATE channels SET paused=1');self.e.enqueue('c','general');self.assertEqual(self.e.claim()['kind'],'general')
 def test_general_serialized(self):
  self.e.enqueue('c','general');self.e.enqueue('c','general');self.assertIsNotNone(self.e.claim());self.assertIsNone(self.e.claim())
 def test_over_hour_wait_human(self):
  v=self.video();self.yes(v);self.now=parse_iso('2026-10-05T20:01:00+03:00');self.assertIsNone(self.e.claim());self.assertEqual(self.e.video(v)['state'],'awaiting_decision')
 def test_within_hour(self):
  self.yes(self.video());self.now=parse_iso('2026-10-05T19:59:59+03:00');self.assertEqual(self.e.claim()['kind'],'upload')
 def test_late_unknown_reconcile(self):
  self.yes(self.video());j=self.e.claim();self.e.fail(j,'network');self.now=parse_iso('2026-10-05T20:01:00+03:00');self.assertEqual(self.e.claim()['kind'],'reconcile')
 def test_exact_schedule_evidence(self):
  v=self.video();self.yes(v);j=self.e.claim();p=self.proof(j)
  with self.assertRaises(UserError): self.e.complete(j['id'],j['lease'],'scheduled',p,'a'*11,self.now+100)
  self.e.complete(j['id'],j['lease'],'scheduled',p,'a'*11,self.e.video(v)['slot']);self.assertEqual(self.e.video(v)['state'],'scheduled')
 def test_no_phone_done(self):
  self.yes(self.video());j=self.e.claim()
  with self.assertRaises(UserError): self.e.complete(j['id'],j['lease'],'done',self.proof(j))
 def test_fake_evidence_rejected(self):
  self.yes(self.video());j=self.e.claim()
  with self.assertRaises(UserError): self.e.complete(j['id'],j['lease'],'published',self.proof(j,'android_tap'),'a'*11,url='https://www.youtube.com/shorts/'+'a'*11)
 def test_stale_evidence(self):
  self.yes(self.video());j=self.e.claim();p=self.proof(j);self.now+=301
  with self.assertRaises(UserError): self.e.complete(j['id'],j['lease'],'published',p,'a'*11,url='https://www.youtube.com/shorts/'+'a'*11)
 def test_same_reply_card(self):
  v=self.video();self.tg.deliver();mid=self.e.video(v)['card_message'];send=[c for c in self.bot.calls if c[0]=='sendMessage'][0][1];self.assertEqual(send['reply_parameters']['message_id'],1)
  self.yes(v);self.tg.deliver();self.assertEqual(self.e.video(v)['card_message'],mid);self.assertEqual(len([c for c in self.bot.calls if c[0]=='sendMessage']),1)
 def test_uncertain_card_no_resend(self):
  v=self.video();self.bot.fail='sendMessage'
  with self.assertRaises(TelegramError): self.tg.deliver()
  self.bot.fail=None;self.tg.deliver();self.assertEqual(len([c for c in self.bot.calls if c[0]=='sendMessage']),1);self.assertEqual(self.e.video(v)['card_unknown'],1)
 def test_wrong_group_callback(self):
  v=self.video();self.tg.deliver();self.tg.process({'callback_query':{'id':'q','data':v+':yes:0','from':{'id':1},'message':{'chat':{'id':-200},'message_id':self.e.video(v)['card_message']}}});self.assertEqual(self.e.video(v)['state'],'review')
 def test_restart_and_backup(self):
  v=self.video();self.yes(v);self.db.close();self.db=Store(self.path,lambda:self.now);self.e=Engine(self.db);self.assertEqual(self.e.video(v)['state'],'pending');self.db.backup(Path(self.t.name)/'backup')
 def test_move_after_attempt_requires_reconcile(self):
  v=self.video();self.yes(v);j=self.e.claim();self.e.fail(j,'unknown');self.e.move('c',v,self.now+1800);new=self.e.claim();self.assertTrue(new['reconcile_first'])
 def test_multiline_metadata(self): self.assertEqual(metadata_from_caption('Название: Test\nОписание: First\nSecond\nТеги: x, y')['description'],'First\nSecond')
 def test_old_event_ignored(self): self.db.execute('UPDATE channels SET bound_at=?',(self.now+10,));self.assertIsNone(self.video())

if __name__=='__main__': unittest.main()
