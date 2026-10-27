import tempfile
import unittest
from unittest.mock import Mock, patch
from web.app import create_app
from web.demo import DemoPlatform
from web.channel_client import ChannelError
from web.config import CONFIG


class AgentDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.platform=DemoPlatform()
        self.app=create_app(self.tmp.name,self.platform,auth=False,public_origin='https://test.example')
        self.service=self.app.extensions['workspace'];self.client=self.app.test_client()
        self.channel=Mock(source='jd');self.channel.catalog.return_value={'agents':[{'id':'new-agent','name':'新建未路由 Agent'}],'routes':[]}
        self.channel.bind.return_value={'route':{'id':'own-route','version':1,'agent_id':'new-agent'},'agent':{'id':'new-agent','name':'新建未路由 Agent'}}
        self.channel.send.return_value={'id':'event-1','state':'queued'};self.channel.status.return_value={'state':'succeeded','result':{'output':{'text':'合成结果'}}}
        self.patch=patch('web.channel_bridge.ChannelClient',return_value=self.channel);self.patch.start()
    def tearDown(self):
        self.patch.stop();self.service.pool.shutdown(wait=True);self.tmp.cleanup()
    def post(self,path,data):return self.client.post(path,json=data,headers={'Origin':'https://test.example','X-App-Request':'1'})
    def test_new_unrouted_agent_live_refresh_failure_and_selection(self):
        self.assertEqual(self.client.get('/api/channel/catalog').json['agents'][0]['name'],'新建未路由 Agent')
        self.channel.catalog.return_value['agents'][0]['name']='改名 Agent'
        self.assertEqual(self.client.get('/api/channel/catalog').json['agents'][0]['name'],'改名 Agent')
        self.channel.catalog.side_effect=ChannelError('目录不可用')
        failure=self.client.get('/api/channel/catalog')
        self.assertEqual(failure.status_code,502)
        self.assertEqual(failure.json['error'],'目录不可用')
        self.assertEqual(self.post('/api/channel/messages',{'message_id':'synthetic-request'}).status_code,400)
        self.channel.send.assert_not_called()
    def test_explicit_binding_and_immutable_own_history(self):
        response=self.post('/api/channel/messages',{'message_id':'synthetic-request','agent_id':'new-agent'})
        self.assertEqual(response.status_code,202,response.json)
        self.assertEqual(self.channel.send.call_args.kwargs['route_id'],'own-route')
        self.assertEqual(self.channel.bind.call_args.args,(self.service.uid(),'content','new-agent'))
        self.assertEqual(self.client.get('/api/channel/messages/event-1').json['agent_snapshot']['agent_name'],'新建未路由 Agent')
        self.channel.bind.return_value['agent']['name']='后来改名'
        self.post('/api/channel/messages',{'message_id':'synthetic-request','agent_id':'new-agent'})
        self.assertEqual(self.client.get('/api/channel/history').json['items'][0]['agent_name'],'新建未路由 Agent')
        self.platform.account={'id':'other','name':'另一个账号'}
        self.assertEqual(self.client.get('/api/channel/history').json['items'],[])
        self.assertEqual(self.post('/api/channel/bindings',{'account_id':'foreign','purpose':'content','agent_id':'new-agent'}).status_code,400)
    def test_category_purpose_contract(self):
        for category,purpose in [('comment_analysis','comment'),('product_research','research')]:
            response=self.post('/api/channel/messages',{'message_id':'synthetic-'+purpose,'agent_id':'new-agent','category':category})
            self.assertEqual(response.status_code,202,response.json)
            self.assertEqual(self.channel.bind.call_args.args[1],purpose)
            self.assertEqual(self.channel.send.call_args.kwargs['surface'],'comment' if category=='comment_analysis' or self.channel.source=='zhihu' else 'content')

    def test_unique_account_name_defaults_preserve_explicit_selection(self):
        expected=CONFIG['name'].removesuffix('工作台')+'-'+self.platform.account['name']
        self.channel.catalog.return_value['agents']=[{'id':'matched','name':expected}]
        self.assertEqual(self.client.get('/api/channel/catalog').json['bindings']['content']['agent_id'],'matched')
        self.service.put('channel-binding',{'id':'content','agent_id':'explicit'})
        self.assertEqual(self.client.get('/api/channel/catalog').json['bindings']['content']['agent_id'],'explicit')
        self.channel.catalog.return_value['agents'].append({'id':'duplicate','name':expected})
        self.assertIsNone(self.client.get('/api/channel/catalog').json['bindings']['comment'])
