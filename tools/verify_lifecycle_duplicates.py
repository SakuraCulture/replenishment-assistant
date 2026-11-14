"""New-directory-only real source/EXE duplicate selection and reopen acceptance."""
import argparse,copy,hashlib,json,os,sqlite3,subprocess,sys,traceback
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')
def read(path):return json.loads(Path(path).read_text('utf-8'))
def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def seed(out):
    from test_archive_orders import seed as source_seed
    import normal_orders as normal,work_repository as wr,purchases,result_semantics as semantics
    import lifecycle_io as io
    import openpyxl
    work,repo,payload=source_seed(out)
    hb=payload['result']['rows'][0];sd=payload['result']['rows'][1]
    rows=[]
    for n in range(10):
        row=copy.deepcopy(hb);row.update(E=f'SYNTHETIC-{n+1:03d}',D=f'合成验收商品{n+1}')
        rows.append(row)
    rows.append(sd);payload['result']['rows']=rows;payload['result']['summary']=semantics.summarize(rows)
    with closing(work.connect()) as db,db:
        db.execute('UPDATE normal_revisions SET digest=?,payload=?',(normal.fingerprint(payload['result']),normal.encode(payload,db)))
    heads=purchases.targets(work.path);by_warehouse={h['warehouse']:h for h in heads}
    def workbook(path,warehouse,quantities):
        book=openpyxl.Workbook();book.remove(book.active)
        for sheet,sku_quantities in quantities:
            ws=book.create_sheet(sheet);ws.append(io.fields('return'))
            for sku,quantity in sku_quantities:
                head=by_warehouse[warehouse]
                record={'补货单号':head['number'],'签发修订':1,'门店编码':head['store'],'供货仓':warehouse,
                        '商品编码':sku,'商品名称':'合成验收商品 '+sku,'数量单位':'库存单位','门店确认数量':quantity,
                        '回传单号':'QA-DUPLICATE-RETURN','回传日期':wr.today()}
                ws.append([record.get(field) for field in io.fields('return')])
        book.save(path);book.close()
    conflict=out/'河东冲突_10款.xlsx';other=out/'定陶正常.xlsx'
    workbook(conflict,'河北仓',[(sheet,[(row['E'],quantity) for row in rows[:10]]) for sheet,quantity in [('往期新品',0),('中心仓动销补货清单',7)]])
    workbook(other,'山东仓',[('回传明细',[(sd['E'],3)])])
    marker={'synthetic':True,'live_platform':False,'source_hashes':{p.name:digest(p) for p in (conflict,other)},'heads':heads}
    write(out/'synthetic.json',marker)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--exe',type=Path)
    parser.add_argument('--timeout',type=float,default=150)
    args=parser.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    try:
        seed(out);runs=[]
        for mode in ('exercise','reopen'):
            env={**os.environ,'LOCALAPPDATA':str(out/'localappdata'),'APPDATA':str(out/'roaming'),
                 'REPLENISHMENT_DATA_DIR':str(out/'settings'),'QT_QPA_PLATFORM':'windows','PYTHONUTF8':'1',
                 'PYTHONIOENCODING':'utf-8','PATH':str(Path(os.environ['SystemRoot'])/'System32')+os.pathsep+os.environ['SystemRoot']}
            for key in ('PYTHONHOME','PYTHONPATH','QT_PLUGIN_PATH','REPLENISHMENT_PORTABLE_PATHS'):env.pop(key,None)
            command=([str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')])+['--lifecycle-duplicate-smoke',str(out),mode]
            with (out/(mode+'-process.log')).open('wb') as stream:
                process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=stream,stderr=stream,creationflags=subprocess.CREATE_NO_WINDOW)
                try:code=process.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    # This is the exact process created above with private data.
                    # No process discovery, name match or foreign PID is used.
                    write(out/(mode+'-timeout.json'),{'pid':process.pid,'isolated':True,'command':command})
                    process.terminate();process.wait(15);raise
                finally:
                    if process.poll() is not None and hasattr(process,'_handle'):process._handle.Close()
            report=read(out/(mode+'-report.json'))
            if code!=0 or not report['passed']:raise AssertionError((mode,code,report))
            if report['frozen_exe']!=bool(args.exe):raise AssertionError('Requested runtime does not match actual process')
            if any(digest(out/name)!=sha for name,sha in read(out/'synthetic.json')['source_hashes'].items()):raise AssertionError('Original synthetic workbook changed')
            runs.append(report)
        report={'passed':True,'frozen_exe':bool(args.exe),'exe_sha256':digest(args.exe) if args.exe else None,
                'live_platform':False,'dialogs_not_stubbed':True,'runs':runs,'output':str(out)}
        write(out/'report.json',report);print(json.dumps({'passed':True,'report':str(out/'report.json')},ensure_ascii=False))
    except BaseException:
        write(out/'report.json',{'passed':False,'error':traceback.format_exc(),'live_platform':False})
        raise


if __name__=='__main__':main()
