"""I2 snapshot composition. Local records plus I0's unchanged legacy reader."""
from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import calendar_query
from wsflow import operations, calendar_identity
from wsflow.readmodel import fingerprint


def overlaps(event, day, zone):
    """Date/start inclusive, end exclusive; compare instants through UTC for DST."""
    if event['all_day']:
        return event['start_date'] <= day.isoformat() < event['end_date']
    start = datetime.fromisoformat(event['start_at']).astimezone(timezone.utc)
    end = datetime.fromisoformat(event['end_at']).astimezone(timezone.utc)
    left = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    right = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    return start < right and end > left


def query(projects, store, start, end, display_timezone='Asia/Seoul', project_ids=None, source_ids=None, google_result=None, google_links=None):
    """Return schema-v2 counts/details from identical selected observations.

    Read failures yield null counts for the affected domain, never a known zero.
    Google observations are supplied by the adapter; this composition has no network.
    """
    first, last = calendar_query.validate_query(start, end, display_timezone)
    selected = None if project_ids is None else set(project_ids)
    sources = {'local','legacy_daily','google'} if source_ids is None else set(source_ids)
    if not sources or sources - {'local','legacy_daily','google'}:
        raise ValueError('지원하지 않는 캘린더 출처입니다')
    registered = {p['id'] for p in projects}
    if selected is not None and (not selected or selected - registered):
        raise ValueError('등록된 프로젝트를 선택하십시오')
    records, local_ok = [], True
    errors = []
    links = {l['local_id']:l for l in (google_links or [])}
    needs_local = 'local' in sources or ('google' in sources and bool(links))
    current_projects = calendar_identity.observe(projects, store.registry) if needs_local else {}
    if needs_local:
        try:
            records = store.records()
        except (OSError, sqlite3.Error, ValueError):
            local_ok = False; errors.append('local_store_unavailable')
    legacy = calendar_query.query([p for p in projects if selected is None or p['id'] in selected]
                                  if 'legacy_daily' in sources else [], start, end, display_timezone)
    if needs_local and local_ok:
        try:
            if records != store.records():
                local_ok = False; errors.append('local_changed_during_query')
        except (OSError, sqlite3.Error, ValueError):
            local_ok = False; errors.append('local_store_unavailable')
    zone = ZoneInfo(display_timezone)
    if needs_local and current_projects != calendar_identity.observe(projects, store.registry):
        local_ok = False; errors.append('project_identity_changed_during_query')
    days = copy.deepcopy(legacy['days'])
    google_result = google_result or {'events':[], 'sources':[], 'complete':True, 'connected':False}
    google_selected = 'google' in sources and selected is None
    google_ok = not google_selected or google_result['complete']
    google_sources = {s['source_id'] for s in google_result['sources']}
    mapped_remote = {'G-'+operations.digest([l['account_id'],l['calendar_id'],l['remote_id']]) for l in links.values()}
    if not google_ok:
        errors.append('google_unavailable')
    for day in days:
        date = calendar_query.iso_date(day['date'])
        day['events'] = []
        day['project_link_warnings'] = []
        try:
            for record in records:
                statuses = {i:calendar_identity.link_status(record.get('project_references', {}).get(i), current_projects.get(i)) for i in record['project_ids']}
                active = {i for i, status in statuses.items() if status in {'verified','legacy_path'}}
                on_date = overlaps(record,date,zone) if record['kind']=='event' else record['date']==day['date']
                if not record['deleted'] and on_date and set(record['project_ids'])-active:
                    day['project_link_warnings'].append({'record_id':record['id'],'project_ids':sorted(set(record['project_ids'])-active),
                                                         'status':'unverified_or_unavailable'})
                if record['deleted'] or (selected is not None and not selected.intersection(active)):
                    continue
                visible = {**record, 'unavailable_project_ids':sorted(set(record['project_ids'])-active),'project_link_status':statuses}
                link = links.get(record['id'])
                linked_google = bool(link and 'google:'+operations.digest([link['account_id'],link['calendar_id']]) in google_sources)
                if link:
                    visible.update(google_link={k:link[k] for k in ('calendar_id','state','hidden','local_id')},sync_status=link['state'])
                    visible['google_link']['remote_deleted']=bool(link.get('remote_deleted'))
                if record['kind'] == 'event' and ('local' in sources or ('google' in sources and linked_google)) and overlaps(record, date, zone):
                    day['events'].append(visible)
                elif record['kind'] == 'note' and 'local' in sources and record['date'] == day['date']:
                    day['memos'].append({**visible,'note_id':record['id']})
            if google_selected:
                day['events'].extend(e for e in google_result['events'] if e['id'] not in mapped_remote and overlaps(e,date,zone))
            day['events'].sort(key=lambda r: (not r['all_day'], r.get('start_at',''), r['title'],r['id']))
        except (ValueError, KeyError, TypeError, OverflowError):
            local_ok = False
            errors.append('invalid_local_record')
    # Evaluate after all records, so one malformed record cannot leave earlier days complete.
    for day in days:
        legacy_ok = day['complete']
        day['event_count'] = len(day['events']) if local_ok and google_ok else None
        day['event_status'] = ('google_and_local' if google_selected and google_result['connected'] else 'local_only') if local_ok and google_ok else 'unavailable'
        day['memo_count'] = len(day['memos']) if legacy_ok and local_ok else None
        day['complete'] = legacy_ok and local_ok and google_ok
        day['errors'] = sorted(set(day['errors'] + errors))
    scope = {**legacy['query'], 'source_scope':'selected_calendar_sources', 'source_ids':sorted(sources),
             'project_ids': sorted(selected) if selected is not None else None}
    return {'schema_version':2, 'query':scope, 'snapshot_id':fingerprint([scope,legacy['snapshot_id'],records,local_ok,days]),
            'captured_at':datetime.now(timezone.utc).isoformat(), 'days':days,
            'sources':legacy['sources'] + ([{'source_id':'local','kind':'local','last_sync':None,'complete':local_ok}] if 'local' in sources else []) + (google_result['sources'] if google_selected else []),
            'observations':legacy['observations'], 'complete':all(d['complete'] for d in days),
            'coverage':{'saved_legacy_work_and_common_memos':'legacy_daily' in sources,'local_events':'local' in sources,
                        'independent_notes':'local' in sources,'google_calendar':google_selected and google_result['connected']}}
