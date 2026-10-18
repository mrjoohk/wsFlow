"""Explicit Jira daily pipeline. The final state replacement alone commits its watermark.

Vendor programs run against a private staging tree. No Jira writes, Google cursor
access, shell commands or user narrative execution occur in this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import calendar_query, fileio, jiraapi, operations, wfhome

STAGES = ('prepare', 'collect', 'aggregate', 'render', 'checks', 'publish', 'commit')
VENDOR = Path(__file__).resolve().parent.parent / 'vendor/jira_worklog'
MANIFEST = Path(__file__).resolve().parent.parent / 'tools/jira_vendor_manifest.json'


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _write(path, value):
    data = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    for attempt in range(5):
        try:
            fileio.atomic_write(path, data)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.02 * (attempt + 1))


def vendor_status():
    manifest = _json(MANIFEST)
    actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in VENDOR.glob('*.py')}
    return {'identical': actual == manifest['files'], 'files': actual,
            'tree_hash': operations.digest(actual), 'upstream_commit': manifest['upstream_commit']}


def config(state):
    return {k: state.get(k) for k in ('schema_version', 'project_key', 'csc_field', 'csc_value',
                                     'epic_keys', 'title_include', 'title_exclude', 'title_prefix',
                                     'sprint_mode', 'issue_type_map')}


def support(state):
    mode = state.get('sprint_mode')
    return {'supported': mode in (None, '', 'active', 'all'), 'sprint_filter': False,
            'warnings': ['스프린트 필터 미지원 — legacy active 설정도 스프린트와 관계없이 조회합니다.'] if mode == 'active' else [],
            'error': None if mode in (None, '', 'active', 'all') else '지원하지 않는 sprint_mode입니다 — active(legacy, 필터 없음) 또는 all만 지원합니다.'}


def _memo(data):
    if data is None:
        return None
    text = data.decode('utf-8-sig')
    if text.count(wfhome.MEMO_START) != 1 or text.count(wfhome.MEMO_END) != 1:
        raise ValueError('메모 구획이 잘못되었습니다 — 원본을 보존하고 배치를 중단합니다')
    return wfhome._split(text)[1]


def _read(path):
    return path.read_bytes() if path.exists() else None


def _same(path, expected):
    if _read(path) != expected:
        raise operations.StateConflict('배치 중 원본이 바뀌었습니다 — 변경을 보존하고 다시 실행하십시오')


def _command(run_dir, script, args):
    cp = subprocess.run([sys.executable, '-X', 'utf8', str(VENDOR / script), *map(str, args)],
                        stdin=subprocess.DEVNULL, capture_output=True, encoding='utf-8', errors='replace', timeout=60)
    fileio.atomic_write(run_dir / (script + '.txt'), (cp.stdout + cp.stderr).encode('utf-8'))
    if cp.returncode:
        raise ValueError(f'{script} 검사 실패 (exit {cp.returncode}) — {cp.stdout[-2000:]} {cp.stderr[-1000:]}')


def run(home, creds, *, send=None, clock=None, checkpoint=None):
    """Collect → aggregate → render → checks → publish → state commit.

    Args: home Path; credentials dict; optional transport/clock/failure seam for tests.
    Returns: receipt dict with UUID, stages, committed flag and durable artifact paths.
    Failure never writes state before all gates pass. A post-commit receipt failure
    is resolved by the run UUID in state, never by advancing the watermark again.
    """
    home = Path(home).resolve()
    with fileio.locked(home):
        run_id = str(uuid4())
        directory = fileio.within(home, home / '.workflow/batches' / run_id)
        directory.mkdir(parents=True, exist_ok=False)
        receipt_path = directory / 'receipt.json'
        receipt = {'schema_version': 1, 'run_id': run_id, 'run_dir': str(directory),
                   'created_at': operations.now(), 'status': 'executing', 'stages': [], 'committed': False}
        _write(receipt_path, receipt)
        state_file = fileio.within(home, wfhome.state_path(home))
        phase = 'prepare'
        try:
            for phase in STAGES:
                receipt['current_stage'] = phase
                _write(receipt_path, receipt)
                if checkpoint:
                    checkpoint(phase)
                if phase == 'prepare':
                    original_state = state_file.read_bytes()
                    state = json.loads(original_state.decode('utf-8-sig'))
                    if state.get('schema_version') != wfhome.SCHEMA_VERSION:
                        raise ValueError('지원하지 않는 업무 schema입니다')
                    supported = support(state)
                    if not supported['supported']:
                        raise ValueError(supported['error'])
                    provenance = vendor_status()
                    if not provenance['identical']:
                        raise ValueError('vendor 원본 hash가 다릅니다 — 모체에서 다시 이관해야 합니다')
                    me = jiraapi.myself(creds, jiraapi.site_url(state), send)
                    if not me.get('accountId') or not me.get('timeZone'):
                        raise ValueError('Jira 계정과 timeZone을 확인해야 합니다')
                    zone = ZoneInfo(me['timeZone'])
                    instant = clock() if clock else datetime.now(timezone.utc)
                    if instant.tzinfo is None or instant.utcoffset() is None:
                        raise ValueError('실행 시각에 UTC offset이 필요합니다')
                    captured = instant.astimezone(zone)
                    previous = datetime.fromisoformat(state['watermark'])
                    if previous.tzinfo is None or captured < previous:
                        raise ValueError('watermark 시각/offset이 잘못되었거나 실행 시계가 후퇴했습니다')
                    day = captured.date().isoformat()
                    if not 2000 <= captured.year <= 2099:
                        raise ValueError('지원 업무 연도는 2000~2099입니다')
                    ymd = captured.strftime('%y%m%d')
                    daily = wfhome.daily_path(home, ymd)
                    original_daily = _read(daily)
                    memo = _memo(original_daily)
                    stage = directory / 'work'
                    staged_state = stage / '.workflow/state.json'
                    staged_daily = stage / 'daily' / (ymd + '.md')
                    staged_agg = stage / '.workflow/agg' / (ymd + '.json')
                    publish_agg = fileio.within(home, home / '.workflow/agg' / (ymd + '.json'))
                    original_agg = _read(publish_agg)
                    fileio.atomic_write(directory / 'original/state.json', original_state)
                    if original_daily is not None:
                        fileio.atomic_write(directory / 'original/daily.md', original_daily)
                        fileio.atomic_write(staged_daily, original_daily)
                    if original_agg is not None:
                        fileio.atomic_write(directory / 'original/aggregate.json', original_agg)
                    # Legacy locally-created roots predate aggregate's first_seen field.
                    for root in state.get('roots', {}).values():
                        root.setdefault('first_seen', day)
                        root.setdefault('last_seen', day)
                    _write(staged_state, state)
                    receipt.update(date=day, watermark_in=state['watermark'], watermark_out=captured.isoformat(),
                                   support=supported, vendor=provenance)
                elif phase == 'collect':
                    since = (previous.astimezone(timezone.utc) - timedelta(minutes=2)).replace(second=0, microsecond=0)
                    collected = jiraapi.fetch(state, stage, run_id, creds, send, since=since)
                    receipt['incremental_since_utc'] = since.isoformat()
                    receipt['queries'] = collected['queries']
                    if any(q['truncated'] for q in collected['queries']):
                        raise ValueError('완전한 Jira 조회가 아닙니다 — watermark를 유지합니다')
                    raw = {q['kind']: p for q, p in zip(collected['queries'], collected['files'])}
                    for path in raw.values():
                        if not jiraapi.fields_complete(_json(path)['issues'],state):
                            raise ValueError('완전한 Jira 필드 응답이 아닙니다 — 설정/권한을 확인하십시오')
                elif phase == 'aggregate':
                    args = ['--changes', raw['changes'], '--open', raw['open'], '--state', staged_state,
                            '--out', staged_agg, '--date', day]
                    if raw.get('orphans'):
                        args += ['--orphans', raw['orphans']]
                    _command(directory, 'aggregate.py', args)
                    agg = _json(staged_agg)
                    counts = agg['counts']
                    narrative = (f"원본 집계: 신규 {counts['new']}건, 이어서 {counts['continuing']}건, "
                                 f"정체 {counts['stalled']}건. 변동 조회 {len(_json(raw['changes'])['issues'])}건.\n"
                                 '이 목록은 예정 업무이며, 수행 기록은 아래 진행 메모에서 따로 남깁니다.\n')
                    fileio.atomic_write(directory / 'narrative.txt', narrative.encode('utf-8'))
                elif phase == 'render':
                    _command(directory, 'render_daily.py', ['--agg', staged_agg, '--narrative', directory / 'narrative.txt',
                             '--out', staged_daily, '--state', staged_state, '--version', 'wsFlow-I7'])
                    if memo is not None:
                        # Vendor text-mode I/O normalizes CRLF; restore the exact memo string.
                        head, _, tail = wfhome._split(staged_daily.read_bytes().decode('utf-8'))
                        fileio.atomic_write(staged_daily, (head + memo + tail).encode('utf-8'))
                        rec = _json(staged_state.parent / 'last_render.json')
                        rec.update(memo_hash_before=wfhome.sha(memo)[:16], memo_hash_after=wfhome.sha(_memo(staged_daily.read_bytes()))[:16])
                        _write(staged_state.parent / 'last_render.json', rec)
                    if memo is not None and _memo(staged_daily.read_bytes()) != memo:
                        raise ValueError('원본 메모 대조 실패')
                elif phase == 'checks':
                    _command(directory, 'loop_checks.py', ['--daily', staged_daily, '--agg', staged_agg,
                             '--state', staged_state, '--evidence', directory / 'checks.json'])
                    checks = _json(directory / 'checks.json')
                    if checks.get('exit_code') != 0 or checks.get('violations'):
                        raise ValueError('일일 불변식 검사 실패')
                elif phase == 'publish':
                    _same(state_file, original_state)
                    _same(daily, original_daily)
                    _same(publish_agg, original_agg)
                    daily_data = staged_daily.read_bytes()
                    agg_data = staged_agg.read_bytes()
                    fileio.atomic_write(publish_agg, agg_data, lambda: _same(publish_agg, original_agg))
                    fileio.atomic_write(daily, daily_data, lambda: (_same(state_file, original_state), _same(daily, original_daily)))
                elif phase == 'commit':
                    final = _json(staged_state)
                    marker = {'run_id': run_id, 'date': day, 'captured_at': captured.isoformat(),
                              'config_hash': operations.digest(config(state)),
                              'aggregate_sha256': hashlib.sha256(agg_data).hexdigest(),
                              'daily_sha256': hashlib.sha256(daily_data).hexdigest(),
                              'changes_count': len(_json(raw['changes'])['issues']), 'run_dir': str(directory)}
                    final.update(watermark=captured.isoformat(), last_success=operations.now(), last_batch=marker)
                    def final_gate():
                        _same(state_file, original_state)
                        _same(daily, daily_data)
                        _same(publish_agg, agg_data)
                    final_gate()
                    # The watermark and proof marker share one final atomic replacement.
                    fileio.atomic_write(state_file, json.dumps(final, ensure_ascii=False, indent=2).encode('utf-8'), final_gate)
                receipt['stages'].append({'stage': phase, 'status': 'succeeded', 'at': operations.now()})
                if phase != 'commit':
                    _write(receipt_path, receipt)
            receipt.update(status='succeeded', committed=True)
        except Exception as exc:
            # A failed receipt write after state replacement cannot turn success into a failed batch.
            try:committed = _json(state_file).get('last_batch', {}).get('run_id') == run_id
            except (OSError,ValueError,TypeError,AttributeError):committed = False
            receipt.update(status='succeeded' if committed else 'failed', committed=committed,
                           failed_stage=None if committed else phase, error=None if committed else str(exc))
        try:
            _write(receipt_path, receipt)
        except OSError:
            receipt['receipt_warning'] = '작업 기록 저장 실패 — state의 run marker로 결과를 확인하십시오'
        return receipt


def status(home):
    """No lock file, directory creation or recovery mutation during reads."""
    home = Path(home).resolve()
    state = wfhome.read_state(home)
    marker = state.get('last_batch') or {}
    directory = fileio.within(home, home / '.workflow/batches')
    runs, warnings = [], []
    for path in sorted(directory.glob('*/receipt.json')) if directory.exists() else []:
        try:
            fileio.within(home, path)
            item = _json(path)
            if (not isinstance(item, dict) or not isinstance(item.get('run_id'), str)
                    or not isinstance(item.get('created_at'), str)
                    or item.get('status') not in ('executing', 'failed', 'succeeded')):
                raise ValueError('잘못된 배치 기록 형식')
            if datetime.fromisoformat(item['created_at'].replace('Z', '+00:00')).tzinfo is None:
                raise ValueError('배치 기록에 offset이 없습니다')
            if item['run_id'] == marker.get('run_id'):
                item.update(status='succeeded', committed=True)
            elif item.get('status') == 'executing':
                item['status'] = 'running_or_interrupted'
            runs.append(item)
        except (OSError, ValueError, KeyError, TypeError):
            warnings.append('배치 작업 기록을 읽지 못했습니다')
    runs.sort(key=lambda r: datetime.fromisoformat(r['created_at'].replace('Z', '+00:00')))
    return {'watermark': state.get('watermark'), 'last_success': state.get('last_success'),
            'latest': runs[-1] if runs else None, 'runs': runs[-20:], 'committed': marker or None,
            'support': support(state), 'warnings': warnings}


def overview(projects, day):
    """S7 signals and Calendar day counts from the same original daily records."""
    selected = calendar_query.iso_date(day)
    calendar = calendar_query.query(projects, day, (selected + timedelta(days=1)).isoformat())
    yesterday = (calendar_query.query(projects, (selected - timedelta(days=1)).isoformat(), day)
                 if selected.year > 2000 or selected.month > 1 or selected.day > 1 else None)
    rows, homes = [], {}
    for project in projects:
        home = wfhome.wf_home_for(project).resolve()
        records = calendar['days'][0]
        tasks = [t for t in records['tasks'] if project['id'] in t['project_ids']]
        notes = [m for m in records['memos'] if project['id'] in m['project_ids']]
        row = {'id': project['id'], 'alias': project.get('alias', project['id']), 'date': day,
               'date_records': {'task_count': len(tasks) if records['complete'] else None,
                                'memo_count': len(notes) if records['complete'] else None},
               'enabled': False, 'complete': False, 'signals': None, 'warnings': []}
        try:
            if home not in homes:
                state = wfhome.read_state(home)
                batch = status(home)
                marker = batch['committed']
                signal = None
                if marker:
                    path = fileio.within(home, home / '.workflow/agg' / (marker['date'].replace('-', '')[2:] + '.json'))
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() == marker['aggregate_sha256'] and operations.digest(config(state)) == marker['config_hash']:
                        agg = json.loads(raw)
                        signal = {**agg['counts'], 'epic_unlisted': agg['epic_unlisted']['epic_count'],
                                  'title_filtered': agg['title_filtered']['count'], 'csc_missing': agg['csc_missing']['count']}
                pending = sum(not c.get('jira_key') for r in state.get('roots', {}).values() for c in r.get('children', []))
                homes[home] = (state, batch, signal, pending)
            state, batch, signal, pending = homes[home]
            previous = yesterday['days'][0] if yesterday else None
            prior_content = previous and any(project['id'] in x['project_ids'] for x in previous['tasks'] + previous['memos'])
            missing_close = ('suspected' if prior_content and batch['committed'] and batch['committed']['date'] == day and batch['committed']['changes_count'] == 0
                             else 'unknown' if prior_content or (previous and not previous['complete']) else 'no_record')
            row.update(enabled=True, schema_ok=state.get('schema_version') == wfhome.SCHEMA_VERSION,
                       batch=batch, signals=signal, pending_children=pending, missing_close=missing_close,
                       complete=signal is not None and batch['latest'] is not None and batch['latest']['status'] == 'succeeded' and batch['committed']['date'] == day and records['complete'] and not batch['warnings'],
                       support=batch['support'])
            if not row['schema_ok']:
                row['complete'] = False
                row['warnings'].append('업무 schema 불일치')
            if not batch['support']['supported']:
                row['complete'] = False
                row['warnings'].append(batch['support']['error'])
            row['warnings'] += batch['warnings'] + batch['support']['warnings']
            if not row['complete']:
                row['warnings'].append('해당 날짜의 최신 완전 배치가 아닙니다 — 이전 집계/미확인을 0건으로 해석하지 마십시오')
            if missing_close == 'suspected':
                row['warnings'].append('어제 마감 누락 의심 — 어제 메모가 있고 최근 Jira 변동 조회가 0건입니다')
        except FileNotFoundError:
            if wfhome.state_path(home).exists():
                row.update(enabled=True, warnings=['배치 원본 또는 집계 파일을 찾지 못했습니다'])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            row.update(enabled=True, warnings=['업무 상태를 읽지 못했습니다'])
        rows.append(row)
    return {'schema_version': 1, 'date': day, 'captured_at': operations.now(),
            'projects': rows, 'calendar': calendar, 'complete': all(r['complete'] for r in rows if r['enabled'])}
