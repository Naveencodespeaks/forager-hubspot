import asyncio
import json
import logging
from urllib.parse import urlsplit
from .clients import UpstreamError
from .mapping import domain, company_properties, person_properties, stamp

log = logging.getLogger(__name__)

class Service:
    def __init__(self, settings, store, forager, hubspot):
        self.s, self.store, self.f, self.h = settings, store, forager, hubspot

    async def save_person(self, person, role, job, contact_id=None, company_id=None):
        pid = person['id']
        # Durable ID checkpoint protects against CRM search indexing delays.
        key = 'contact-id:'+str(pid)
        contact_id = contact_id or self.store.read(key)
        if not contact_id:
            match = await self.h.find('contacts','fg_forager_id',pid)
            if not match:
                url = (person.get('linkedin_info') or {}).get('public_profile_url')
                match = await self.h.find('contacts','fg_linkedin_url',url) if url else None
            contact_id = match['id'] if match else None
        existing = (await self.h.get('contacts',contact_id))['properties'] if contact_id else {}
        props = person_properties(person,role)
        if not existing.get('email'):
            email = await self.f.email(person,props.get('fg_company_domain',''),job)
            if email:
                props['email'] = email
                if not contact_id:
                    match = await self.h.find('contacts','email',email)
                    if match:
                        contact_id = match['id']
                        existing = (await self.h.get('contacts',contact_id))['properties']
        if not existing.get('phone'):
            phone = await self.f.phone(pid,job)
            if phone:
                props['phone'] = phone
        props = stamp(props,existing)
        if contact_id:
            await self.h.update('contacts',contact_id,props)
        else:
            record = await self.h.create('contacts',props,company_id)
            contact_id = record['id']
        self.store.write(key,str(contact_id))
        if company_id:
            await self.h.associate(contact_id,company_id)
        # Record extra field names for discussion, not sensitive full raw profiles.
        extras = [k for k in ('description','headline','skills','educations','certifications','languages') if person.get(k)]
        return {'contact_id':str(contact_id),'status':props['fg_status'],
                'missing_fields':props['fg_missing_fields'],'extra_fields_available':extras}

    async def contact(self, contact_id, job, company_id=None, employer_domain=None):
        record = await self.h.get('contacts',contact_id)
        p = record['properties']
        if p.get('fg_enriched_at'):
            # The app's own contact creations also trigger HubSpot webhooks.
            return {'contact_id':str(contact_id),'status':'already_enriched'}
        if p.get('fg_forager_id'):
            person = await self.f.person({'person_id':int(p['fg_forager_id'])},job)
        elif p.get('fg_linkedin_url'):
            parsed = urlsplit(p['fg_linkedin_url'])
            if parsed.hostname not in ('linkedin.com','www.linkedin.com') or not parsed.path.startswith('/in/'):
                raise ValueError('Invalid person LinkedIn URL')
            person = await self.f.person({'linkedin_public_identifier':parsed.path.strip('/').split('/')[1]},job)
        elif p.get('email'):
            person = await self.f.by_email(p['email'],job)
        else:
            raise ValueError('Contact needs email, fg_linkedin_url or fg_forager_id for reliable identification')
        roles = [r for r in person.get('roles',[]) if r.get('is_current') is True]
        if employer_domain:
            roles = [r for r in roles if domain((r.get('organization') or {}).get('domain')) == employer_domain]
        if len(roles) != 1:
            raise ValueError('Current employer missing or ambiguous; review contact before enrichment')
        role = roles[0]
        if not company_id:
            target = domain((role.get('organization') or {}).get('domain'))
            match = await self.h.find('companies','domain',target) if target else None
            company_id = match['id'] if match else None
        return await self.save_person(person,role,job,contact_id,company_id)

    async def company(self, company_id, job, options):
        record = await self.h.get('companies',company_id)
        target = domain(record['properties'].get('domain') or record['properties'].get('website'))
        if not target or '.' not in target:
            raise ValueError('Company must have a valid domain or website')
        organization = await self.f.company(target,job)
        props = stamp(company_properties(organization),record['properties'],company=True)
        await self.h.update('companies',company_id,props)
        existing = await self.h.associated('companies',company_id,'contacts')
        contacts = []
        if existing and not options.get('discover'):
            for cid in existing:
                contacts.append(await self.contact(cid,job,company_id,target))
        else:
            limit = options.get('limit',self.s.limit)
            roles = await self.f.discover(target,options.get('titles',self.s.title_query),limit,job)
            for role in roles:
                person = await self.f.person({'person_id':role['person']['id']},job)
                current = [r for r in person.get('roles',[]) if r.get('is_current') is True and
                           domain((r.get('organization') or {}).get('domain')) == target]
                if not current:
                    continue  # Search index can lag behind person detail data.
                contacts.append(await self.save_person(person,role,job,company_id=company_id))
        return {'company_id':str(company_id),'company_status':props['fg_status'],
                'company_missing_fields':props['fg_missing_fields'],'contacts':contacts,
                'contact_count':len(contacts),'requested_limit':options.get('limit',self.s.limit),
                'note':'Missing contact data remains blank. A completed job does not imply complete records.'}

    async def process(self, job):
        if job['kind'] == 'company':
            return await self.company(job['object_id'],job['id'],json.loads(job['options']))
        return await self.contact(job['object_id'],job['id'])

    async def worker(self):
        while True:
            job = self.store.claim()
            if not job:
                await asyncio.sleep(.5)
                continue
            try:
                self.store.finish(job['id'],await self.process(job))
            except UpstreamError as exc:
                self.store.fail(job,str(exc),retry=exc.retry)
            except ValueError as exc:
                self.store.fail(job,str(exc))
            except Exception as exc:
                # Do not expose upstream response bodies / personal data / secrets.
                log.error('Job %s failed with %s',job['id'],type(exc).__name__)
                self.store.fail(job,'Unexpected '+type(exc).__name__+'; inspect adapter/schema')
