"""Opt-in UI wiring check with synthetic identity receipts, never a live account."""
from pathlib import Path
from catalog_sync_ui import Dialog

class OfflineClient:
    def __init__(self,folder):self.folder=Path(folder);self.running=False;self.error=None;self.calls=[];self.accounts={};self.operations={}
    def start(self):self.running=True
    def close(self,timeout=.5):self.running=False;return True
    def request(self,action,**values):
        self.calls.append(action)
        if action=='accounts.auto':
            wh=values['warehouse'];a={'id':'sd' if wh=='山东仓' else 'hb','warehouse':wh,'expectedCompany':'离线验收 '+wh,'expectedCoid':'1' if wh=='山东仓' else '2','expectedUid':'9','mappingVerified':True,'bindingVersion':1,'enabled':True,'binding':{'actualProfilePath':str(self.folder/wh/'Default')}}
            self.accounts[wh]=a;ident=str(len(self.operations)+1);self.operations[ident]={'state':'SUCCEEDED','result':{'account':a}}
            return {'operation_id':ident}
        if action in {'sync.status','session.health'}:
            return {'running':False,'active':None,'accounts':list(self.accounts.values()),'operations':self.operations,'connections':[],'batches':[]}
        if action in {'session.release','sync.stop'}:return {'idle':True}
        raise AssertionError('Unexpected action in offline auto configuration: '+action)

def verify(win,out,checks):
    owner=win.catalog_sessions.session
    if owner.client is not None:raise AssertionError('QA requires an unused catalog session')
    original=owner.factory;owner.factory=OfflineClient;dialog=None
    try:
        dialog=Dialog(win);dialog.show()
        assert dialog.manual.isHidden() and not dialog.fetch.isEnabled()
        for wh in ['山东仓','河北仓']:
            dialog.warehouse.setCurrentText(wh);dialog.auto_btn.click()
            assert dialog.account_data()['warehouse']==wh and dialog.fetch.isEnabled()
            assert not dialog.mapping.isChecked()
        assert set(owner.client.calls)<={'accounts.auto','sync.status','session.health'}
        dialog.grab().save(str(out/'04-聚水潭自动配置.png'))
        checks.append('实际界面自动配置两仓：无需选择目录、勾选或再次保存；身份回执为离线模拟')
    finally:
        if dialog:dialog.reject();dialog.deleteLater()
        owner.close();owner.factory=original
