"""Verify correction and re-open through the installed dialog using disposable cases."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from verify_jst_one_click_exe import ROOT, contents, sha, write


def seed(root, stage):
    import jst_batches
    import jst_orders
    import order_lifecycle
    import warehouse_catalog
    import work_repository as wr
    from test_jst import catalog
    from test_lifecycle import LifecycleTests

    root.mkdir()
    sample=LifecycleTests();sample.root=root;sample.count=0
    from test_archive_orders import seed as signed
    sample.work,sample.repo,sample.payload=signed(root)
    sample.path=sample.work.path
    heads=order_lifecycle.current_targets(sample.path)
    hb,sd=(next(h for h in heads if h['warehouse']==warehouse) for warehouse in ('河北仓','山东仓'))
    heads=(sd,hb) if stage=='allocation' else (hb,sd)
    if stage=='signed':
        data=catalog()
        data['items']['00123']={**data['items']['0001'],'商品编码':'00123','商品名称':'隔离新增商品','_row':3}
        data['revision']=warehouse_catalog.revision(data['items'])
        warehouse_catalog.Repository(sample.path.parent).save(data)
        ids=[head['digest'] for head in heads]
    elif stage=='allocation':
        for head,quantity in zip(heads,(16,20)):
            sample.h=head
            sample.save('return',[{'供货仓':head['warehouse'],'商品编码':'0001','门店确认数量':quantity}])
        result=jst_batches.execute(sample.path,jst_batches.preview_new(sample.path,heads,
            {'河北仓':catalog('河北仓',100),'山东仓':catalog('山东仓',145)}))
        assert not result['errors'],result
        with closing(wr.read_connection(sample.path)) as db:
            ids=[jst_orders.active(db,head) for head in heads]
    else:
        for head in heads:
            sample.h=head
            row={'供货仓':head['warehouse'],'商品编码':'0001'}
            for document in ('return','operations','payment','receipt'):
                sample.save(document,[row])
                if document==stage:break
        ids=[order_lifecycle.history(sample.path,head,stage)[0]['digest'] for head in heads]
    write(root/'amendment-fixture.json',{'synthetic':True,'stage':stage,'head':heads[0],'other':heads[1],
          'sku':'00123' if stage=='signed' else '0001','quantity':'12' if stage=='allocation' else '3',
          'prior_id':ids[0],'other_id':ids[1]})


def launch(exe,case,phase):
    env=os.environ.copy()
    for key in ('QT_QPA_PLATFORM','QT_PLUGIN_PATH','PYTHONHOME','PYTHONPATH','REPLENISHMENT_PORTABLE_PATHS'):
        env.pop(key,None)
    env.update(LOCALAPPDATA=str(case/'local'),APPDATA=str(case/'roaming'),
               REPLENISHMENT_DATA_DIR=str(case/'settings'),PYTHONUTF8='1')
    command=[str(exe)] if exe else [sys.executable,str(ROOT/'src/app.py')]
    with (case/(phase+'-app.log')).open('xb') as log:
        result=subprocess.run([*command,'--order-amendments-smoke',str(case),phase],cwd=case,env=env,
                              stdout=log,stderr=subprocess.STDOUT,timeout=130,creationflags=subprocess.CREATE_NO_WINDOW)
    file=case/(phase+'-report.json')
    report=json.loads(file.read_text('utf-8')) if file.is_file() else {}
    assert result.returncode==0 and report.get('passed') and report['actual_exe']==bool(exe),(result.returncode,report,file)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();root=args.out.resolve();root.mkdir(parents=True,exist_ok=False)
    exe=app=files=None
    if args.zip:
        with zipfile.ZipFile(args.zip) as archive:
            assert archive.testzip() is None
            archive.extractall(root/'unpacked')
        app=root/'unpacked/补货助手';exe=app/'补货助手.exe';files=contents(app)
    reports={}
    for stage in ('signed','return','operations','allocation','receipt','payment'):
        case=root/stage;seed(case,stage)
        one,two=(launch(exe,case,phase) for phase in ('exercise','reopen'))
        assert one==two
        reports[stage]=one
    if app:assert contents(app)==files
    write(root/'report.json',{'passed':True,'synthetic':True,'actual_exe':bool(exe),'cases':reports,
                              'exe_sha256':sha(exe) if exe else None,'zip_sha256':sha(args.zip) if exe else None})
    print(root/'report.json')


if __name__=='__main__':
    main()
