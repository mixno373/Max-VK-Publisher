import tempfile, unittest, json, time
from pathlib import Path
from postpult import Store,Engine,CATS,default_config
class LocalTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name));self.e=Engine(self.s)
 def tearDown(self):
  self.s.close();self.tmp.cleanup()
 def author(self,publish=True):
  self.s.execute('INSERT INTO users VALUES(?,?,?,?,?)',('42','Test','author',json.dumps([CATS[0]]),int(publish)))
 def test_encrypted_config_restart(self):
  cfg=self.s.config();cfg['incoming']='PRIVATE-TEST-TOKEN';self.s.set('config',cfg,True)
  raw=self.s.rows("SELECT v FROM kv WHERE k='config'")[0]['v'];self.assertNotIn('PRIVATE-TEST-TOKEN',raw)
  self.s.close();self.s=Store(Path(self.tmp.name));self.assertEqual(self.s.config()['incoming'],'PRIVATE-TEST-TOKEN')
 def test_pending_has_no_access(self):
  self.s.execute('INSERT INTO users VALUES(?,?,?,?,?)',('42','Test','pending','[]',0));self.assertFalse(self.s.permit('42'))
 def test_author_scopes_and_publish(self):
  self.author(False);self.assertTrue(self.s.permit('42',CATS[0]));self.assertFalse(self.s.permit('42',CATS[1]));self.assertFalse(self.s.permit('42',CATS[0],True))
 def test_draft_persistence_and_invalidation(self):
  self.author();d=self.s.draft('42');d['body']='Hello';d['checked']='old';r=d['revision'];self.s.save(d)
  got=self.s.draft('42');self.assertEqual(got['body'],'Hello');self.assertEqual(got['checked'],'');self.assertGreater(got['revision'],r)
 def test_footer_and_signature(self):
  self.author();d=self.s.draft('42');d['body']='Text';cfg=self.s.config();cfg['categories'][CATS[0]]['links']='https://example.org';self.s.set('config',cfg,True)
  self.assertEqual(self.e.compiled(d),'Text\n\nhttps://example.org');before=self.e.signature(d);d['body']='Changed';self.assertNotEqual(before,self.e.signature(d))
 def test_rutube_only_video(self):
  self.author();d=self.s.draft('42');cfg=self.s.config();cfg['categories'][CATS[0]]['routes']['RUTUBE']['enabled']=True;self.s.set('config',cfg,True)
  self.assertNotIn('RUTUBE',self.e.available(d));d['media']=[{'type':'video'}];self.assertIn('RUTUBE',self.e.available(d))
 def test_revoked_job_not_sent(self):
  self.author(False);self.s.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',('job','42',CATS[0],'Text','[]','["MAX"]',time.time()-1,'В очереди','{}'))
  self.e.dispatch();self.assertEqual(self.s.rows('SELECT status FROM jobs')[0]['status'],'Доступ отозван')
 def test_interrupted_job_requires_review(self):
  self.s.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',('job','42',CATS[0],'Text','[]','["MAX"]',0,'Отправляется','{}'))
  self.s.close();self.s=Store(Path(self.tmp.name));self.assertEqual(self.s.rows('SELECT status FROM jobs')[0]['status'],'Требует сверки')
if __name__=='__main__': unittest.main()
