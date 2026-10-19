import asyncio
import hashlib
import json
from urllib.parse import urlsplit
import httpx
from .mapping import domain

class UpstreamError(Exception):
    def __init__(self, service, status, retry=False):
        super().__init__(f'{service} HTTP {status}')
        self.retry = retry

async def request(client, method, path, **kwargs):
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.TransportError:
        raise UpstreamError('Network', 0, True) from None
    if response.status_code >= 400:
        raise UpstreamError(client.base_url.host, response.status_code,
                            response.status_code == 429 or response.status_code >= 500)
    return response.json() if response.content else {}

class Forager:
    def __init__(self, settings, store, http=None):
        self.s, self.store = settings, store
        self.http = http or httpx.AsyncClient(base_url='https://api-v2.forager.ai',
            headers={'X-API-KEY':settings.forager_key}, timeout=45, follow_redirects=False)

    def path(self, name, v2=False):
        return f'/api/{"v2/" if v2 else ""}{self.s.account_id}/datastorage/{name}/'

    async def post(self, name, payload, job, cost=1, v2=False):
        path = self.path(name, v2)
        key = job + ':forager:' + hashlib.sha256((path + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        cached = self.store.read(key)
        if cached is not None:
            return cached
        self.store.reserve(cost, self.s.budget)
        result = await request(self.http, 'POST', path, json=payload)
        self.store.write(key, result)
        return result

    async def company(self, target, job):
        import os

        target = domain(target)
        expected = os.getenv("FORAGER_COMPANY_LINKEDIN_SLUG", "").strip().lower()
        matches = {}

        for page in range(4):
            result = await self.post(
                "organization_search",
                {"domains": [target], "page": page},
                job,
            )
            rows = result["search_results"]

            for row in rows:
                if domain(row.get("domain")) != target:
                    continue
                linkedin = row.get("linkedin_info") or {}
                slug = (linkedin.get("public_identifier") or "").strip().lower()
                if expected and slug != expected:
                    continue
                identity = row.get("id")
                if identity is not None:
                    matches[str(identity)] = row

            total = result.get("total_search_results")
            if not rows or (
                isinstance(total, int)
                and (page + 1) * len(rows) >= total
            ):
                break

        if len(matches) != 1:
            raise ValueError(
                f"Expected one company matching domain={target!r} "
                f"and LinkedIn slug={expected!r}; found {len(matches)} "
                "within the first four pages. No company selected."
            )
        return next(iter(matches.values()))

    async def discover(self, target, titles, limit, job):
        people, seen = [], set()
        for page in range(self.s.max_pages):
            result = await self.post('person_role_search', {
                'organization_domains': [target],
                'role_is_current': True,
                'role_title': titles,
                'page': page
            }, job)
            rows = result['search_results']
            for row in rows:
                pid = (row.get('person') or {}).get('id')
                if not pid or pid in seen or domain((row.get('organization') or {}).get('domain')) != target:
                    continue
                if row.get('is_current') is False:
                    continue
                seen.add(pid)
                people.append(row)
                if len(people) == limit:
                    return people
            if not rows or len(seen) >= result.get('total_search_results', 10**9):
                break
        return people

    async def person(self, identity, job):
        return await self.post('person_detail_lookup', identity, job)

    async def by_email(self, email, job):
        return await self.post('person_detail_reverse_lookup/by_email', {'email': email}, job, 5)

    async def phone(self, pid, job):
        rows = await self.post('person_contacts_lookup/phone_numbers', {'person_id': int(pid)}, job, 15)
        return next((r['phone_number'] for r in rows if r.get('phone_number')), None)

    async def email(self, person, employer_domain, job):
        payload = {
            'first_name': person.get('first_name') or '',
            'last_name': person.get('last_name') or '',
            'domain': employer_domain
        }
        slug = (person.get('linkedin_info') or {}).get('public_identifier')
        if slug:
            payload = {'linkedin_public_identifier': slug}
        if not slug and not all(payload.values()):
            return None
        result = await self.post('person_contacts_lookup/work_emails', payload, job, 5, True)
        job_id = result['job_id']
        # Construct a known-origin poll path: never send API key to a returned URL.
        import uuid
        job_id = str(uuid.UUID(job_id))
        for _ in range(24):
            result = await request(self.http, 'GET', self.path('person_contacts_lookup/work_emails', True) + job_id + '/')
            if result['status'] == 'completed':
                for email in result.get('emails', []):
                    if isinstance(email, str) and '@' in email:
                        return email
                    if isinstance(email, (list, tuple)) and email and '@' in str(email[0]):
                        return email[0]
                return None
            if result['status'] == 'failed':
                raise ValueError('Forager email lookup failed: ' + str(result.get('error_code')))
            await asyncio.sleep(5)
        raise UpstreamError('Forager email still processing', 202, True)

CUSTOM_COMMON = ['fg_forager_id', 'fg_linkedin_url', 'fg_enriched_at', 'fg_missing_fields', 'fg_status']
CUSTOM_CONTACT = ['fg_company_domain', 'fg_company_linkedin_url']
CUSTOM_COMPANY = ['fg_employee_range', 'fg_founded_date', 'fg_industry']
CONTACT_PROPS = ['firstname', 'lastname', 'email', 'phone', 'jobtitle', 'company', 'city', 'state', 'country'] + CUSTOM_COMMON + CUSTOM_CONTACT
COMPANY_PROPS = ['name', 'domain', 'description', 'website', 'numberofemployees', 'annualrevenue', 'city', 'state', 'country'] + CUSTOM_COMMON + CUSTOM_COMPANY

class HubSpot:
    def __init__(self, settings, http=None):
        self.http = http or httpx.AsyncClient(base_url='https://api.hubapi.com',
            headers={'Authorization': 'Bearer ' + settings.hubspot_token}, timeout=30)

    async def get(self, kind, oid):
        props = CONTACT_PROPS if kind == 'contacts' else COMPANY_PROPS
        return await request(self.http, 'GET', f'/crm/v3/objects/{kind}/{oid}', params={'properties': ','.join(props)})

    async def update(self, kind, oid, props):
        return await request(self.http, 'PATCH', f'/crm/v3/objects/{kind}/{oid}', json={'properties': props})

    async def create(self, kind, props, company_id=None):
        body = {'properties': props}
        if company_id:
            body['associations'] = [{'to': {'id': str(company_id)}, 'types': [{'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 279}]}]
        return await request(self.http, 'POST', f'/crm/v3/objects/{kind}', json=body)

    async def find(self, kind, prop, value):
        result = await request(self.http, 'POST', f'/crm/v3/objects/{kind}/search', json={
            'filterGroups': [{'filters': [{'propertyName': prop, 'operator': 'EQ', 'value': str(value)}]}], 'limit': 2})
        if len(result['results']) > 1:
            raise ValueError('Ambiguous HubSpot match; resolve duplicate records')
        return result['results'][0] if result['results'] else None

    async def associated(self, kind, oid, target):
        out, after = [], None
        while True:
            params = {'limit': 100}
            if after:
                params['after'] = after
            result = await request(self.http, 'GET', f'/crm/v4/objects/{kind}/{oid}/associations/{target}', params=params)
            out.extend(str(r['toObjectId']) for r in result['results'])
            after = result.get('paging', {}).get('next', {}).get('after')
            if not after:
                return out

    async def associate(self, cid, company_id):
        await request(self.http, 'PUT', f'/crm/v4/objects/contacts/{cid}/associations/default/companies/{company_id}')

    async def setup(self):
        for kind, extra, group in [('contacts', CUSTOM_CONTACT, 'contactinformation'), ('companies', CUSTOM_COMPANY, 'companyinformation')]:
            current = await request(self.http, 'GET', f'/crm/v3/properties/{kind}')
            names = {p['name'] for p in current['results']}
            for name in CUSTOM_COMMON + extra:
                if name not in names:
                    await request(self.http, 'POST', f'/crm/v3/properties/{kind}', json={
                        'name': name,
                        'label': name.replace('fg_', 'Forager ').replace('_', ' ').title(),
                        'type': 'string',
                        'fieldType': 'text',
                        'groupName': group
                    })