"""I7 real app/Git/vendor with Jira replaced only at its transport boundary."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from helpers import git, make_repo, make_bare
from test_i7 import Provider, STATE, CREDS, WHEN, put, issue
from wsflow import auth, jiraapi, jirawrite, workflow, wfhome
from wsflow.server import serve
from wsflow.store import Store

with tempfile.TemporaryDirectory(prefix='wsflow I7 browser ') as temp:
    base=Path(temp);root=base/'workspace';root.mkdir();os.environ['WSFLOW_DATA_DIR']=str(base/'userdata');os.environ.pop('WSFLOW_GOOGLE_CLIENT_FILE',None)
    home=make_repo(root,'Alpha');remote=make_bare(base);git(home,'remote','add','origin',str(remote))
    put(home/'.gitignore','daily/\n.workflow/\nweekly/\nstate.json\njira_project_key_id.txt\n');put(home/'USER.txt','USER ORIGINAL');put(home/'PROJECT_RULES.md','USER POLICY')
    git(home,'add','-A');git(home,'commit','-m','fixture');git(home,'push','-u','origin','main')
    git(base,'clone',str(remote),str(base/'other'));other=base/'other';put(other/'remote.txt','from other');git(other,'add','-A');git(other,'commit','-m','other');git(other,'push')
    put(home/'.workflow/state.json',json.dumps(STATE));memo=wfhome.read_memo(home,'261018')
    wfhome.write_memo(home,'261018','- XXX-11: ACTUAL PRIVATE WORK\nCOMMON PRIVATE',memo['sha'])
    (base/'registry').mkdir();registry=Store(base/'registry/projects.json');registry.add_root(str(root));project=registry.add_project(str(home),'Alpha <img src=x onerror=alert(1)>')
    beta=registry.create_project(str(root),'Beta');provider=Provider();control=base/'control.json';put(control,'{}')
    original_run=workflow.run
    def transport(url,*args):
        value=json.loads(control.read_text(encoding='utf-8'));provider.truncated=value.get('mode')=='truncated';provider.visible=value.get('mode')!='invisible'
        provider.open=[issue('OTHER-1')] if value.get('mode')=='invalid_scope' else [issue()]
        return provider(url,*args)
    with mock.patch.object(auth,'load',return_value=CREDS),mock.patch.object(jiraapi,'_send',transport),mock.patch.object(jirawrite,'_send',transport),mock.patch.object(workflow,'run',lambda h,c:original_run(h,c,clock=lambda:WHEN)):
        httpd=serve(registry.file,'127.0.0.1',0)
        print(json.dumps({'url':'http://127.0.0.1:'+str(httpd.server_address[1]),'token':httpd.wsflow_token,'pid':project['id'],'other_id':beta['id'],
                          'home':str(home),'other':str(other),'control':str(control),'base':str(base)}),flush=True)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        try:sys.stdin.readline()
        finally:httpd.shutdown();httpd.server_close()
