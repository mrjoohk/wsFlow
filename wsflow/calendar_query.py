"""I0 date records from existing daily files. No live Jira/Google or source writes.

Dates are explicit business dates, never inferred from mtime or current issue status.
New local events/notes and Google coverage belong to later phases and remain unknown.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .readmodel import Reader, fingerprint
from .wfhome import MEMO_END, MEMO_START, parse_memo, wf_home_for

MAX_RANGE_DAYS = 62


def iso_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('날짜는 YYYY-MM-DD 형식이어야 합니다')
    return date.fromisoformat(value)


def validate_query(start: str, end: str, display_timezone: str) -> tuple[date, date]:
    """Validate end-exclusive range; YYMMDD files support business years 2000..2099."""
    first, last = iso_date(start), iso_date(end)
    if first < date(2000, 1, 1) or last > date(2100, 1, 1):
        raise ValueError('기존 YYMMDD 기록의 지원 업무 연도는 2000~2099입니다')
    if not 1 <= (last - first).days <= MAX_RANGE_DAYS:
        raise ValueError(f'기간은 종료일 제외 1~{MAX_RANGE_DAYS}일이어야 합니다')
    try:
        ZoneInfo(display_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('지원하지 않는 시간대 또는 IANA 시간대 데이터가 없습니다') from None
    return first, last


def _site(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        state = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(state, dict) or not isinstance(state.get('issue_type_map', {}), dict):
            raise ValueError('invalid_state')
        value = state.get('issue_type_map', {}).get('site_url')
        if isinstance(value, str):
            parts = urlsplit(value)
            if parts.scheme in ('https', 'http') and parts.hostname and not parts.username and not parts.password:
                return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), '', ''))
    except (ValueError, UnicodeError, AttributeError):
        raise ValueError('invalid_state') from None
    return None


def _memo(raw: bytes) -> str:
    text = raw.decode('utf-8-sig')
    if text.count(MEMO_START) != 1 or text.count(MEMO_END) != 1:
        raise ValueError('invalid_memo_markers')
    begin, end = text.find(MEMO_START) + len(MEMO_START), text.find(MEMO_END)
    if end < begin:
        raise ValueError('invalid_memo_markers')
    return text[begin:end]


def query(projects: list[dict], start: str, end: str,
          display_timezone: str = 'Asia/Seoul') -> dict:
    """Read existing records once per WF_HOME and aggregate unique site/key per day.

    Args: registered project dictionaries; ISO date bounds (end exclusive); IANA name.
    Returns: schema-v1 range with detail/counts from the same in-memory observations.
    Raises: ValueError for invalid dates, ranges or unavailable timezone data.
    """
    first, last = validate_query(start, end, display_timezone)
    homes, unresolved = {}, []
    for pr in projects:
        try:
            home = wf_home_for(pr).expanduser().resolve()
            homes.setdefault(home, []).append(pr['id'])
        except (OSError, RuntimeError):
            unresolved.append(pr['id'])
    reader = Reader(directories=list(homes))
    dates = [first + timedelta(days=i) for i in range((last - first).days)]
    by_date = {d.isoformat(): {'date': d.isoformat(), 'tasks': [], 'memos': [],
                              'events': [], 'event_count': None,
                              'event_status': 'not_implemented', 'complete': not unresolved,
                              'errors': ['unresolved_wf_home'] if unresolved else []} for d in dates}
    sources = []
    for home, ids in sorted(homes.items(), key=lambda pair: str(pair[0])):
        ids = sorted(set(ids))
        source_id = fingerprint(str(home))
        source = {'source_id': source_id, 'wf_home': str(home), 'project_ids': ids,
                  'kind': 'legacy_daily', 'last_sync': None}
        sources.append(source)
        state_meta, state_raw = reader.read(home / '.workflow/state.json', source_id + ':state', 'wf_home', home)
        try:
            site = _site(state_raw)
        except ValueError:
            site = None
            state_meta.update(status='error', error='invalid_state')
        try:
            home_exists = home.is_dir()
        except OSError:
            home_exists = False
        for day in dates:
            item = by_date[day.isoformat()]
            path = home / 'daily' / f'{day:%y%m%d}.md'
            meta, raw = reader.read(path, source_id + ':' + day.isoformat(), 'wf_home', home)
            if not home_exists or meta['status'] in ('blocked', 'error') or state_meta['status'] in ('blocked', 'error'):
                item['complete'] = False
                item['errors'].append(source_id + ':source_unavailable')
                continue
            if raw is None:
                continue
            try:
                parsed = parse_memo(_memo(raw))
            except (ValueError, UnicodeError):
                meta.update(status='error', error='invalid_memo')
                item['complete'] = False
                item['errors'].append(source_id + ':invalid_memo')
                continue
            for key, values in parsed['by_key'].items():
                records = [v for v in values if v.strip()]
                if not records:
                    continue
                if site is None:
                    item['complete'] = False
                    item['errors'].append(source_id + ':task_site_unknown')
                task_id = fingerprint([site or str(home), key])
                task = next((t for t in item['tasks'] if t['task_id'] == task_id), None)
                if task is None:
                    task = {'task_id': task_id, 'key': key, 'site_url': site, 'summary': None,
                            'identity_scope': 'site' if site else 'wf_home',
                            'project_ids': [], 'records': []}
                    item['tasks'].append(task)
                task['project_ids'] = sorted(set(task['project_ids'] + ids))
                task['records'].extend({'text': text, 'source_id': source_id,
                                        'path': str(path)} for text in records)
            common = [text for text in parsed['common'] if text.strip()]
            marks = [m for m in parsed['marks'] if m['text'].strip()]
            if common or marks:
                item['memos'].append({'note_id': fingerprint([source_id, day.isoformat(), 'legacy_common']),
                                      'kind': 'legacy_common_block', 'project_ids': ids,
                                      'source_id': source_id, 'path': str(path),
                                      'common': common, 'marks': marks})
    reader.finish()
    # Any source that changed during this query invalidates its affected observations.
    for meta in reader.sources:
        if meta.get('error') == 'source_changed_during_query':
            for item in by_date.values():
                item['complete'] = False
                item['errors'].append('source_changed_during_query')
    days = list(by_date.values())
    for item in days:
        item['tasks'].sort(key=lambda t: (t['site_url'] or '', t['key']))
        item['task_count'] = len(item['tasks']) if item['complete'] else None
        item['memo_count'] = len(item['memos']) if item['complete'] else None
        item['errors'] = sorted(set(item['errors']))
    query_scope = {'start': start, 'end': end, 'end_exclusive': True,
                   'timezone': display_timezone, 'project_ids': sorted(pr['id'] for pr in projects),
                   'source_scope': 'legacy_daily_only'}
    return {'schema_version': 1, 'snapshot_id': fingerprint([query_scope, reader.sources, days]),
            'captured_at': datetime.now(timezone.utc).isoformat(), 'query': query_scope,
            'complete': all(d['complete'] for d in days), 'days': days,
            'sources': sources, 'observations': reader.sources,
            'coverage': {'saved_legacy_work_and_common_memos': True, 'local_events': False,
                         'independent_notes': False, 'google_calendar': False}}
