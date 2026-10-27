import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path
from web.app import create_app
from web.demo import DemoPlatform
from web.config import CONFIG
from web.service import PlatformError

class OperationsTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.platform=DemoPlatform();self.app=create_app(Path(self.temp.name),platform=self.platform,auth=False);self.ops=self.app.extensions['operations'];self.s=self.app.extensions['workspace'];self.client=self.app.test_client()
 def tearDown(self):self.s.pool.shutdown(wait=True);self.temp.cleanup()
 def create(self):return self.ops.create({'name':'观察','kind':self.ops.capabilities()['kinds'][0]['id'],'target':'跑步','interval':300})['id']
 def run(self,result=None):
  # unittest owns run; explicit tick helper is below.
  return super().run(result)
 def tick_plan(self,pid):self.ops.change(pid,'start');self.ops.tick()
 def test_paused_default_and_stopped_do_not_read(self):
  pid=self.create();self.platform.search=Mock(side_effect=AssertionError('unexpected'));self.ops.tick();self.assertEqual(self.ops.snapshot()['plans'][0]['state'],'paused')
 def test_results_deduplicate_and_survive_recreation(self):
  pid=self.create();self.tick_plan(pid);snap=self.ops.snapshot();self.assertGreater(len(snap['events']),0)
  self.ops.change(pid,'pause');self.tick_plan(pid);self.assertEqual(len(self.ops.snapshot()['events']),len(snap['events']));self.assertEqual(self.ops.snapshot()['runs'][0]['added'],0)
  from web.operations import Operations
  self.assertEqual(len(Operations(self.s,CONFIG).snapshot()['events']),len(snap['events']))
 def test_account_isolation(self):
  pid=self.create();self.platform.account={**self.platform.account,'id':'other'};self.assertEqual(self.ops.snapshot()['plans'],[])
  with self.assertRaises(LookupError):self.ops.change(pid,'start')
  self.ops.tick();self.assertEqual(self.ops.snapshot()['events'],[])
 def test_failures_pause_until_explicit_recovery(self):
  pid=self.create();self.platform.search=Mock(side_effect=RuntimeError('secret raw message'));self.platform.tool=self.platform.search;self.tick_plan(pid);snap=self.ops.snapshot();self.assertEqual(snap['plans'][0]['state'],'attention');self.assertNotIn('secret',str(snap));self.ops.tick();self.assertEqual(self.platform.search.call_count,1)
 def test_validation(self):
  for interval in [0,True,'300',299,604801]:
   with self.assertRaises(PlatformError):self.ops.create({'name':'x','kind':self.ops.capabilities()['kinds'][0]['id'],'target':'x','interval':interval})
 def test_event_resolution_and_collection(self):
  pid=self.create();self.tick_plan(pid);eid=self.ops.snapshot()['events'][0]['id'];self.ops.event_action(eid,{'action':'done'});self.assertEqual(next(e for e in self.ops.snapshot()['events'] if e['id']==eid)['state'],'done');self.ops.event_action(eid,{'action':'collect'});self.assertTrue(self.s.records('item'))
 def test_http_guards(self):
  self.assertEqual(self.client.post('/api/operations/plans',json={}).status_code,403)
  authapp=create_app(Path(self.temp.name),platform=self.platform,auth=True);self.assertEqual(authapp.test_client().get('/api/operations').status_code,401);authapp.extensions['workspace'].pool.shutdown(wait=True)
 def test_no_platform_login_blocks_create(self):
  self.platform.account=None
  with self.assertRaises(PlatformError):self.create()
 @unittest.skipUnless(CONFIG['slug']=='bilibili','Bilibili-only outbound adapter')
 def test_comment_uncertain_is_not_retried(self):
  pid=self.create();self.tick_plan(pid);event=next(e for e in self.ops.snapshot()['events'] if e['payload'].get('aid'));cid=self.ops.event_action(event['id'],{'action':'draft','body':'测试草稿'})['id'];self.platform.post_comment=Mock(side_effect=TimeoutError())
  data={'confirm':True,'account_id':self.s.uid()};self.assertEqual(self.ops.send(cid,data)['state'],'uncertain');self.ops.send(cid,data);self.assertEqual(self.platform.post_comment.call_count,1)
 @unittest.skipUnless(CONFIG['slug']=='bilibili','Bilibili-only outbound adapter')
 def test_comment_explicit_account_review_and_idempotency(self):
  pid=self.create();self.tick_plan(pid);event=next(e for e in self.ops.snapshot()['events'] if e['payload'].get('aid'));cid=self.ops.event_action(event['id'],{'action':'draft','body':'测试草稿'})['id'];self.platform.post_comment=Mock(return_value={'id':'123'})
  with self.assertRaises(PlatformError):self.ops.send(cid,{'confirm':True,'account_id':'old'})
  self.platform.post_comment.assert_not_called();data={'confirm':True,'account_id':self.s.uid()};self.assertEqual(self.ops.send(cid,data)['state'],'submitted');self.ops.send(cid,data);self.assertEqual(self.platform.post_comment.call_count,1)

 def test_slow_platform_does_not_block_snapshot_or_pause(self):
  import threading,time
  entered=threading.Event();release=threading.Event();pid=self.create()
  def slow(*args,**kwargs):
   entered.set();release.wait(5);return {'items':[]}
  self.platform.search=Mock(side_effect=slow);self.platform.tool=self.platform.search
  self.ops.change(pid,'start');thread=threading.Thread(target=self.ops.tick);thread.start()
  self.assertTrue(entered.wait(2))
  try:
   started=time.monotonic();snapshot=self.ops.snapshot();self.assertLess(time.monotonic()-started,.3)
   self.assertEqual(snapshot['plans'][0]['state'],'running')
   started=time.monotonic();self.ops.change(pid,'pause');self.assertLess(time.monotonic()-started,.3)
  finally:release.set();thread.join(3)
  snapshot=self.ops.snapshot();self.assertEqual(snapshot['plans'][0]['state'],'paused');self.assertEqual(snapshot['runs'][0]['status'],'completed')
 def test_immediate_check_returns_to_paused_and_deduplicates(self):
  pid=self.create();self.ops.change(pid,'check');self.assertEqual(self.ops.snapshot()['plans'][0]['state'],'queued')
  self.ops.tick();snapshot=self.ops.snapshot();self.assertEqual(snapshot['plans'][0]['state'],'paused');self.assertGreater(len(snapshot['events']),0)
  self.ops.change(pid,'check');self.ops.tick();self.assertEqual(self.ops.snapshot()['runs'][0]['added'],0)
 def test_immediate_check_preserves_existing_schedule(self):
  pid=self.create();self.ops.change(pid,'start');self.ops.change(pid,'check');self.ops.tick()
  self.assertEqual(self.ops.snapshot()['plans'][0]['state'],'active')
 def test_pause_cancels_queued_check(self):
  pid=self.create();self.ops.change(pid,'check');self.ops.change(pid,'pause');self.platform.search=Mock();self.platform.tool=self.platform.search
  self.ops.tick();self.platform.search.assert_not_called()
 def test_deleted_inflight_plan_is_not_recreated(self):
  import threading
  entered=threading.Event();release=threading.Event();pid=self.create()
  def slow(*args,**kwargs):entered.set();release.wait(5);return {'items':[]}
  self.platform.search=Mock(side_effect=slow);self.platform.tool=self.platform.search
  self.ops.change(pid,'start');thread=threading.Thread(target=self.ops.tick);thread.start();self.assertTrue(entered.wait(2))
  try:self.ops.change(pid,'delete')
  finally:release.set();thread.join(3)
  self.assertEqual(self.ops.snapshot()['plans'],[])
 def test_scheduler_wakes_and_starts_only_once(self):
  import time
  pid=self.create();thread=self.ops.start_worker()
  try:
   self.assertIs(self.ops.start_worker(),thread);self.ops.change(pid,'check')
   deadline=time.monotonic()+3
   while time.monotonic()<deadline:
    if self.ops.snapshot()['runs']:break
    time.sleep(.01)
   self.assertEqual(len(self.ops.snapshot()['runs']),1);self.assertTrue(self.ops.snapshot()['worker']['running'])
  finally:self.ops.stop.set();self.ops.wake.set();thread.join(3)
 def test_oneoff_failure_requires_explicit_recovery(self):
  pid=self.create();self.platform.search=Mock(side_effect=RuntimeError('raw-secret'));self.platform.tool=self.platform.search
  self.ops.change(pid,'check');self.ops.tick();self.assertEqual(self.ops.snapshot()['plans'][0]['state'],'attention')
  self.ops.tick();self.assertEqual(self.platform.search.call_count,1)
 def test_collection_preserves_existing_research_notes(self):
  pid=self.create();self.tick_plan(pid);event=self.ops.snapshot()['events'][0];self.ops.event_action(event['id'],{'action':'collect'})
  item=self.s.records('item')[0];self.s.put('item',{**item,'note':'my note','tags':'research'})
  self.ops.event_action(event['id'],{'action':'collect'});self.assertEqual(self.s.records('item')[0]['note'],'my note')

 def test_platform_failure_explains_recovery(self):
  pid=self.create();self.platform.search=Mock(side_effect=PlatformError('平台限制，请检查登录状态'));self.platform.tool=self.platform.search
  self.ops.change(pid,'check');self.ops.tick();self.assertIn('请检查登录状态',self.ops.snapshot()['plans'][0]['error'])
 def test_pending_items_are_visible_before_done_history(self):
  pid=self.create();self.tick_plan(pid);pending=self.ops.snapshot()['events'][0]['id']
  with self.s.db() as db:
   for i in range(110):
    db.execute('INSERT INTO operating_events(uid,id,plan_id,source_id,payload,state,created) VALUES(?,?,?,?,?,?,?)',(self.s.uid(),'old-'+str(i),pid,'old-'+str(i),'{}','done',9999999999+i))
  snapshot=self.ops.snapshot();self.assertTrue(any(e['id']==pending for e in snapshot['events']));self.assertEqual(len(snapshot['events']),100)

if __name__=='__main__':unittest.main()
