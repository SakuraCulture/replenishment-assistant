"""Execute copied business prompts with the installed authenticated Codex model.

No personal config mutation, persisted chat, shell/browser tools or real data.
This exercises a client, not a delegated coding agent.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def main():
    p=argparse.ArgumentParser();p.add_argument('--workspace',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--cases',default='check,calculate,compare,review,explain,history,stop,retry,adopt');a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);workspace=a.workspace.resolve()
    config=json.loads((workspace/'clients.json').read_text('utf-8'))['workbuddy']['mcpServers']['replenishment']
    codex=shutil.which('codex.exe')
    if not codex:raise FileNotFoundError('Codex not installed')
    report={'client':'Codex CLI actual model','synthetic':True,'cases':[],'passed':False}
    for case in a.cases.split(','):
        ident=case+'-'+str(time.time_ns());temp=workspace/'request.tmp'
        temp.write_text(json.dumps({'id':ident,'case':case}),'utf-8');os.replace(temp,workspace/'request.json')
        deadline=time.monotonic()+20
        while True:
            response=workspace/'response.json'
            value=json.loads(response.read_text('utf-8')) if response.exists() else {}
            if value.get('id')==ident:break
            if time.monotonic()>deadline:raise TimeoutError('Fixture did not prepare '+case)
            time.sleep(.1)
        if 'error' in value:
            report['cases'].append({'case':case,'prepared':False,'error':value['error']});break
        folder=out/case;folder.mkdir();prompt=value['prompt'];(folder/'prompt.txt').write_text(prompt,'utf-8')
        command=[codex,'exec','--ignore-user-config','--ephemeral','--skip-git-repo-check','--sandbox','read-only','--json','--color','never','-C',str(folder)]
        for feature in ('shell_tool','apps','plugins','multi_agent','browser_use','computer_use'):
            command+=['--disable',feature]
        for key,val in config.items():command+=['-c','mcp_servers.replenishment.'+key+'='+json.dumps(val,ensure_ascii=False)]
        command+=['-c','mcp_servers.replenishment.startup_timeout_sec=30','-c','mcp_servers.replenishment.tool_timeout_sec=60','-o',str(folder/'answer.txt'),'-']
        start=time.monotonic()
        with (folder/'events.jsonl').open('w',encoding='utf-8') as log,(folder/'stderr.txt').open('w',encoding='utf-8') as err:
            child=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=log,stderr=err,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
            try:child.communicate(prompt,timeout=300);code=child.returncode
            except subprocess.TimeoutExpired:child.terminate();child.wait(15);code=-1
        events=[]
        for line in (folder/'events.jsonl').read_text('utf-8').splitlines():
            try:events.append(json.loads(line))
            except ValueError:pass
        entry={'case':case,'exit_code':code,'seconds':time.monotonic()-start,'events':len(events),'values':value['values']}
        report['cases'].append(entry)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        print(json.dumps(entry,ensure_ascii=False),flush=True)
        if code:break
    # Human/independent result comparison is required; process success alone is not acceptance.
    report['execution_completed']=len(report['cases'])==len(a.cases.split(',')) and all(x.get('exit_code')==0 for x in report['cases'])
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')


if __name__=='__main__':main()
