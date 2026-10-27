"""Read-only product and order workspace over cv-cat/JdApis."""
import base64
import json
import os
from pathlib import Path
import re
import secrets
import time
from html import unescape
from urllib.parse import unquote,urlsplit
from loguru import logger
from builder.auth import JdAuth
from jd_apis.jd_api import JdAPI
from jd_apis.jd_login_api import JdLoginAPI
from utils import http_client
from .service import PlatformError

logger.disable('')  # Upstream diagnostic responses can include private session data.
_original_send=http_client._send

def _verified_send(method,url,**kwargs):
    kwargs['verify']=True
    kwargs.setdefault('timeout',20)
    return _original_send(method,url,**kwargs)
http_client._send=_verified_send


def text(value): return unescape(re.sub('<[^>]+>','',str(value or '')))


def image(value):
    value=str(value or '')
    if value.startswith('//'):value='https:'+value
    if value and not value.startswith('http'):value='https://img10.360buyimg.com/n7/'+value
    return value if value.startswith('https://') else ''


def checked(raw):
    if not isinstance(raw,dict) or raw.get('_error') or raw.get('disposal') or str(raw.get('code','0')) not in ('0','200'):
        raise PlatformError('京东接口未确认请求，请检查账号或在官网完成验证后重试')
    return raw.get('data') or raw


def projection(raw):
    sku=str(raw.get('wareId') or raw.get('skuId') or raw.get('id') or '')
    return {'id':sku,'title':text(raw.get('wareName') or raw.get('name') or raw.get('skuName')),
        'text':text(raw.get('description') or raw.get('promoDesc') or ''),'author':text(raw.get('shopName') or raw.get('shop')),
        'uid':str(raw.get('venderId') or ''),'price':str(raw.get('jdPrice') or raw.get('finalPrice') or raw.get('price') or ''),
        'cover':image(raw.get('imageurl') or raw.get('imageUrl') or raw.get('imgUrl') or raw.get('img')),
        'url':f'https://item.jd.com/{sku}.html','views':raw.get('good','—'),'likes':'—','comments':raw.get('comment',0),'tags':'','note':''}


