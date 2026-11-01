import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';
import {chromium} from 'playwright-core';
import {wait,exists} from '../src/common.mjs';
import {discoverEndpoint} from '../src/environment.mjs';
export async function isolatedEdge(){
  const executable=path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe');
  if(!await exists(executable))throw Error('隔离Edge夹具需要本机Stable安装');
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-isolated-edge-'));
  const child=spawn(executable,['--headless=new','--remote-debugging-port=0','--user-data-dir='+root,'--no-first-run','--no-default-browser-check','about:blank'],{windowsHide:true,stdio:'ignore'});
  let endpoint;for(let i=0;i<150;i++){try{endpoint=await discoverEndpoint(root);break;}catch{}await wait(100);}
  if(!endpoint){child.kill();throw Error('隔离Edge调试端口未就绪');}
  const browser=await chromium.connectOverCDP(endpoint,{noDefaults:true});
  return {browser,root,executable,async close(){try{const c=await browser.newBrowserCDPSession();await c.send('Browser.close');}catch{}await wait(200);if(child.exitCode===null)child.kill();}};
}
