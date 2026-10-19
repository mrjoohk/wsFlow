"""Real I4 app, synthetic Google; serialized acknowledged stdin/stdout controls."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent)]
from test_i4 import WriteProvider
from wsflow import google_calendar as google
from wsflow.server import serve


def main():
    with tempfile.TemporaryDirectory(prefix='wsflow_i4_browser_') as temp:
        root = Path(temp); workspace = root/'workspace'; workspace.mkdir()
        data = root/'userdata'; data.mkdir(); os.environ['WSFLOW_DATA_DIR'] = str(data)
        (data/'google-client.json').write_text(json.dumps({'installed': {
            'client_id':'test.apps.googleusercontent.com','client_secret':'synthetic'}}))
        registry = workspace/'projects.json'
        registry.write_text(json.dumps({'roots':[],'projects':[]}))
        provider = WriteProvider(); lock = threading.RLock(); settings = {}

        def snapshot():
            return {'writes':[{'method':c[0],'path':c[1],'if_match':c[3].get('If-Match')} for c in provider.writes()],
                    'events':[{'id':e['id'],'title':e['summary'],'etag':e['etag']} for e in provider.events.values()]}

        def transport(method, url, data=None, headers=None):
            with lock:
                if url == google.TOKEN_URL and data.get('grant_type') == 'authorization_code':
                    provider.scopes = [*google.SCOPES, *([google.WRITE_SCOPE] if data['code']=='write' else [])]
                if settings.get('fail') and '/calendar/v3/' in url:
                    raise google.GoogleError(settings['fail'])
                return provider(method, url, data, headers)

        httpd = serve(registry, port=0, google_transport=transport)
        print(json.dumps({'protocol':1,'url':f'http://127.0.0.1:{httpd.server_port}',
                          'token':httpd.wsflow_token,'home':str(workspace)}), flush=True)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
        try:
            for line in sys.stdin:
                request = json.loads(line); ident = request['id']; action = request['action']
                try:
                    with lock:
                        if action == 'control':
                            new = request['data']
                            if new.get('edit_id'): provider.edit(new['edit_id'], summary=new['summary'])
                            settings.clear(); settings.update(new); provider.drop = new.get('drop')
                            result = {'applied':True}
                        elif action == 'stats': result = snapshot()
                        elif action == 'stop': result = {'stopping':True}
                        else: raise ValueError('unknown fixture action')
                    print(json.dumps({'id':ident,'ok':True,'result':result}), flush=True)
                except Exception as error:
                    print(json.dumps({'id':ident,'ok':False,'error':str(error)}), flush=True)
                if action == 'stop': break
        finally:
            httpd.shutdown(); httpd.server_close(); thread.join(timeout=5)


if __name__ == '__main__': main()
