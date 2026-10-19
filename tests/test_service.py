import asyncio, base64, hashlib, hmac, json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.store import Store
from app.security import verify
from app.mapping import company_properties, person_properties, stamp
from app.clients import Forager, HubSpot, request, UpstreamError
from app.service import Service
from app.main import create_app

# Synthetic test data only; these are not actual employees.
ORG={'id':10,'name':'Example','domain':'https://example.com','description':'Fixture',
'employees_amount':20,'finance_info':{'revenue':1000},'linkedin_info':{'public_profile_url':'https://www.linkedin.com/company/example/'}}
ROLE={'role_title':'Software Engineer','is_current':True,'organization':ORG}
PERSON={'id':42,'first_name':'Test','last_name':'Person','description':'Fixture bio',
'linkedin_info':{'public_identifier':'fixture-person','public_profile_url':'https://www.linkedin.com/in/fixture-person/'},
'location':{'osm_locations':[{'place_type':'city','name':'Example City'}]},'roles':[ROLE]}

def settings(tmp_path):
    return Settings(hubspot_token='test',hubspot_secret='secret',portal_id='123',forager_key='test',
        account_id='456',admin_key='a'*32,db_path=str(tmp_path/'db.sqlite3'))

class FakeHubSpot:
    def __init__(self):
        self.contacts={}; self.companies={'1':{'id':'1','properties':{'domain':'example.com'}}}; self.links=[]
    async def get(self,kind,oid):
        return (self.contacts if kind=='contacts' else self.companies)[str(oid)]
    async def find(self,kind,prop,value):
        records=self.contacts if kind=='contacts' else self.companies
        return next((r for r in records.values() if str(r['properties'].get(prop))==str(value)),None)
    async def update(self,kind,oid,props):
        (await self.get(kind,oid))['properties'].update(props)
    async def create(self,kind,props,company_id=None):
        oid=str(len(self.contacts)+100)
        self.contacts[oid]={'id':oid,'properties':props.copy()}
        return self.contacts[oid]
    async def associate(self,cid,company_id):
        if (cid,company_id) not in self.links: self.links.append((cid,company_id))
    async def associated(self,kind,oid,target):
        return [c for c,company in self.links if company==oid]

class FakeForager:
    async def company(self,target,job): return ORG
    async def discover(self,target,titles,limit,job):
        return [{**ROLE,'person':{'id':42}}] if titles=='engineer' else []
    async def person(self,identity,job): return PERSON
    async def by_email(self,email,job): return PERSON
    async def phone(self,pid,job): return None
    async def email(self,person,domain,job): return 'fixture@example.com'

def test_mapping_missing_data():
    p=stamp(person_properties(PERSON,ROLE))
    assert p['city']=='Example City' and 'state' not in p and 'phone' not in p
    assert 'phone' in p['fg_missing_fields'] and p['fg_status']=='partial'
    assert company_properties(ORG)['annualrevenue']=='1000'

def test_queue_dedup_recovery_budget(tmp_path):
    st=Store(str(tmp_path/'db')); one=st.enqueue('company','1',dedupe='event1')
    assert st.enqueue('company','1',dedupe='event1')==one
    st.claim(); st.recover(); assert st.get(one)['status']=='queued'
    st.reserve(15,20)
    with pytest.raises(ValueError): st.reserve(15,20)
    assert st.spent()==15

def test_signatures_and_replay():
    body=b'[]'; secret='s'; url='https://example.com/webhooks/hubspot'; timestamp='1000000'
    expected=hashlib.sha256(secret.encode()+body).hexdigest()
    headers={'x-hubspot-signature-version':'v1','x-hubspot-signature':expected}
    assert verify(body,headers,'POST',url,secret)
    assert not verify(b'[1]',headers,'POST',url,secret)
    signature=base64.b64encode(hmac.new(secret.encode(),b'POST'+url.encode()+body+timestamp.encode(),hashlib.sha256).digest()).decode()
    headers={'x-hubspot-request-timestamp':timestamp,'x-hubspot-signature-v3':signature}
    assert verify(body,headers,'POST',url,secret,'v3',now=1000)
    assert not verify(body,headers,'POST',url,secret,'v3',now=1400)
    assert not verify(body,headers,'POST',url,secret,'v3',now=0)

def test_webhooks_and_admin_auth(tmp_path):
    s=settings(tmp_path); st=Store(s.db_path); svc=Service(s,st,FakeForager(),FakeHubSpot())
    with TestClient(create_app(s,svc,start_worker=False)) as client:
        event={'eventId':1,'objectId':1,'portalId':123,'subscriptionType':'company.creation'}
        body=json.dumps([event]).encode()
        headers={'x-hubspot-signature-version':'v1','x-hubspot-signature':hashlib.sha256(b'secret'+body).hexdigest()}
        a=client.post('/webhooks/hubspot',content=body,headers=headers)
        b=client.post('/webhooks/hubspot',content=body,headers=headers)
        assert a.status_code==200 and a.json()==b.json() and len(st.list_jobs())==1
        assert client.get('/admin/jobs').status_code==401
        assert client.get('/admin/jobs',headers={'Authorization':'Bearer '+'a'*32}).status_code==200
        assert client.post('/webhooks/hubspot',content=body).status_code==401
        event['portalId']=999; body=json.dumps([event]).encode()
        headers['x-hubspot-signature']=hashlib.sha256(b'secret'+body).hexdigest()
        assert client.post('/webhooks/hubspot',content=body,headers=headers).status_code==403

