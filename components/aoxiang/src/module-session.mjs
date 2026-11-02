import {sameIdentity,fail} from './common.mjs';

// One owned page, fixed identity, replaceable UI adapters. No platform writes here.
export class ModuleSession {
  constructor({page,adapters,timeoutMs=30000,focusSession,onReceipt=()=>{},onImportReceipt=()=>{},onPhase=()=>{}}){
    Object.assign(this,{page,adapters,timeoutMs,focusSession,onReceipt,onImportReceipt,onPhase});
    this.ui=null;this.source=null;this.ready=false;this.identity=null;
  }
  async open(source,expected=this.identity,{force=false}={}){
    if(this.identity&&expected&&!sameIdentity(this.identity,expected))fail('WRONG_IDENTITY','共享连接已绑定另一商家，不能切换身份');
    if(!this.identity&&expected)this.identity=structuredClone(expected);
    if(this.ready&&this.source===source&&!force)return this.ui;
    this.ready=false;
    const old=this.ui;this.ui=null;this.source=null;
    await old?.dispose();
    const Adapter=this.adapters[source];if(!Adapter)fail('UNKNOWN_SOURCE','未知的翱象页面模块');
    const ui=new Adapter(this.page,{timeoutMs:this.timeoutMs,focusSession:this.focusSession});
    this.focusSession=null;this.ui=ui;
    ui.onReceipt=this.onReceipt;ui.onImportReceipt=this.onImportReceipt;
    const fixed=this.identity||expected;
    for(let attempt=0;;attempt++){
      try{
        const identity=await ui.open(fixed);
        if(fixed&&!sameIdentity(fixed,identity))fail('WRONG_IDENTITY','页面身份与共享连接不一致');
        this.identity=structuredClone(identity);this.source=source;this.ready=true;return ui;
      }catch(error){
        // Only the read-only identity/page-open step can retry, once, on this same page.
        // Unknown acceptance and write requests never enter this recovery path.
        if(attempt||error.code!=='IDENTITY_RESPONSE_TIMEOUT'||this.page.isClosed?.())throw error;
        this.onPhase('身份响应超时，正在复用原连接核验一次；不会重新上传或创建导出');
      }
    }
  }
  async dispose(){this.ready=false;const old=this.ui;this.ui=null;this.source=null;await old?.dispose();}
}
