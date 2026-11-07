"""Time the delivered component using a fresh offline fixture; no platform action."""
import json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
from prepare_collection_maintenance import seed
from aoxiang_client import ProcessClient
from aoxiang_jobs import Jobs

out=ROOT/'qa/deletion_0462_final/component-startup-probe'
fixture=seed(out)
component=ROOT/'交付_0.46.2_rc1/补货助手/_internal/components/aoxiang'
assert (component/'runtime/node.exe').is_file()
report={'live_platform':False,'component':str(component),'timings':{}}
client=ProcessClient(component,Path(fixture['state_root']))
try:
    started=time.monotonic();client.start();report['timings']['start']=time.monotonic()-started
    started=time.monotonic();Jobs(out/'settings/aoxiang/jobs.sqlite3').synchronize(client);report['timings']['synchronize']=time.monotonic()-started
    started=time.monotonic();config=client.request('configuration');report['timings']['configuration']=time.monotonic()-started
    report['configured']=config['configured']
    report['passed']=True
finally:
    report['closed']=client.close()
    (out/'probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
