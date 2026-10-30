"""Architecture guards for the local 0.6.1 refactor (no user configuration)."""
import ast,hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))


def verify(baseline="e50072cc00c6997aed9fe1113012192210ad7eb3"):
    import app,engine,batch_service,xlsx_exports,result_semantics,runtime_support
    from batch_app import BatchWindow
    from workbench import Workbench
    from ui_session import SessionWindow
    from legacy_window import MainWindow
    assert MainWindow not in BatchWindow.__mro__ and MainWindow not in Workbench.__mro__
    assert SessionWindow in Workbench.__mro__ and app.MainWindow is MainWindow
    assert engine.Cancelled is runtime_support.Cancelled
    assert engine.export_xlsx is xlsx_exports.export_xlsx
    assert batch_service.export_batch is xlsx_exports.export_batch
    assert batch_service.write_sheet is xlsx_exports.write_sheet
    for name in ['numeric','fmt','issue_text','exclusion_reason','order_quantity','order_amount','orderable','order_status','summarize','quantity','amount_value','coverage','explanation','routing']:
        assert getattr(engine,name) is getattr(result_semantics,name),name
    reverse=[]
    for path in (ROOT/'src').glob('*.py'):
        if path.name in {'app.py','smoke.py'}:continue
        for n in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(n,ast.ImportFrom) and n.module=='app':reverse.append(path.name)
            if isinstance(n,ast.Import) and any(a.name=='app' for a in n.names):reverse.append(path.name)
    assert not reverse,reverse
    core={}
    for name in ['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json']:
        old=subprocess.check_output(['git','show',baseline+':src/'+name],cwd=ROOT)
        new=(ROOT/'src'/name).read_bytes();assert old==new,name
        core[name]=hashlib.sha256(new).hexdigest()
    return {'passed':True,'core_sha256':core,'checks':['active windows do not inherit legacy MainWindow','no UI reverse imports of app','result/export compatibility aliases have identical object identity','single cancellation exception identity','core rule and default bytes unchanged']}


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--baseline',default='e50072cc00c6997aed9fe1113012192210ad7eb3');a=p.parse_args()
    print(json.dumps(verify(a.baseline),ensure_ascii=False,indent=2))
