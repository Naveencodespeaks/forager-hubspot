"""Single-account settings; secrets are never included in API responses."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    hubspot_token: str = os.getenv('HUBSPOT_ACCESS_TOKEN', '')
    hubspot_secret: str = os.getenv('HUBSPOT_CLIENT_SECRET', '')
    portal_id: str = os.getenv('HUBSPOT_PORTAL_ID', '')
    forager_key: str = os.getenv('FORAGER_API_KEY', '')
    account_id: str = os.getenv('FORAGER_ACCOUNT_ID', '')
    public_url: str = os.getenv('PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')
    admin_key: str = os.getenv('ADMIN_API_KEY', '')
    db_path: str = os.getenv('DATABASE_PATH', './data/jobs.sqlite3')
    title_query: str = os.getenv('DEFAULT_JOB_TITLES', 'engineer')
    limit: int = int(os.getenv('MAX_CONTACTS', '5'))
    budget: int = int(os.getenv('FORAGER_CREDIT_BUDGET', '140'))
    max_pages: int = int(os.getenv('MAX_SEARCH_PAGES', '3'))
    signature_version: str = os.getenv('HUBSPOT_SIGNATURE_VERSION', 'v1')

    def validate(self):
        for name in ('hubspot_token', 'hubspot_secret', 'portal_id', 'forager_key', 'account_id', 'admin_key'):
            if not getattr(self, name):
                raise ValueError(f'Missing setting: {name}; see .env.example')
        if not self.account_id.isdigit() or not self.portal_id.isdigit():
            raise ValueError('Account and portal IDs must be numeric')
        if len(self.admin_key) < 24:
            raise ValueError('ADMIN_API_KEY must contain at least 24 characters')
        if self.signature_version not in ('v1', 'v3'):
            raise ValueError('Signature version must be v1 or v3')
        if not 1 <= self.limit <= 25 or not 1 <= self.max_pages <= 10:
            raise ValueError('Invalid contact/page limit')
