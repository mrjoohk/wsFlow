"""Temporary actual app, no user ROOT initialization or external services."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from wsflow.server import serve
from wsflow.store import Store


with tempfile.TemporaryDirectory(prefix='wsflow I5 browser ') as temp:
    base=Path(temp);root=base/'workspace with spaces';root.mkdir()
    os.environ['WSFLOW_DATA_DIR']=str(base/'userdata')
    registry=Store(base/'projects.json');registry.add_root(str(root))
    project=registry.create_project(str(root),'Alpha');other=registry.create_project(str(root),'Beta')
    home=Path(project['path'])
    for name in ('alpha','unselected'):
        package=root/'skills'/name;(package/'references').mkdir(parents=True)
        (package/'SKILL.md').write_text('---\nname: '+name+'\ndescription: fixture\n---\nRead [support](references/data.md).',encoding='utf-8')
        (package/'references/data.md').write_text('SUPPORT '+name,encoding='utf-8')
    (root/'GLOBAL_RULES.md').write_bytes(b'COMMON USER RULES\r\n')
    (home/'AGENTS.md').write_bytes(b'USER HEADER\r\nNO NEWLINE')
    (home/'PROJECT_RULES.md').write_text('PROJECT USER POLICY',encoding='utf-8')
    (home/'.claude/skills/legacy').mkdir(parents=True)
    (home/'.claude/skills/legacy/SKILL.md').write_text('LEGACY USER SKILL',encoding='utf-8')
    (home/'.workflow').mkdir();(home/'.workflow/state.json').write_text('{"watermark":"keep","roots":{"x":1}}',encoding='utf-8')
    httpd=serve(registry.file,'127.0.0.1',0)
    print(json.dumps({'url':'http://127.0.0.1:'+str(httpd.server_address[1]),'token':httpd.wsflow_token,'home':str(home),'root':str(root),'pid':project['id'],'other':other['id']}),flush=True)
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    try:sys.stdin.readline()
    finally:httpd.shutdown();httpd.server_close()
