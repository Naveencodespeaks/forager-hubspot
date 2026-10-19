from urllib.parse import urlsplit
from datetime import datetime, timezone

CONTACT_REQUIRED = ('firstname','lastname','fg_linkedin_url','jobtitle','company',
                    'fg_company_domain','fg_company_linkedin_url','email','phone','city','state','country')

def domain(value):
    value = (value or '').strip().lower()
    host = urlsplit(value if '://' in value else 'https://'+value).hostname or ''
    return host.removeprefix('www.')

def clean(values):
    return {k: str(v) for k,v in values.items() if v is not None and v != ''}

def linkedin(record):
    return (record.get('linkedin_info') or {}).get('public_profile_url')

def location(record):
    places = (record.get('location') or {}).get('osm_locations') or []
    out = {}
    for p in places:
        kind = p.get('place_type')
        key = 'city' if kind in ('city','town','village','municipality') else kind
        if key in ('city','state','country') and p.get('name'):
            out.setdefault(key,p['name'])
    return out

def company_properties(record):
    return clean(dict(name=record.get('name'), domain=domain(record.get('domain')),
        description=record.get('description'), website=record.get('website'),
        numberofemployees=record.get('employees_amount'),
        annualrevenue=(record.get('finance_info') or {}).get('revenue'),
        fg_employee_range=record.get('employees_range'), fg_linkedin_url=linkedin(record),
        fg_forager_id=record.get('id'), fg_founded_date=record.get('founded_date'),
        fg_industry=((record.get('linkedin_info') or {}).get('industry') or {}).get('name'),
        **location(record)))

def person_properties(person, role):
    employer = role.get('organization') or {}
    return clean(dict(firstname=person.get('first_name'),lastname=person.get('last_name'),
        fg_forager_id=person.get('id'), fg_linkedin_url=linkedin(person),
        jobtitle=role.get('role_title'), company=employer.get('name') or role.get('organization_name'),
        fg_company_domain=domain(employer.get('domain')), fg_company_linkedin_url=linkedin(employer),
        **location(person)))

def stamp(properties, existing=None, company=False):
    merged = {**(existing or {}), **properties}
    required = ('name','domain','description','numberofemployees','annualrevenue','fg_linkedin_url') if company else CONTACT_REQUIRED
    missing = [k for k in required if not merged.get(k)]
    return {**properties,'fg_enriched_at':datetime.now(timezone.utc).isoformat(),
            'fg_missing_fields': ', '.join(missing), 'fg_status':'partial' if missing else 'complete'}
