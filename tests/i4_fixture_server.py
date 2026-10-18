"""Actual I4 app with synthetic Google writes. Control/stats stay in temp files."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path[:0]=[str(Path(__file__).resolve().parent.parent),str(Path(__file__).resolve().parent)]
from test_i4 import WriteProvider
from wsflow import google_calendar as google, fileio
from wsflow.server import serve

with tempfile.TemporaryDirectory(prefix='wsflow_i4_browser_') as temp:
    root=Path(temp);workspace=root/'workspace';workspace.mkdir();data=root/'userdata';data.mkdir()
    os.environ['WSFLOW_DATA_DIR']=str(data)
    (data/'google-client.json').write_text(json.dumps({'installed':{'client_id':'test.apps.googleusercontent.com','client_secret':'synthetic'}}))
    registry=workspace/'projects.json';registry.write_text(json.dumps({'roots':[],'projects':[]}))
    provider=WriteProvider();control=root/'control.json';stats=root/'stats.json';control.write_text('{}');seen=set()
    def transport(method,url,data=None,headers=None):
        settings=json.loads(control.read_text())
        if url==google.TOKEN_URL and data.get('grant_type')=='authorization_code':
            provider.scopes=[*google.SCOPES,*([google.WRITE_SCOPE] if data['code']=='write' else [])]
        command=settings.get('command')
        if command and command not in seen:
            seen.add(command)
            if settings.get('edit_id'):provider.edit(settings['edit_id'],summary=settings['summary'])
            provider.drop=settings.get('drop')
        try:
            if settings.get('fail') and '/calendar/v3/' in url:raise google.GoogleError(settings['fail'])
            return provider(method,url,data,headers)
        finally:
            output={'writes':[{'method':c[0],'path':c[1],'if_match':c[3].get('If-Match')} for c in provider.writes()],
                    'events':[{'id':e['id'],'title':e['summary'],'etag':e['etag']} for e in provider.events.values()]}
            fileio.atomic_write(stats,json.dumps(output).encode())
    httpd=serve(registry,port=0,google_transport=transport)
    print(json.dumps({'url':f'http://127.0.0.1:{httpd.server_port}','token':httpd.wsflow_token,'control':str(control),'stats':str(stats)}),flush=True)
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    try:sys.stdin.readline()
    finally:httpd.shutdown();httpd.server_close()
