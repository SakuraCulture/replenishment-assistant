"""7,000 store/SKU rows, native UI acceptance and three isolated scale runs."""
import copy,json,time,statistics
from pathlib import Path


def fixture():
    return {f'BULK{i:03}':{'name':f'合成门店{i:03}','qualification':{'state':'无资质'},'refusals':{f'{j:06}':{'name':f'合成商品{j}','reason':'合成拒收','active':True,'source':{'original':{'门店名称':f'合成门店{i:03}'}}} for j in range(100)}} for i in range(70)}


def verify(app,win,out,report,drive,require):
    import admission,admission_overview as service
    from admission_overview_ui import Overview
    from work_repository import Repository
    from PySide6.QtCore import Qt,QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QTableView
    vals=fixture();admission.save(win.work_repository.path,vals,{s:{} for s in vals})
    d=Overview(win,win,'refusals');started=time.perf_counter();d.reload();d.show();app.processEvents();report['bulk_ui_query_seconds']=time.perf_counter()-started
    try:
        d.search.setText('BULK');d.filter();require(len(d.filtered)==7000,'7000条店-SKU按门店商品展示');started=time.perf_counter();d.select_all(True);report['bulk_select_seconds']=time.perf_counter()-started
        require(len(d.model.keys())==7000,'一键勾选当前筛选7000条')
        index=d.model.index(0,0);rect=d.table.visualRect(index);QTest.mouseClick(d.table.viewport(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,QPoint(rect.left()+10,rect.center().y()))
        require(len(d.model.keys())==6999 and ('BULK000','000000') not in d.model.checked,'实际点击复选框仅取消该店该SKU');d.select_all(True);d.grab().save(str(out/'07-批量勾选7000条.png'))
        def preview(cancel):
            def handler(dialog):
                if dialog.objectName()=='interactionReview':
                    require(dialog.findChild(QTableView).model().rowCount()==7000,'批量核对完整显示7000条，不截断');dialog.grab().save(str(out/'08-批量解除核对.png'));dialog.reject() if cancel else dialog.accept()
            return handler
        drive(d.release_checked,preview(True));require(admission.snapshot(win.work_repository.path,'BULK000')['record']==vals['BULK000'],'取消批量核对未解除')
        d.search.setText('BULK001');d.filter();require(not d.model.keys(),'改变筛选清空旧勾选');d.select_all(True);require(len(d.model.keys())==100,'筛选单店只勾选该店100条')
        d.search.setText('BULK');d.filter();d.select_all(True);started=time.perf_counter();drive(d.release_checked,preview(False));report['bulk_ui_confirm_save_refresh_seconds']=time.perf_counter()-started
        require(not d.filtered,'批量解除后有效拒收筛选为空');d.state.setCurrentText('已解除');require(len(d.filtered)==7000,'7000条全部可在已解除中核对')
        for s,before in vals.items():
            expected=copy.deepcopy(before)
            for r in expected['refusals'].values():r['active']=False
            if admission.snapshot(win.work_repository.path,s)['record']!=expected:raise AssertionError('其他字段变化：'+s)
        require(True,'70店全部核对：仅active改变，资质、原因和来源保留')
        require(admission.snapshot(win.work_repository.path,'NEW')['record']['refusals']['000099']['active'],'未勾选的其他店拒收未解除')
        d.grab().save(str(out/'09-批量解除完成.png'))
    finally:d.close()
    raw=[]
    for i in range(3):
        repo=Repository(out/f'bulk-benchmark-{i}');admission.save(repo.path,vals,{s:{} for s in vals})
        t=time.perf_counter();data=service.load(repo.path);read=time.perf_counter()-t
        t=time.perf_counter();p=service.prepare_release(data,[(r[0],r[2]) for r in data['refusals']]);prepare=time.perf_counter()-t
        t=time.perf_counter();service.commit_release(repo.path,p);save=time.perf_counter()-t
        require(all(r[6]=='已解除' for r in service.load(repo.path)['refusals']),f'规模测试第{i+1}轮7000条保存成功')
        raw.append({'query':read,'prepare':prepare,'save':save})
    report['bulk_benchmark']={'synthetic':True,'stores':70,'store_sku_rows':7000,'seconds':raw,'summary':{k:{'median':statistics.median(r[k] for r in raw),'min':min(r[k] for r in raw),'max':max(r[k] for r in raw)} for k in raw[0]},'scope':'本机暖文件缓存；逐轮独立合成SQLite库，保存包含原备份/版本复核/事件/事务；不代表真实大库备份耗时'}
