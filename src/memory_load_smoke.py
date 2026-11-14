"""Opt-in isolated EXE acceptance for response release and lazy history restore."""
import faulthandler,hashlib,json,os,subprocess,sys,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QComboBox,QMessageBox
from perf_core_smoke import typed
from version_info import diagnostic


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();fixture=json.loads((out/'fixture.json').read_text('utf-8'))
    faulthandler.dump_traceback_later(20,repeat=True)
    assert fixture['synthetic'] and not fixture['live_platform']
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    state={'mode':None,'seen':set()};checks=[];shots=[]
    expected=json.loads((out/'expected.json').read_text('utf-8'))
    def shot(widget,name):
        assert widget.grab().save(str(out/name));shots.append(name)
    def handle():
        dialog=QApplication.activeModalWidget()
        if (out/'stop.request').exists():
            if dialog is not None:dialog.reject()
            return
        if dialog is None or id(dialog) in state['seen']:return
        state['seen'].add(id(dialog))
        try:
            combo=dialog.findChild(QComboBox)
            if combo is not None:
                assert [combo.itemText(i) for i in range(combo.count())]==expected['history_labels']
                assert all('items' not in combo.itemData(i) for i in range(combo.count()))
                combo.setCurrentIndex(expected['selected_index']);state['entry']=combo.currentData()
                shot(dialog,state['mode']+'-directory.png')
                if state['mode']=='cancel-directory':dialog.reject();return
                if state['mode']=='changed':
                    p=Path(state['entry']['file']);state['backup']=(p,p.read_bytes(),p.stat())
                    p.write_bytes(state['backup'][1]+b' ')
                dialog.accept()
            elif isinstance(dialog,QMessageBox):
                shot(dialog,state['mode']+'-message.png')
                if state['mode']=='changed':
                    assert '历史仓资料未恢复' in dialog.text() and '变化' in dialog.text();dialog.accept()
                else:
                    assert '确认更新并保存仓资料' in dialog.windowTitle(),dialog.windowTitle()
                    answer=QMessageBox.StandardButton.Cancel if state['mode']=='cancel-confirm' else QMessageBox.StandardButton.Yes
                    dialog.button(answer).click()
            else:raise AssertionError('Unexpected modal: '+dialog.windowTitle())
        except Exception:
            state['error']=traceback.format_exc();dialog.reject()
    timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(handle);timer.start()
    def exercise():
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'live_platform':False,'checks':checks,'screenshots':shots}
        try:
            from history_read import Index
            index=Index(out/'cache.sqlite3')
            assert typed([index.get('key',False),index.get('key',True)])==expected['cache']
            record=json.loads((out/'record.json').read_text('utf-8'));view=json.loads((out/'view.json').read_text('utf-8'))
            index.put('key',record,view)
            with closing(index.connect()) as db:
                columns=db.execute('SELECT summary,search,projection,checksum FROM views WHERE k=?',('key',)).fetchone()
                assert [hashlib.sha256(v.encode()).hexdigest() for v in columns]==expected['stored_columns_sha256']
                db.execute('UPDATE views SET projection=? WHERE k=?',('corrupted-derived-only','key'));db.commit()
            assert index.get('key',False) is None and index.get('key',True) is None
            checks.append('Actual cache reads old bytes, writes identical checksum, checks unrequested full projection corruption')
            assert win.concurrency.value()==2
            checks.append('Default replenishment calculation concurrency remains two')
            initial=win.catalog_repository.catalogs()
            for mode in ('cancel-directory','changed','cancel-confirm','restore'):
                state.update(mode=mode,seen=set());state.pop('error',None)
                (out/'state.json').write_text(json.dumps({'mode':mode,'stage':'before_dialog'}),'utf-8')
                win.restore_catalog()
                if state.get('backup'):
                    p,raw,stamp=state.pop('backup');p.write_bytes(raw);os.utime(p,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
                assert not state.get('error'),state.get('error')
                current=win.catalog_repository.catalogs()
                if mode!='restore':assert current==initial
                else:
                    selected=expected['selected_raw'];assert typed(current[selected['warehouse']])==expected['selected']
                    assert typed(win.catalogs[selected['warehouse']])==expected['selected']
                checks.append('Real history restore '+mode+': exact directory order, metadata-only selection, correct persisted result')
            if getattr(sys,'frozen',False):
                from catalog_sync_client import components
                from catalog_sync_bundle import verify_component
                jst,ax=components();verify_component(jst,ax)
                node=ax/'runtime/node.exe';probe=out/'verify_memory_response.cjs'
                with (out/'bundled-node.log').open('w',encoding='utf-8') as log:
                    process=subprocess.Popen([str(node),'--expose-gc',str(probe),'--source',str(jst),'--bridge',str(ax),
                        '--out',str(out/'bundled-response'),'--count','5','--bytes',str(2*1024*1024)],stdout=log,stderr=log,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    code=process.wait(90)
                node_report=json.loads((out/'bundled-response/report.json').read_text('utf-8'))
                assert code==0 and node_report['passed'] and node_report['retained_body_bytes']==0,node_report
                report['bundled_node_sha256']=hashlib.sha256(node.read_bytes()).hexdigest();report['bundled_node']=node_report['node']
                checks.append('Actual packaged Node, Playwright and JST receiveBoundFile release five local responses without browser or platform')
            report['passed']=True;shot(win,'completed.png')
        except Exception:report['error']=traceback.format_exc()
        finally:
            faulthandler.cancel_dump_traceback_later()
            timer.stop();(out/'ui-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win.job_dirty=False;win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(350,exercise)
