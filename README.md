# Forager × HubSpot enrichment automation

Python/FastAPI implementation for the One GTM Lab assessment. Receives HubSpot
creation subscription events, persists jobs, calls Forager, and updates HubSpot.

**Delivery status:** implemented and tested locally with mocked APIs. Not yet
live-tested, deployed, connected to a Git remote, or populated with real OpenAI
contacts. No real credentials or fabricated demo people are included. The actual
video must be recorded after the live acceptance checklist passes.

## Start locally (Ubuntu)

```bash
cd forager-hubspot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
# Edit .env with your own account values; never commit it.
python -m pytest -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Use Python 3.12. Open http://localhost:8000/docs for the interactive API.
Click **Authorize** and enter ADMIN_API_KEY for admin endpoints. Secrets are not
needed to run tests. Normal application startup fails until required settings
are supplied. Generate an admin key with:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## Account setup

### Forager

Create an account at https://app.forager.ai. In its API section obtain an API key
and your numeric account ID. Set FORAGER_API_KEY and FORAGER_ACCOUNT_ID.
Verify the actual account credit balance and endpoint access. The assessment
mentions 150 free credits; the application does not assume that balance exists.

### HubSpot

Use a dedicated test account. Create/configure an app for that account with:

- crm.objects.contacts.read and crm.objects.contacts.write
- crm.objects.companies.read and crm.objects.companies.write
- crm.schemas.contacts.read and crm.schemas.contacts.write
- crm.schemas.companies.read and crm.schemas.companies.write

Use its private access token as HUBSPOT_ACCESS_TOKEN. Set HUBSPOT_PORTAL_ID to the
numeric CRM account ID. HUBSPOT_CLIENT_SECRET must be the **signing client secret
of the app sending the webhooks**, not the private access token.

This service targets CRM subscription events, not a custom workflow action.
Configure subscriptions for **company.creation** and **contact.creation** with:

```
https://YOUR-SERVICE-DOMAIN/webhooks/hubspot
```

HubSpot app setup differs between legacy/private apps and the developer platform.
Use the account's supported app subscription configuration. If subscriptions
aren't available for your app, configure an appropriate app first; manual API
calls are useful for debugging but do not satisfy the webhook requirement.

Set HUBSPOT_SIGNATURE_VERSION=v1 for legacy CRM subscription signatures.
For an app sending v3 signatures, set v3; the application checks HMAC and the
five-minute timestamp window. Versions are explicitly pinned to avoid fallback
or signature downgrade. PUBLIC_BASE_URL must exactly match the public origin
used for subscriptions, particularly when running behind Railway's proxy.

### Create CRM properties before activating subscriptions

Start the application, then in another terminal (same virtual environment):

```bash
python -m app.cli setup
```

This creates missing `fg_*` text properties and leaves existing properties alone.
If names already exist with incompatible types, fix the test portal schema first.
Add these fields to HubSpot record views so they are visible during the demo.

## Railway deployment

1. Put this source in your own Git repository. Commit `.env.example`, never `.env`.
2. Create a Railway project from that repository. The Dockerfile starts Uvicorn.
3. Add a **persistent volume mounted at /data**; set DATABASE_PATH=/data/jobs.sqlite3.
4. Set the variables from `.env.example` in Railway Variables.
5. Generate the public service domain and set PUBLIC_BASE_URL to its HTTPS origin.
6. Deploy with **one replica and one Uvicorn worker**. Do not enable horizontal
   scaling or overlapping deployments. This design has a single queue consumer.
7. Check `/health`, run setup using the public URL, then enable subscriptions.
8. Keep the volume across deployments. It stores the job queue, retry checkpoints,
   contact identity mapping, and conservative credit reservations.

Health checks confirm the process is up, not that vendor credentials are valid.
Run the live checklist below to verify integrations. Hosting may have account
limits or charges; no hosting account or paid resource was created for you.

## OpenAI live demonstration

1. Set DEFAULT_JOB_TITLES to a suitable Boolean title query, such as
   `engineer OR researcher`. Set MAX_CONTACTS=5. Restart after setting variables.
2. Create a company in HubSpot named `OpenAI` with domain `openai.com`.
   Use the isolated test portal to distinguish it from production data.
3. The real company.creation webhook should create a job automatically.
4. Inspect jobs:

```bash
python -m app.cli jobs
python -m app.cli job YOUR_JOB_ID
```

5. Verify company description, headcount, revenue (if returned), LinkedIn URL,
   and related contacts in HubSpot. Forager results determine the actual values.
6. If a company already has contacts, normal processing enriches those contacts.
   To explicitly discover contacts with a different job-title filter:

```bash
python -m app.cli company YOUR_NUMERIC_COMPANY_ID --titles 'engineer OR researcher' --limit 5 --discover
```

7. Verify each of the five people works at the target company. Inspect all required
   fields. `fg_status=partial` and `fg_missing_fields` make coverage gaps visible.
8. For an independent contact webhook demo, create a *new, real* contact with
   a known email or `fg_linkedin_url`, and verify its contact.creation job.
9. For transient failures, automatic retries use the same checkpoints. After
   correcting a configuration failure, retry its existing job:

```bash
python -m app.cli retry YOUR_JOB_ID
```

Do not repeatedly create new jobs to retry the same operation: checkpoints are
per job. Already-enriched contacts are skipped by the contact path to prevent
self-triggered enrichment loops. A deliberate refresh requires reviewing/removing
`fg_enriched_at` on that contact first, then enqueueing a contact job.

## Field mappings

| Forager field | HubSpot company property |
| --- | --- |
| name / domain / website / description | name / domain / website / description |
| employees_amount | numberofemployees |
| finance_info.revenue | annualrevenue |
| employees_range | fg_employee_range |
| founded_date | fg_founded_date |
| linkedin_info.public_profile_url | fg_linkedin_url |
| linkedin_info.industry.name | fg_industry |
| id | fg_forager_id |
| location.osm_locations | city, state, country when explicitly supplied |

| Forager field | HubSpot contact property |
| --- | --- |
| first_name / last_name | firstname / lastname |
| linkedin_info.public_profile_url | fg_linkedin_url |
| current matching role.role_title | jobtitle |
| role.organization.name | company |
| role.organization.domain | fg_company_domain |
| role.organization.linkedin_info.public_profile_url | fg_company_linkedin_url |
| asynchronous work email lookup | email |
| phone lookup | phone |
| location.osm_locations | city / state / country |

A contact is associated to its HubSpot company record. Employer location is
never used as a substitute for a person's location. Null provider values never
wipe existing values. Existing email and phone values are preserved. Revenue is
mapped only from the provider's numeric revenue field; ranges are not invented.

Description, headline, skills, education, certifications, and language availability
are listed by field name in each job result for reviewer discussion. These extras
are not automatically written as additional CRM personal-data fields.

## Engineering behavior

- Signed raw-body webhook validation; single-portal allowlist; admin bearer auth.
- Durable webhook enqueue before HTTP acknowledgment; batch validation before writes.
- Duplicate event keys ignore delivery attempt numbers.
- Serial worker, restart recovery, bounded retries for network/429/5xx failures.
- Retry checkpoints preserve successful Forager responses and email job IDs.
- Company matches require exact normalized domain; people search is scoped to the
  company domain and `role_is_current=true`, with the requested server-side title query.
- Person detail must confirm current employment at the target company.
- Existing company-contact associations are paginated.
- Contact match order: durable Forager-ID map, CRM Forager ID, LinkedIn URL, email.
- Unknown/ambiguous contacts fail for manual review rather than guessing identity.
- Email polling uses a constructed vendor-origin URL so keys cannot leak to an
  arbitrary `status_check_url`. No automatic redirect following for Forager.
- No email sending, outbound messaging, LLM calls, or scraping of LinkedIn.

## Credits and limitations

The local cumulative budget defaults to 140 estimated credits. Reservations are
conservative: company/person/role requests reserve 1, email lookups 5, and phone
lookups 15; reverse-email lookups reserve 5. Polling reserves zero. This is a
request guard, **not a billing meter**: actual vendor charges, failed requests,
and credits used outside this service may differ. Review the Forager account.
Do not reset the database to bypass the guard. Increase the configured budget
only after checking available credits. Five fresh people typically reserve about
105 credits plus company/search operations, before extra tests/retries.

- Provider coverage is not guaranteed. The assessment's requirement for five
  fully populated people may need reviewer clarification if data is unavailable.
- Search is capped at MAX_SEARCH_PAGES, returns up to the requested number, and
  may return fewer when coverage or current-employer checks fail.
- Per-job responses are stored as retry checkpoints in SQLite. Protect the volume,
  do not commit it, and delete it when the evaluation is complete and retention
  is no longer needed. No indefinite reusable global enrichment cache is used.
- Single worker/replica only. A larger deployment needs a distributed queue,
  leases, tenant isolation, scalable storage, and a stronger rate-limit strategy.
- A process crash after a remote write but before its local checkpoint can cause
  a repeated remote request; this is not distributed exactly-once processing.
  CRM matching mitigates duplicates, but indexing delay/crash windows remain.
- Failed enrichment stops that job; already written records remain. Correct the
  cause and retry the same job. No destructive rollback is attempted.
- v1 signatures lack timestamp replay protection; durable event deduplication
  prevents repeat processing while the database is retained. Prefer v3 when supported.
- The included API contracts were checked against official documentation; real
  authentication, response variants and deployment must still be tested live.

## Live acceptance checklist (not yet executed)

- [ ] Credentials and scopes validated; custom properties created.
- [ ] Public HTTPS service + persistent volume ready.
- [ ] Real HubSpot company.creation webhook accepted and completed.
- [ ] Existing contacts enriched without duplicate creation.
- [ ] Title-filtered discovery produces five associated, current OpenAI contacts.
- [ ] Real contact.creation webhook enriches a new contact.
- [ ] Required fields reviewed; missing fields documented accurately.
- [ ] Replayed delivery does not repeat API work.
- [ ] Service restart preserves pending jobs and credit reservations.
- [ ] Git repository access and live URL shared with reviewer.
- [ ] Video recorded and CV attached to submission.

## Suggested 4–6 minute video

1. Explain the architecture and show a sanitized environment variable list.
2. Create the OpenAI test company and show the live webhook job arriving.
3. Show enriched company data and the five associated people in HubSpot.
4. Show title filtering with the admin endpoint and the resulting job titles.
5. Create a new contact; show its separate webhook and enrichment.
6. Explain missing-field reporting, duplicate protection, retries, and extras.
7. Show repository README and live service health URL. Hide API keys and tokens.

## Official references

- https://docs.forager.ai/api-overview/authentication
- https://api-v2.forager.ai/static/schema.html
- https://docs.forager.ai/openapi/people/datastorage_person_role_search_create
- https://docs.forager.ai/openapi/work-emails
- https://docs.forager.ai/openapi/work-emails/v2_datastorage_person_contacts_lookup_work_emails_retrieve
- https://docs.forager.ai/api-overview/credit-pricing
- https://developers.hubspot.com/docs/apps/legacy-apps/authentication/validating-requests
- https://developers.hubspot.com/docs/api-reference/crm-contacts-v3/guide
- https://developers.hubspot.com/docs/api-reference/crm-associations-v4/guide
- https://docs.railway.com/volumes
# forager-hubspot
