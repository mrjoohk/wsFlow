"""Actual local app, synthetic Google HTTP transport; never calls Google."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path[:0]=[str(Path(__file__).resolve().parent.parent),str(Path(__file__).resolve().parent)]
from test_i3 import Provider,event
from wsflow.server import serve
from wsflow import google_calendar

with tempfile.TemporaryDirectory(prefix='wsflow_i3_browser_') as temp:
    root=Path(temp);workspace=root/'workspace';workspace.mkdir();data=root/'userdata';data.mkdir()
    os.environ['WSFLOW_DATA_DIR']=str(data)
    (data/'google-client.json').write_text(json.dumps({'installed':{'client_id':'test.apps.googleusercontent.com','client_secret':'synthetic'}}))
    registry=workspace/'projects.json';registry.write_text(json.dumps({'roots':[],'projects':[]}))
    provider=Provider();control=root/'control.json';control.write_text('{}')
    def transport(method,url,data=None,headers=None):
        settings=json.loads(control.read_text())
        if settings.get('fail') and '/calendar/v3/' in url:raise google_calendar.GoogleError(settings['fail'])
        provider.pages[('range','')]={'items':[event('instance',summary='<img src=x onerror=alert(1)> Google 일정',
                    description='GOOGLE PRIVATE BODY',recurringEventId='master'),event('cancel',status='cancelled')]}
        return provider(method,url,data,headers)
    httpd=serve(registry,'127.0.0.1',0,google_transport=transport)
    print(json.dumps({'url':f'http://127.0.0.1:{httpd.server_port}','token':httpd.wsflow_token,'control':str(control)}),flush=True)
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    try:sys.stdin.readline()
    finally:httpd.shutdown();httpd.server_close()