class Platform:
    def __init__(self,directory):
        self.directory=Path(directory);self.path=self.directory/'account.json';self.auth_path=self.directory/'jd-auth.json';self.auth=None;self.qr={}

    def identity(self):return json.loads(self.path.read_text()) if self.path.exists() else None

    def new_auth(self,path=None):
        auth=JdAuth();auth._storage_path=path or self.auth_path
        return auth

    def client(self):
        if self.auth is None:self.auth=JdAuth.load(self.auth_path) if self.auth_path.exists() else self.new_auth()
        return self.auth

    def persist(self,auth,name):
        account={'id':unquote(auth.pin),'name':text(name or unquote(auth.pin)),'status':'connected','checked_at':time.time()}
        auth.save(self.auth_path)
        temporary=self.path.with_suffix('.tmp');fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:json.dump(account,f,ensure_ascii=False)
        os.replace(temporary,self.path);self.auth=auth
        return account

    def connect(self,cookie):
        pending=self.directory/('incoming-'+secrets.token_hex(8)+'.json')
        auth=self.new_auth(pending).prepare_auth(cookie)
        try:
            ok,name=JdAPI.check_session(auth)
            if not ok:raise PlatformError('京东 Cookie 未通过账号检查，请导入 PC 网页登录后的 thor / pin Cookie')
            return self.persist(auth,name)
        finally:pending.unlink(missing_ok=True)

    def check(self):
        if not self.identity():raise PlatformError('请先连接京东账号')
        ok,name=JdAPI.check_session(self.client())
        if not ok:raise PlatformError('京东登录已失效，请重新扫码或导入 Cookie')
        return self.persist(self.client(),name)

    def disconnect(self):
        self.path.unlink(missing_ok=True);self.auth_path.unlink(missing_ok=True);self.auth=None;self.qr.clear()

    def search(self,keyword,page,order):
        data=checked(JdAPI.search(self.client(),keyword,page=page,sort='' if order=='default' else order,retry=0,auto_verify=False))
        if 'wareList' not in data:raise PlatformError('京东没有返回商品列表，请先连接账号并在官网确认可正常搜索')
        items=[projection(x) for x in data.get('wareList') or []]
        return {'items':items,'total':data.get('resultCount',len(items)),'has_more':page*30<int(data.get('resultCount') or 0)}

    @staticmethod
    def resolve(value):
        if re.fullmatch(r'\d{5,24}',value):return value
        u=urlsplit(value)
        if u.scheme!='https' or u.hostname!='item.jd.com' or u.port not in (None,443) or u.username or u.password:raise PlatformError('请输入 SKU 或 https://item.jd.com/商品编号.html 链接')
        match=re.fullmatch(r'/(\d{5,24})\.html',u.path)
        if not match:raise PlatformError('没有读取到有效 SKU')
        return match.group(1)

    def detail(self,value):
        sku=self.resolve(value);data=checked(JdAPI.get_product_detail(self.client(),sku,retry=0))
        ware=data.get('wareInfo') or {};basic=ware.get('basicInfo') or {};price=data.get('price') or {}
        if not isinstance(price,dict):price={'p':price}
        raw={**basic,**{k:v for k,v in data.items() if isinstance(v,(str,int,float))},'skuId':sku}
        raw['wareName']=raw.get('name') or raw.get('skuName') or ware.get('name') or data.get('skuName')
        if not raw['wareName']:raise PlatformError('商品详情未返回名称，请确认 SKU 与账号状态')
        raw['jdPrice']=price.get('p') or price.get('price') or raw.get('jdPrice','')
        raw['shopName']=(data.get('shopInfo') or {}).get('shopName') or raw.get('shopName','')
        raw['imageurl']=(ware.get('images') or {}).get('image') if isinstance(ware.get('images'),dict) else raw.get('imageurl')
        raw['description']=text(data.get('skuName') or basic.get('name') or '')
        return projection(raw)

    def comments(self,item):
        data=checked(JdAPI.get_product_comments(self.client(),item['id'],count=10))
        rows=data.get('comments') or (data.get('commentInfo') or {}).get('comments') or []
        if not isinstance(rows,list):raise PlatformError('商品评论格式已变化，请检查上游接口')
        return {'comments':[{'id':str(x.get('id','')),'author':text(x.get('nickname') or x.get('userNickname')),
            'text':text(x.get('content') or x.get('commentData')),'likes':x.get('usefulVoteCount',0)} for x in rows], 'notice':'展示本次接口返回的商品评论'}

    def user(self,uid,page):raise PlatformError('京东使用商品和订单查询，请打开专项工具')
    def publish(self,content):raise PlatformError('京东工作台仅保存本地草稿')

    def tool(self,name,data):
        if not self.identity():raise PlatformError('请先连接京东账号')
        page=int(data.get('page',1))
        if not 1<=page<=50:raise PlatformError('页码需要 1–50')
        if name=='orders':
            raw=JdAPI.get_order_list(self.client(),page=page,date_range=str(data.get('range','1')))
            checked(raw)
            items=[{'id':'order-'+x['orderId'],'kind':'order','title':' / '.join(x.get('products') or ['订单 '+x['orderId']]),'author':'订单 '+x['orderId'],
                'text':f"{x.get('status','')} · {x.get('time','')}",'url':x['url'],'price':x.get('amount','').replace('¥',''),
                'views':'—','likes':'—','comments':0,'uid':'','cover':'','tags':'','note':''} for x in raw.get('orders') or []]
            return {'items':items,'notice':'只读取订单列表；未执行下单、支付或订单修改'}
        if name=='followed':
            raw=checked(JdAPI.get_follow_products(self.client(),page=page,page_size=20,retry=0))
            rows=raw.get('list') or raw.get('wareList') or raw.get('data') or []
            if not isinstance(rows,list):raise PlatformError('关注商品列表格式已变化')
            return {'items':[projection(x) for x in rows],'notice':'来自当前京东账号的关注商品'}
        raise PlatformError('工具不存在')

    def qr_start(self):
        auth=self.new_auth(self.directory/'jd-qr-auth.json');session=http_client.session(verify=True)
        original=session.request
        def request(method,url,**kwargs):kwargs['verify']=True;kwargs.setdefault('timeout',15);return original(method,url,**kwargs)
        session.request=request
        ok,_,png=JdLoginAPI.get_qrcode(auth,session=session)
        if not ok:raise PlatformError('京东二维码申请失败，请稍后重试')
        token=secrets.token_urlsafe(24);self.qr={token:{'auth':auth,'session':session,'expires':time.time()+110,'last':0}}
        return {'token':token,'image':'data:image/png;base64,'+base64.b64encode(png).decode(),'status':'waiting'}

    def qr_poll(self,token):
        pending=self.qr.get(token)
        if not pending or time.time()>pending['expires']:return {'status':'expired'}
        if time.time()-pending['last']<3:return {'status':'waiting'}
        pending['last']=time.time();raw=JdLoginAPI.check_qrcode(pending['auth'],session=pending['session'])
        if raw.get('code')==200 and raw.get('ticket'):
            ok,_,_=JdLoginAPI.validate_ticket(pending['auth'],raw['ticket'],session=pending['session'])
            if not ok:raise PlatformError('扫码票据未通过校验，请重新生成二维码')
            ok,name=JdAPI.check_session(pending['auth'])
            if not ok:raise PlatformError('扫码后账号检查未通过，请重新扫码')
            account=self.persist(pending['auth'],name);self.qr.clear();(self.directory/'jd-qr-auth.json').unlink(missing_ok=True)
            return {'status':'connected','account':account}
        return {'status':{201:'waiting',202:'scanned',203:'expired'}.get(raw.get('code'),'waiting')}
