"""Durable immutable plans and conditional operation claims, outside project sources.

SQLite owns operation facts. Legacy approval logs are an export with explicit repair,
not part of the same transaction. No credentials are stored here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class StateConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


class Store:
    def __init__(self, home):
        self.home = str(Path(home).resolve())
        configured = os.environ.get('WSFLOW_DATA_DIR')
        base = Path(configured) if configured else (Path(os.environ.get('APPDATA') or Path.home() / '.config') / 'wsFlow')
        base = base.expanduser().resolve()
        if base == Path(self.home) or Path(self.home) in base.parents:
            raise StateConflict('작업 DB는 WF_HOME 밖의 사용자 데이터 영역을 사용해야 합니다')
        self.path = base / 'operations' / (digest(self.home) + '.sqlite3')

    @contextmanager
    def connection(self, write=False):
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = self.path.as_uri() + ('?mode=rwc' if write else '?mode=ro')
        db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1) or (version == 0 and db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]):
                raise StateConflict('지원하지 않는 작업 DB schema입니다 — 원본을 보존하고 복구해야 합니다')
            if write:
                db.execute('PRAGMA synchronous=FULL')
                db.executescript('''
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, intent TEXT NOT NULL,
 status TEXT NOT NULL, approved_at TEXT, result TEXT, error TEXT, local_recorded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY, operation_id TEXT,
 status TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS plan_immutable BEFORE UPDATE ON plans BEGIN SELECT RAISE(ABORT,'immutable plan'); END;
CREATE TRIGGER IF NOT EXISTS plan_no_delete BEFORE DELETE ON plans BEGIN SELECT RAISE(ABORT,'immutable plan'); END;
CREATE TRIGGER IF NOT EXISTS intent_immutable BEFORE UPDATE OF intent ON operations BEGIN SELECT RAISE(ABORT,'immutable intent'); END;
CREATE TRIGGER IF NOT EXISTS operation_no_delete BEFORE DELETE ON operations BEGIN SELECT RAISE(ABORT,'immutable operation'); END;
CREATE TRIGGER IF NOT EXISTS events_immutable BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append only audit'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append only audit'); END;
PRAGMA user_version=1;
''')
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db, ident, status, detail=None):
        db.execute('INSERT INTO events(operation_id,status,detail,at) VALUES(?,?,?,?)',
                   (ident, status, encoded(detail or {}), now()))

    def create(self, intent, ident=None):
        ident = ident or 'A-' + uuid4().hex
        value = {**intent, 'home': self.home, 'operation_id': ident}
        with self.connection(True) as db:
            db.execute('INSERT INTO operations(id,intent,status) VALUES(?,?,?)', (ident, encoded(value), 'planned'))
            self._event(db, ident, 'planned')
        return self.get(ident)

    def save_plan(self, snapshot, items, ttl=1800):
        plan_id = 'P-' + uuid4().hex
        value = {**snapshot, 'plan_id': plan_id, 'home': self.home,
                 'created_at': now(), 'expires_at': time.time() + ttl, 'items': []}
        with self.connection(True) as db:
            for item in items:
                ident = 'A-' + uuid4().hex
                intent = {**item, 'site': snapshot.get('site'), 'operation_id': ident,
                          'home': self.home, 'plan_id': plan_id}
                db.execute('INSERT INTO operations(id,intent,status) VALUES(?,?,?)', (ident, encoded(intent), 'planned'))
                self._event(db, ident, 'planned', {'plan_id': plan_id})
                value['items'].append(intent)
            db.execute('INSERT INTO plans(id,snapshot) VALUES(?,?)', (plan_id, encoded(value)))
        return value

    def plan(self, ident):
        with self.connection() as db:
            row = db.execute('SELECT snapshot FROM plans WHERE id=?', (ident,)).fetchone() if db else None
        if row is None:
            raise StateConflict('발급한 계획을 찾지 못했습니다 — 다시 미리보기하십시오')
        return json.loads(row[0])

    @staticmethod
    def _decode(row):
        return {**json.loads(row['intent']), 'status': row['status'], 'approved_at': row['approved_at'],
                'result': json.loads(row['result']) if row['result'] else None,
                'error': row['error'], 'local_recorded': bool(row['local_recorded'])}

    def get(self, ident):
        with self.connection() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (ident,)).fetchone() if db else None
        return self._decode(row) if row else None

    def list(self):
        with self.connection() as db:
            rows = db.execute('SELECT * FROM operations ORDER BY rowid').fetchall() if db else []
        return [self._decode(row) for row in rows]

    def approve(self, ident):
        stamp = now()
        with self.connection(True) as db:
            row = db.execute('SELECT status FROM operations WHERE id=?', (ident,)).fetchone()
            if row is None or row[0] not in ('planned', 'approved', 'failed'):
                raise StateConflict('이미 실행됐거나 결과 확인이 필요한 작업입니다')
            if row[0] != 'approved':
                db.execute('UPDATE operations SET status=?,approved_at=COALESCE(approved_at,?),error=NULL WHERE id=?', ('approved', stamp, ident))
                self._event(db, ident, 'approved')
        return self.get(ident)

    def claim(self, ident):
        with self.connection(True) as db:
            changed = db.execute("UPDATE operations SET status='executing' WHERE id=? AND status='approved'", (ident,)).rowcount
            if changed != 1:
                raise StateConflict('중복 실행 또는 결과 확인이 필요한 작업입니다')
            self._event(db, ident, 'executing')

    def reject_unexecuted(self, ident, error):
        """Cancel only a never-claimed intent; a delayed apply cannot revive it."""
        with self.connection(True) as db:
            changed = db.execute("UPDATE operations SET status='failed',error=? WHERE id=? AND status IN ('planned','approved')",
                                 (error, ident)).rowcount
            if changed != 1:
                raise StateConflict('실행되지 않은 작업만 종료할 수 있습니다')
            self._event(db, ident, 'failed', {'error': error, 'effect_started': False})

    def finish(self, ident, status, result=None, error=None):
        if status not in ('succeeded', 'failed', 'uncertain'):
            raise ValueError('invalid operation state')
        with self.connection(True) as db:
            changed = db.execute("UPDATE operations SET status=?,result=?,error=? WHERE id=? AND status IN ('executing','uncertain')",
                                 (status, encoded(result) if result is not None else None, error, ident)).rowcount
            if changed != 1:
                raise StateConflict('이 상태의 작업 결과를 덮을 수 없습니다')
            self._event(db, ident, status, {'error': error})

    def local_recorded(self, ident):
        with self.connection(True) as db:
            db.execute("UPDATE operations SET local_recorded=1 WHERE id=? AND status='succeeded'", (ident,))
            self._event(db, ident, 'local_recorded')
