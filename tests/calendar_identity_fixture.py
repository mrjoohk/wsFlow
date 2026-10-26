"""Owned synthetic managed identity replacement behind the actual local HTTP server."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from wsflow import calendar_store
from wsflow.server import serve


def main():
    with tempfile.TemporaryDirectory(prefix='wsflow_uid_browser_') as temp:
        home=Path(temp).resolve();root=home/'root';checkout=root/'project';checkout.mkdir(parents=True)
        os.environ['WSFLOW_DATA_DIR']=str(home/'data')
        def write(path,value):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value),encoding='utf-8')
        registry=root/'projects.json';projects=[{'id':'p','alias':'Current project','path':str(checkout),'tags':[]}]
        write(registry,{'roots':[{'path':str(root),'template':'none'}],'projects':projects})
        write(root/'.wsflow/workspace.json',{'schema_version':1,'root_uid':'root-1','root_path':str(root)})
        profile={'schema_version':1,'root_uid':'root-1','project_uid':'old-project','checkout_path':str(checkout)}
        write(checkout/'.wsflow/project.json',profile)
        store=calendar_store.Store(registry)
        payload={'date':'2026-10-17','body':'HISTORICAL NOTE BODY','project_ids':['p']}
        request={'kind':'note','action':'create','payload':payload}
        plan=store.preview(request,projects);record=store.apply(plan['plan_id'],plan['operation_id'],projects)['record']
        stale=store.preview(request,projects)
        write(checkout/'.wsflow/project.json',{**profile,'project_uid':'new-project'})
        httpd=serve(registry,'127.0.0.1',0)
        print(json.dumps({'url':f'http://127.0.0.1:{httpd.server_address[1]}','token':httpd.wsflow_token,'record_id':record['id'],
                          'old_plan':{'plan_id':stale['plan_id'],'operation_id':stale['operation_id']}}),flush=True)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        try:sys.stdin.readline()
        finally:httpd.shutdown();httpd.server_close()


if __name__=='__main__':main()
