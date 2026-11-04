"""Actual installed Codex MCP client. Ephemeral local protocol context, no model turn."""
import argparse,json,os,queue,shutil,subprocess,threading,time
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    config=json.loads(a.config.read_text('utf-8'))
    codex=shutil.which('codex.exe')
    if not codex:raise FileNotFoundError('Installed Codex executable missing')
    home=out/'codex-home';home.mkdir();(home/'config.toml').write_text(config['codex'],'utf-8')
    responses=queue.Queue();events=[];n=0
    log=(out/'stderr.log').open('w',encoding='utf-8')
    proc=subprocess.Popen([codex,'app-server','--stdio'],env={**os.environ,'CODEX_HOME':str(home)},
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
    def reader():
        for line in proc.stdout:
            try:responses.put(json.loads(line))
            except ValueError:pass
    threading.Thread(target=reader,daemon=True).start()
    def call(method,params):
        nonlocal n
        n+=1;proc.stdin.write(json.dumps({'id':n,'method':method,'params':params})+'\n');proc.stdin.flush()
        end=time.monotonic()+60
        while time.monotonic()<end:
            try:r=responses.get(timeout=.5)
            except queue.Empty:
                if proc.poll() is not None:raise RuntimeError('Codex app-server exited')
                continue
            if r.get('id')==n:
                if 'error' in r:raise RuntimeError(r['error'])
                return r['result']
            # Keep only event method names, never unrelated auth/account data.
            events.append(r.get('method'))
        raise TimeoutError(method)
    report={'passed':False,'client':'installed Codex app-server','exe':codex,'model_turns':0}
    try:
        report['initialize']=call('initialize',{'clientInfo':{'name':'replenishment_acceptance','version':'1'},'capabilities':{'experimentalApi':True}})
        proc.stdin.write(json.dumps({'method':'initialized'})+'\n');proc.stdin.flush()
        inventory=call('mcpServerStatus/list',{'serverName':'replenishment','limit':10,'detail':'toolsAndAuthOnly'})
        report['inventory']=inventory
        # Ephemeral context supplies a connection owner for explicit tool calls; no turn/start, no AI agent.
        context=call('thread/start',{'ephemeral':True,'cwd':str(out),'approvalPolicy':'never'})
        tid=context['thread']['id']
        def tool(name,args):return call('mcpServer/tool/call',{'threadId':tid,'server':'replenishment','tool':name,'arguments':args})
        report['context']=tool('workbench_context',{})
        report['resources']=tool('find_resources',{'kind':'tasks','limit':2})
        report['invalid_argument']=tool('find_resources',{'kind':'tasks','limit':101})
        if 'workbench_context' not in json.dumps(inventory):raise AssertionError('Codex did not discover tools')
        if 'INVALID_ARGUMENT' not in json.dumps(report['invalid_argument']):raise AssertionError('Error not returned')
        def value(name,args):
            reply=tool(name,args)
            if reply.get('isError'):raise AssertionError(reply)
            return reply['structuredContent']
        ctx=report['context']['structuredContent']
        if not ctx['scope'].startswith('查询'):raise AssertionError('Chinese text was not preserved')
        prepared=value('prepare_calculation',{'context_token':ctx['context_token'],'operation_key':'actual-codex-trial','mode':'trial','stores':ctx['selected_stores']})
        def completed(tid):
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                status=value('task_status',{'task_id':tid})
                if status['state'] not in ('preparing','running','stopping'):return status
                time.sleep(.2)
            raise TimeoutError('Actual Codex task did not finish')
        preview=completed(prepared['task_id'])
        if preview['state']!='prepared':raise AssertionError(preview)
        task=value('run_calculation',{'preview_id':prepared['task_id']})
        status=completed(task['task_id'])
        if status['state']!='completed':raise AssertionError(status)
        report['calculation']={'prepare':preview,'task':status,'result':value('calculation_results',{'task_id':task['task_id']}),
            'opened':value('open_workbench_view',{'view':'results'})}
        report['passed']=True
    except Exception as e:report['error']=str(e)
    finally:
        proc.stdin.close()
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=10)
        log.close();report['events']=events
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    if not report['passed']:raise SystemExit(report.get('error','Failed'))

if __name__=='__main__':main()
