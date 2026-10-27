import json
from pathlib import Path
import tempfile
import time
import unittest
from web.app import create_app
from web.demo import DemoPlatform,fixtures
from web.service import Service,UncertainError
from web.config import CONFIG


class PlatformFake(DemoPlatform):
    demo=False
    def connect(self,cookie):
        self.account={'id':cookie,'name':'测试账号 '+cookie,'status':'connected'}
        return self.account
    def publish(self,content):
        self.publish_calls+=1
        if content=='timeout':raise UncertainError()
        return {'id':'100001','url':'https://example.com/item/100001'}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.platform=PlatformFake()
        self.app=create_app(self.tmp.name,self.platform,auth=False,public_origin='https://test.example')
        self.client=self.app.test_client();self.service=self.app.extensions['workspace']
        self.headers={'Origin':'https://test.example','X-App-Request':'1'}
    def tearDown(self):
        self.service.pool.shutdown(wait=True);self.tmp.cleanup()
    def post(self,path,data=None,method='POST'):
        return self.client.open(path,method=method,json=data or {},headers=self.headers)
    def wait(self,job):
        for _ in range(200):
            result=self.client.get('/api/jobs/'+job['id']).json
            if result['status']!='running':return result
            time.sleep(.01)
        self.fail('任务没有结束')
    def test_auth_and_origin(self):
        guarded=create_app(Path(self.tmp.name)/'auth',self.platform,auth=True,public_origin='https://test.example')
        c=guarded.test_client()
        try:
            self.assertEqual(c.get('/').status_code,302)
            self.assertEqual(c.get('/api/meta').status_code,401)
            self.assertEqual(c.post('/api/login',json={'password':'wrong'}).status_code,403)
            pw=(Path(self.tmp.name)/'auth/admin-password').read_text()
            self.assertEqual(c.post('/api/login',json={'password':pw},headers=self.headers).status_code,200)
            self.assertEqual(c.get('/api/meta').status_code,200)
            self.assertEqual(c.post('/api/logout',json={},headers={'Origin':'https://evil.example','X-App-Request':'1'}).status_code,403)
        finally:guarded.extensions['workspace'].pool.shutdown(wait=True)
    def test_security_headers_and_private_files(self):
        response=self.client.get('/')
        self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual((Path(self.tmp.name)/'admin-password').stat().st_mode&0o777,0o600)
        self.assertEqual(self.client.get('/.local/web/admin-password').status_code,404)
    def test_search_deduplicates(self):
        data={'keyword':'研究','order':CONFIG['orders'][0][0],'request_id':'search-12345'}
        first=self.post('/api/searches',data).json
        second=self.post('/api/searches',data).json
        self.assertEqual(first['id'],second['id'])
        final=self.wait(first);self.assertEqual(final['status'],'completed')
        self.assertTrue(final['result']['items'])
    def test_invalid_input(self):
        self.assertEqual(self.post('/api/searches',{'keyword':'x','order':'invalid','request_id':'invalid-12345'}).status_code,400)
        self.assertEqual(self.post('/api/searches',{'keyword':'x','page':999,'order':CONFIG['orders'][0][0],'request_id':'invalid-12345'}).status_code,400)
        self.assertEqual(self.post('/api/searches',{'keyword':'x','order':CONFIG['orders'][0][0]}).status_code,400)
    def test_library_notes_export_and_account_isolation(self):
        item=fixtures()[0];self.service.put('result',item)
        self.assertEqual(self.post('/api/library/'+item['id']).status_code,200)
        self.post('/api/library/'+item['id'],{'tags':'研究','note':'保留观察'},method='PATCH')
        self.assertEqual(self.client.get('/api/library').json['items'][0]['note'],'保留观察')
        self.assertEqual(json.loads(self.client.get('/api/export').data)[0]['tags'],'研究')
        self.post('/api/account/import',{'cookie':'other-account'})
        self.assertEqual(self.client.get('/api/library').json['items'],[])
        self.assertEqual(self.post('/api/library/'+item['id']).status_code,400)
    def test_jobs_are_account_scoped(self):
        job=self.post('/api/details',{'value':'some-item','request_id':'detail-12345'}).json
        self.wait(job)
        self.post('/api/account/import',{'cookie':'other-account'})
        self.assertEqual(self.client.get('/api/jobs/'+job['id']).status_code,404)
    def test_drafts_do_not_publish(self):
        response=self.post('/api/drafts',{'text':'一条本地草稿','title':'想法'})
        self.assertEqual(response.status_code,200);self.assertEqual(self.platform.publish_calls,0)
        self.assertEqual(self.post('/api/drafts/'+response.json['id']+'/publish',{'request_id':'publish-12345'}).status_code,400)
        self.assertEqual(self.platform.publish_calls,0)
    def test_publish_is_explicit_and_deduplicated(self):
        if not CONFIG['publish']:self.skipTest('此平台只支持本地草稿')
        draft=self.post('/api/drafts',{'text':'已确认的正文'}).json
        body={'confirm':True,'request_id':'publish-12345'}
        job=self.post('/api/drafts/'+draft['id']+'/publish',body).json
        final=self.wait(job);self.assertEqual(final['status'],'completed')
        again=self.post('/api/drafts/'+draft['id']+'/publish',body)
        self.assertEqual(again.status_code,202);self.assertEqual(again.json['id'],job['id'])
        self.assertEqual(self.platform.publish_calls,1)
    def test_uncertain_publish_is_not_retried(self):
        if not CONFIG['publish']:self.skipTest('此平台只支持本地草稿')
        draft=self.post('/api/drafts',{'text':'timeout'}).json
        job=self.post('/api/drafts/'+draft['id']+'/publish',{'confirm':True,'request_id':'publish-timeout'}).json
        self.assertEqual(self.wait(job)['status'],'uncertain')
        self.assertEqual(self.post('/api/drafts/'+draft['id']+'/publish',{'confirm':True,'request_id':'publish-timeout-new'}).status_code,400)
        self.assertEqual(self.platform.publish_calls,1)
        self.assertEqual(self.client.get('/api/drafts').json['items'][0]['status'],'uncertain')
    def test_restart_marks_unfinished_operations_uncertain(self):
        self.service.put('draft',{'id':'pending-draft','text':'pending','status':'publishing'})
        with self.service.db() as db:db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',('pending','demo-10001','pending-id','发布','running','{}',time.time()))
        replacement=Service(self.tmp.name,self.platform)
        try:
            self.assertEqual(replacement.get_job('pending')['status'],'uncertain')
            self.assertEqual(replacement.record('draft','pending-draft')['status'],'uncertain')
        finally:replacement.pool.shutdown(wait=True)
    def test_batch_collection_preserves_notes_and_is_atomic(self):
        values=fixtures()[:2]
        for item in values:self.service.put('result',item)
        self.assertEqual(self.post('/api/library/batch',{'ids':[x['id'] for x in values]}).status_code,200)
        self.post('/api/library/'+values[0]['id'],{'note':'保留备注','tags':'原标签'},method='PATCH')
        self.post('/api/library/batch',{'ids':[values[0]['id']]})
        self.assertEqual(self.service.record('item',values[0]['id'])['note'],'保留备注')
        self.service.remove('item',values[1]['id'])
        response=self.post('/api/library/batch',{'ids':[values[1]['id'],'missing-result']})
        self.assertEqual(response.status_code,400)
        with self.assertRaises(LookupError):self.service.record('item',values[1]['id'])
    def test_csv_preserves_unicode_and_prevents_formula_injection(self):
        self.service.put('item',{'id':'csv-item','title':'=unsafe()','text':'中文正文'})
        response=self.client.get('/api/export?format=csv')
        self.assertEqual(response.status_code,200)
        content=response.data.decode('utf-8-sig')
        self.assertIn("'=unsafe()",content);self.assertIn('中文正文',content)
        self.assertIn('.csv',response.headers['Content-Disposition'])

    def test_tool_allowlist(self):
        self.assertEqual(self.post('/api/tools/unknown',{'request_id':'tool-12345'}).status_code,404)
        if CONFIG['tools']:
            response=self.post('/api/tools/'+CONFIG['tools'][0]['id'],{'request_id':'tool-12345'})
            self.assertEqual(response.status_code,202);self.assertEqual(self.wait(response.json)['status'],'completed')
    def test_demo_rejects_real_credentials(self):
        with self.assertRaises(ValueError):DemoPlatform().connect('secret-cookie')
        with self.assertRaises(ValueError):DemoPlatform().qr_start()

    def test_comments_from_saved_owned_snapshot(self):
        item=fixtures()[0]
        self.service.put('item',item)
        job=self.post('/api/comments',{'id':item['id'],'request_id':'saved-comment-check'}).json
        self.assertEqual(self.wait(job)['status'],'completed')
        self.platform.account={'id':'another-owner','name':'Other'}
        self.assertEqual(self.post('/api/comments',{'id':item['id'],'request_id':'other-comment-check'}).status_code,404)


if __name__=='__main__':unittest.main()