def test_company_discovery_dedup_and_loop_protection(tmp_path):
    async def run():
        s=settings(tmp_path); st=Store(s.db_path); h=FakeHubSpot(); svc=Service(s,st,FakeForager(),h)
        result=await svc.company('1','job1',{'titles':'engineer','discover':True})
        assert result['contact_count']==1 and result['contacts'][0]['status']=='partial'
        assert h.contacts['100']['properties']['company']=='Example' and h.links==[('100','1')]
        await svc.company('1','job1',{'titles':'engineer','discover':True})
        assert len(h.contacts)==1
        assert (await svc.contact('100','self-event'))['status']=='already_enriched'
        assert (await svc.company('1','other',{'titles':'CEO','discover':True}))['contact_count']==0
    asyncio.run(run())

def test_existing_contact_path(tmp_path):
    async def run():
        s=settings(tmp_path); h=FakeHubSpot(); st=Store(s.db_path)
        h.contacts['101']={'id':'101','properties':{'email':'fixture@example.com'}}; h.links=[('101','1')]
        result=await Service(s,st,FakeForager(),h).company('1','existing',{})
        assert result['contact_count']==1 and len(h.contacts)==1
        assert h.contacts['101']['properties']['jobtitle']=='Software Engineer'
    asyncio.run(run())

def test_forager_contract_poll_origin_and_checkpoints(tmp_path):
    seen=[]
    def handler(req):
        seen.append((req.method,req.url.path,json.loads(req.content) if req.content else {}))
        if req.url.path.endswith('/organization_search/'):
            return httpx.Response(200,json={'search_results':[ORG]})
        if req.url.path.endswith('/person_role_search/'):
            return httpx.Response(200,json={'search_results':[{**ROLE,'person':{'id':42}}],'total_search_results':1})
        if req.method=='POST':
            return httpx.Response(202,json={'job_id':'453bd7d7-5355-4d6d-a38e-d9e7eb218c3f','status_check_url':'https://evil.example/'})
        return httpx.Response(200,json={'status':'completed','emails':['fixture@example.com']})
    async def run():
        s=settings(tmp_path); st=Store(s.db_path)
        async with httpx.AsyncClient(base_url='https://api-v2.forager.ai',transport=httpx.MockTransport(handler)) as http:
            f=Forager(s,st,http)
            await f.company('example.com','job'); await f.company('example.com','job')
            await f.discover('example.com','engineer',5,'job')
            assert await f.email(PERSON,'example.com','job')=='fixture@example.com'
        assert len([r for r in seen if 'organization_search' in r[1]])==1
        assert seen[0][2]=={'domains':['example.com'],'page':0} and seen[1][2]['role_title']=='engineer'
        assert '/api/v2/456/' in seen[-1][1] and st.spent()==7
    asyncio.run(run())

@pytest.mark.parametrize('status,retry',[(429,True),(503,True),(401,False),(400,False)])
def test_http_error_classification(status,retry):
    async def run():
        async with httpx.AsyncClient(base_url='https://example.com',transport=httpx.MockTransport(lambda r:httpx.Response(status,json={'secret':'hidden'}))) as c:
            with pytest.raises(UpstreamError) as e: await request(c,'GET','/')
            assert e.value.retry==retry and 'secret' not in str(e.value)
    asyncio.run(run())

def test_hubspot_association_contract(tmp_path):
    calls=[]
    def handler(req):
        calls.append(req); return httpx.Response(200,json={'id':'100'})
    async def run():
        async with httpx.AsyncClient(base_url='https://api.hubapi.com',transport=httpx.MockTransport(handler)) as http:
            h=HubSpot(settings(tmp_path),http)
            await h.create('contacts',{'firstname':'Test'},'1'); await h.associate('100','1')
        assert json.loads(calls[0].content)['associations'][0]['types'][0]['associationTypeId']==279
        assert calls[1].url.path=='/crm/v4/objects/contacts/100/associations/default/companies/1'
    asyncio.run(run())

def test_five_contact_pipeline(tmp_path):
    class FiveForager(FakeForager):
        async def discover(self,target,titles,limit,job):
            return [{**ROLE,'person':{'id':i}} for i in range(1,limit+1)]
        async def person(self,identity,job):
            pid=identity['person_id']
            return {**PERSON,'id':pid,'first_name':f'Fixture{pid}',
                    'linkedin_info':{'public_identifier':f'fixture-{pid}','public_profile_url':f'https://www.linkedin.com/in/fixture-{pid}/'}}
        async def email(self,person,domain,job):
            return f"fixture{person['id']}@example.com"
    async def run():
        s=settings(tmp_path); h=FakeHubSpot(); st=Store(s.db_path)
        result=await Service(s,st,FiveForager(),h).company('1','five',{'titles':'engineer','limit':5,'discover':True})
        assert result['contact_count']==5 and len(h.contacts)==5 and len(h.links)==5
        assert all(r['status']=='partial' for r in result['contacts'])
        assert all('phone' in r['missing_fields'] for r in result['contacts'])
    asyncio.run(run())
