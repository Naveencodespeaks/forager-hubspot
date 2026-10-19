import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager, suppress
from typing import Annotated
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from .config import Settings
from .store import Store
from .clients import Forager, HubSpot, UpstreamError
from .service import Service
from .security import verify

class EnrichOptions(BaseModel):
    titles: str = Field(default='engineer',min_length=1,max_length=300)
    limit: int = Field(default=5,ge=1,le=25)
    discover: bool = False

def create_app(settings=None, service=None, start_worker=True):
    s = settings or Settings()
    @asynccontextmanager
    async def lifespan(app):
        s.validate()
        st = service.store if service else Store(s.db_path)
        svc = service or Service(s,st,Forager(s,st),HubSpot(s))
        app.state.service = svc
        st.recover()
        task = asyncio.create_task(svc.worker()) if start_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if not service:
                await svc.f.http.aclose()
                await svc.h.http.aclose()
    app = FastAPI(title='Forager × HubSpot',version='1.0.0',lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)
    def admin(credentials: Annotated[HTTPAuthorizationCredentials | None,Depends(bearer)]):
        if not credentials or not hmac.compare_digest(credentials.credentials,s.admin_key):
            raise HTTPException(401,'Invalid admin key')
    protected = [Depends(admin)]

    @app.get('/health')
    async def health():
        return {'status':'ok'}

    @app.post('/webhooks/hubspot')
    async def webhook(request: Request):
        body = await request.body()
        if len(body)>1024*1024:
            raise HTTPException(413,'Payload too large')
        uri = s.public_url+request.url.path
        if request.url.query:
            uri += '?'+request.url.query
        if not verify(body,request.headers,request.method,uri,s.hubspot_secret,s.signature_version):
            raise HTTPException(401,'Invalid webhook signature')
        try:
            events = json.loads(body)
        except ValueError:
            raise HTTPException(400,'Invalid JSON')
        if not isinstance(events,list) or len(events)>100:
            raise HTTPException(400,'Expected up to 100 subscription events')
        accepted = []
        for e in events:
            if not isinstance(e,dict) or str(e.get('portalId')) != s.portal_id:
                raise HTTPException(403,'Unexpected portal')
            if e.get('subscriptionType') in ('company.creation','contact.creation'):
                if not str(e.get('objectId','')).isdigit() or 'eventId' not in e:
                    raise HTTPException(400,'Missing objectId/eventId')
                accepted.append(e)
        jobs=[]
        for e in accepted:
            key = ':'.join(str(e.get(k,'')) for k in ('portalId','subscriptionId','eventId','objectId','subscriptionType','occurredAt'))
            jobs.append(app.state.service.store.enqueue(e['subscriptionType'].split('.')[0],e['objectId'],dedupe=key))
        return {'accepted':len(jobs),'job_ids':jobs}

    @app.post('/admin/setup',dependencies=protected)
    async def setup():
        try:
            await app.state.service.h.setup()
        except UpstreamError as exc:
            raise HTTPException(502,str(exc))
        return {'status':'custom properties ready'}

    @app.post('/admin/companies/{company_id}/enrich',dependencies=protected,status_code=202)
    async def enrich_company(company_id: int, options: EnrichOptions):
        job = app.state.service.store.enqueue('company',company_id,options.model_dump())
        return {'job_id':job}

    @app.post('/admin/contacts/{contact_id}/enrich',dependencies=protected,status_code=202)
    async def enrich_contact(contact_id: int):
        return {'job_id':app.state.service.store.enqueue('contact',contact_id)}

    @app.get('/admin/jobs',dependencies=protected)
    async def jobs():
        st=app.state.service.store
        return {'jobs':st.list_jobs(),'reserved_credits':st.spent(),'budget':s.budget}

    @app.get('/admin/jobs/{job_id}',dependencies=protected)
    async def job(job_id: str):
        result = app.state.service.store.get(job_id)
        if not result:
            raise HTTPException(404,'Unknown job')
        return result

    @app.post('/admin/jobs/{job_id}/retry',dependencies=protected)
    async def retry(job_id: str):
        st=app.state.service.store
        with st.connect() as db:
            updated=db.execute("UPDATE jobs SET status='queued',attempts=0,available=0 WHERE id=? AND status='failed'",(job_id,)).rowcount
        if not updated:
            raise HTTPException(409,'Only failed jobs can be retried')
        return {'job_id':job_id,'status':'queued'}
    return app

app = create_app()
