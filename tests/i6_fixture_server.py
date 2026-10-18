"""Real temporary app/SQLite; observations and Google warning are synthetic."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from wsflow import context_projection, session_prep
from wsflow.server import serve
from wsflow.store import Store

with tempfile.TemporaryDirectory(prefix='wsflow I6 browser ') as temp:
    base=Path(temp);root=base/'workspace with spaces';root.mkdir()
    os.environ['WSFLOW_DATA_DIR']=str(base/'userdata');os.environ.pop('WSFLOW_GOOGLE_CLIENT_FILE',None)
    registry_dir=base/'registry';registry_dir.mkdir()
    registry=Store(registry_dir/'projects.json');registry.add_root(str(root))
    a=registry.create_project(str(root),'Alpha');b=registry.create_project(str(root),'Beta');home=Path(a['path'])
    (root/'GLOBAL_RULES.md').write_text('COMMON POLICY',encoding='utf-8');(home/'PROJECT_RULES.md').write_text('PROJECT POLICY',encoding='utf-8')
    (home/'AGENTS.md').write_bytes(b'USER ENTRY\r\n')
    package=root/'skills/alpha';(package/'references').mkdir(parents=True)
    (package/'SKILL.md').write_text('---\nname: alpha\ndescription: fixture\n---\nRead [ref](references/data.md).',encoding='utf-8')
    (package/'references/data.md').write_text('SUPPORT PRIVATE',encoding='utf-8')
    (home/'.workflow').mkdir();(home/'.workflow/state.json').write_text('{"watermark":"keep","roots":{"x":1}}',encoding='utf-8')
    service=context_projection.Service(registry)
    for kind in ('root_init','project_init'):
        p=service.preview(a['id'],{'kind':kind,'backend':'managed-copy','adopt_existing':True,'selected_skills':['common:alpha'] if kind=='project_init' else []})
        assert not p['blockers'],p['blockers'];assert service.apply(a['id'],p['plan_id'],p['operation_id'])['status']=='succeeded'
    sessions=session_prep.Service(registry)
    httpd=serve(registry.file,'127.0.0.1',0)
    if httpd.google_calendar:httpd.google_calendar.background_error='SYNTHETIC Google sync warning'
    print(json.dumps({'url':'http://127.0.0.1:'+str(httpd.server_address[1]),'token':httpd.wsflow_token,'pid':a['id'],'other':b['id'],
                      'home':str(home),'root':str(root),'evidence_dir':str(sessions.evidence_dir)}),flush=True)
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    try:sys.stdin.readline()
    finally:httpd.shutdown();httpd.server_close()
