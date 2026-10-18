"""Ephemeral actual wsFlow server for the I2 browser harness; synthetic data only."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from wsflow.calendar_store import Store
from wsflow.server import serve
from wsflow.wfhome import MEMO_START, MEMO_END


with tempfile.TemporaryDirectory(prefix='wsflow_i2_browser_') as temp:
    root=Path(temp);workspace=root/'workspace';workspace.mkdir()
    os.environ['WSFLOW_DATA_DIR']=str(root/'userdata')
    projects=[]
    for ident in ('a','b'):
        home=workspace/ident;home.mkdir();(home/'.workflow').mkdir();(home/'daily').mkdir()
        (home/'.workflow/state.json').write_text(json.dumps({'schema_version':4,'project_key':'XXX','roots':{},'issue_type_map':{'site_url':'https://example.atlassian.net'}}),encoding='utf-8')
        text='- XXX-1: TASK PRIVATE FIRST\n- XXX-1: TASK PRIVATE SECOND\n- XXX-99: ARCHIVED TASK PRIVATE\nCOMMON PRIVATE\nSECOND COMMON LINE' if ident=='a' else '- YYY-1: OTHER PROJECT PRIVATE'
        (home/'daily/261017.md').write_text(f'{MEMO_START}\n{text}\n{MEMO_END}',encoding='utf-8')
        projects.append({'id':ident,'alias':'Alpha' if ident=='a' else 'Beta','path':str(home),'tags':[]})
    registry=workspace/'projects.json';registry.write_text(json.dumps({'roots':[],'projects':projects}),encoding='utf-8')
    store=Store(registry)
    for i in range(5):
        payload={'title':'검토 일정 '+str(i+1),'body':'EVENT DESCRIPTION PRIVATE','location':'회의실','all_day':True,'start_date':'2026-10-17','end_date':'2026-10-19','project_ids':['a']}
        plan=store.preview({'kind':'event','action':'create','payload':payload},projects)
        store.apply(plan['plan_id'],plan['operation_id'],projects)
    plan=store.preview({'kind':'note','action':'create','payload':{'date':'2026-10-17','body':'NOTE PRIVATE FIRST\nNOTE PRIVATE SECOND','project_ids':['a']}},projects)
    store.apply(plan['plan_id'],plan['operation_id'],projects)
    httpd=serve(registry,'127.0.0.1',0)
    print(json.dumps({'url':f'http://127.0.0.1:{httpd.server_address[1]}','token':httpd.wsflow_token}),flush=True)
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    try:sys.stdin.readline()
    finally:httpd.shutdown();httpd.server_close()
