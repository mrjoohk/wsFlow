"""Local-only schema3 UI fixture; all provider requests replaced with synthetic reads."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from test_i7 import Provider, STATE, CREDS, WHEN
from wsflow import auth,jiraapi,workflow
from wsflow.server import serve
from wsflow.store import Store

with tempfile.TemporaryDirectory(prefix='wsflow schema3 browser ') as temp:
    base=Path(temp);home=base/'Legacy';home.mkdir();(home/'.workflow').mkdir()
    os.environ['WSFLOW_DATA_DIR']=str(base/'userdata');os.environ.pop('WSFLOW_GOOGLE_CLIENT_FILE',None)
    state={**copy.deepcopy(STATE),'schema_version':3,'epic_keys':None,'csc_field':None,'csc_value':None,'user_field':{'none':None,'text':'USER KEEP'}}
    (home/'.workflow/state.json').write_bytes(b'\xef\xbb\xbf'+json.dumps(state,indent=1).replace('\n','\r\n').encode())
    (home/'USER.txt').write_bytes(b'USER ORIGINAL\x00');(home/'daily').mkdir()
    (home/'daily/261018.md').write_bytes(b'# daily\r\n<!-- MEMO:START -->\r\nKEEP MEMO\r\n<!-- MEMO:END -->\r\n')
    (base/'registry').mkdir()
    registry=Store(base/'registry/projects.json');pr=registry.add_project(str(home))
    provider=Provider();original=workflow.run
    def send(url,headers,payload,method):
        assert method=='GET' or method=='POST' and url.endswith('/search/jql'), 'remote business writes prohibited'
        return provider(url,headers,payload,method)
    with mock.patch.object(auth,'load',return_value=CREDS),mock.patch.object(jiraapi,'_send',send),mock.patch.object(workflow,'run',lambda h,c:original(h,c,clock=lambda:WHEN)):
        httpd=serve(registry.file,'127.0.0.1',0)
        print(json.dumps({'url':'http://127.0.0.1:'+str(httpd.server_address[1]),'token':httpd.wsflow_token,'pid':pr['id'],'home':str(home)}),flush=True)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        try:sys.stdin.readline()
        finally:httpd.shutdown();httpd.server_close()
