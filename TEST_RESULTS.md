# Local verification

13 pytest cases passed on 2026-10-19 using synthetic data and mocked HTTP responses.

Covered: v1/v3 signatures and timestamp rejection, admin authorization, portal
isolation, duplicate events, durable queue restart recovery, credit limits, missing
fields, company discovery, five-contact pipeline, existing contact enrichment,
self-webhook suppression, Forager polling and same-origin API use, retry
classification, and HubSpot association request shape.

Python source compilation and CLI help also passed.
One dependency deprecation warning was emitted by Starlette's test client.

No live vendor calls, deployment, real CRM records, or video were verified.
