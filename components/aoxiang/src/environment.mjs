import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {exists, fail,AppError} from './common.mjs';
const execFileAsync = promisify(execFile);

export async function powershell(script, extraEnv = {}) {
  const executable = path.join(process.env.SystemRoot || 'C:/Windows','System32/WindowsPowerShell/v1.0/powershell.exe');
  const modulePath=[path.join(process.env.SystemRoot||'C:/Windows','System32/WindowsPowerShell/v1.0/Modules'),path.join(process.env.ProgramFiles||'C:/Program Files','WindowsPowerShell/Modules')].join(';');
  const {stdout} = await execFileAsync(executable, ['-NoProfile','-NonInteractive','-Command',"[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"+script], {windowsHide:true,timeout:20000,env:{...process.env,PSModulePath:modulePath,...extraEnv},maxBuffer:1048576});
  return stdout.trim().replace(/^\uFEFF/,'');
}
export const defaultStateRoot = () => path.join(process.env.LOCALAPPDATA || path.join(os.homedir(),'AppData/Local'),'AoxiangReplenish');
export const defaultEdgeRoot = () => path.join(process.env.LOCALAPPDATA || path.join(os.homedir(),'AppData/Local'),'Microsoft/Edge/User Data');
export async function knownFolders() {
  if (process.platform !== 'win32') return {documents:path.join(os.homedir(),'Documents')};
  return JSON.parse(await powershell("@{documents=[Environment]::GetFolderPath('MyDocuments')}|ConvertTo-Json -Compress"));
}
export async function discover({extraDataRoot} = {}) {
  if (process.platform !== 'win32') fail('UNSUPPORTED_PLATFORM','当前发行仅支持Windows x64');
  const registration = JSON.parse(await powershell("$a=@();foreach($p in @('Registry::HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\msedge.exe','Registry::HKEY_LOCAL_MACHINE\\Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\msedge.exe')){if(Test-Path -LiteralPath $p){$a+= (Get-Item -LiteralPath $p).GetValue('')}};$u=@();foreach($p in @('Registry::HKEY_CURRENT_USER\\Software\\Policies\\Microsoft\\Edge','Registry::HKEY_LOCAL_MACHINE\\Software\\Policies\\Microsoft\\Edge')){if(Test-Path -LiteralPath $p){$x=(Get-ItemProperty -LiteralPath $p -Name UserDataDir -ErrorAction SilentlyContinue).UserDataDir;if($x){$u+=$x}}};@{executables=@($a);dataRoots=@($u)}|ConvertTo-Json -Compress"));
  const executableCandidates = [...new Set([...(registration.executables||[]),
    path.join(process.env['ProgramFiles(x86)']||'C:/Program Files (x86)','Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.ProgramFiles||'C:/Program Files','Microsoft/Edge/Application/msedge.exe'),
    path.join(process.env.LOCALAPPDATA||'','Microsoft/Edge/Application/msedge.exe')].filter(Boolean))];
  const installations=[];
  for(const executable of executableCandidates) if(await exists(executable)) {
    const version=await powershell("(Get-Item -LiteralPath $env:AX_EDGE_CANDIDATE).VersionInfo.ProductVersion", {AX_EDGE_CANDIDATE:executable});
    installations.push({executable,version,channel:'stable'});
  }
  const expand = s => String(s).replace(/\$\{local_app_data\}/gi,process.env.LOCALAPPDATA||'').replace(/\$\{profile\}/gi,os.homedir()).replace(/%([^%]+)%/g,(_,k)=>process.env[k]||'%'+k+'%');
  const roots=[...new Set([defaultEdgeRoot(),...(registration.dataRoots||[]).map(expand),extraDataRoot].filter(Boolean).map(p=>path.resolve(p)))];
  const profiles=[];
  for(const root of roots) {
    if(!await exists(path.join(root,'Local State'))) continue;
    let cache; try { cache=JSON.parse(await fs.readFile(path.join(root,'Local State'),'utf8')).profile?.info_cache; } catch { continue; }
    for(const [directory,info] of Object.entries(cache||{})) {
      if(directory !== path.basename(directory) || /[\/\\]/.test(directory)) continue;
      const actualProfilePath=path.join(root,directory); if(!await exists(actualProfilePath)) continue;
      profiles.push({userDataRoot:root,profileDirectory:directory,displayName:String(info.name||directory),actualProfilePath,portFileExists:await exists(path.join(root,'DevToolsActivePort'))});
    }
  }
  return {platform:process.platform,arch:process.arch,node:process.version,stateRoot:defaultStateRoot(),installations,profiles,...await knownFolders()};
}
export async function discoverEndpoint(root) {
  let lines; try { lines=(await fs.readFile(path.join(root,'DevToolsActivePort'),'utf8')).trim().split(/\r?\n/); }
  catch { fail('WAIT_BROWSER_SETUP','请在选定Edge配置打开edge://inspect/#remote-debugging并允许远程调试'); }
  const port=Number(lines[0]); if(!Number.isInteger(port)||port<1||port>65535||!/^\/devtools\/browser\/[A-Za-z0-9-]+$/.test(lines[1]||'')) fail('INVALID_ENDPOINT','Edge调试发现文件无效，请重新确认浏览器设置');
  await new Promise((resolve,reject)=>{
    const socket=net.createConnection({host:'127.0.0.1',port});
    socket.setTimeout(3000);
    socket.once('connect',()=>{socket.destroy();resolve();});
    const failed=()=>{socket.destroy();reject(new AppError('WAIT_BROWSER_SETUP','当前Edge调试端口未运行，发现文件可能已过期；请重新开启当前实例的远程调试'));};
    socket.once('error',failed);socket.once('timeout',failed);
  });
  return `ws://127.0.0.1:${port}${lines[1]}`;
}
