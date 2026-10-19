"""Durable queue and per-job checkpoints. Run ONE process / ONE replica."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, dedupe TEXT UNIQUE, kind TEXT, object_id TEXT,
              options TEXT, status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0,
              available REAL DEFAULT 0, result TEXT, error TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS checkpoints (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS spend (id INTEGER PRIMARY KEY, amount INTEGER);
            INSERT OR IGNORE INTO spend VALUES (1,0);
            ''')
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db
    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='queued' WHERE status='running'")
    def enqueue(self, kind, object_id, options=None, dedupe=None):
        job_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO jobs(id,dedupe,kind,object_id,options,created) VALUES(?,?,?,?,?,?)',
                       (job_id, dedupe or job_id, kind, str(object_id), json.dumps(options or {}), time.time()))
            return db.execute('SELECT id FROM jobs WHERE dedupe=?', (dedupe or job_id,)).fetchone()['id']
    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND available<=? ORDER BY created LIMIT 1", (time.time(),)).fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running', attempts=attempts+1 WHERE id=?", (row['id'],))
                return dict(row)
    def finish(self, job_id, result):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='completed', result=?, error=NULL WHERE id=?", (json.dumps(result), job_id))
    def fail(self, job, error, retry=False):
        attempts = job['attempts'] + 1
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=?,error=?,available=? WHERE id=?',
                       ('queued' if retry and attempts < 4 else 'failed', error, time.time()+min(60, 2**attempts), job['id']))
    def get(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ('options','result'):
            result[key] = json.loads(result[key]) if result[key] else None
        return result
    def list_jobs(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,kind,object_id,status,attempts,error,created FROM jobs ORDER BY created DESC LIMIT 100')]
    def read(self, key):
        with self.connect() as db:
            row = db.execute('SELECT value FROM checkpoints WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else None
    def write(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?)', (key, json.dumps(value)))
    def reserve(self, cost, budget):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            spent = db.execute('SELECT amount FROM spend WHERE id=1').fetchone()[0]
            if spent + cost > budget:
                raise ValueError('Local credit budget reached; review usage before increasing FORAGER_CREDIT_BUDGET')
            db.execute('UPDATE spend SET amount=amount+? WHERE id=1', (cost,))
    def spent(self):
        with self.connect() as db:
            return db.execute('SELECT amount FROM spend WHERE id=1').fetchone()[0]
