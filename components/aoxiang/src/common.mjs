import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {parse as parseLossless} from 'lossless-json';

export const APP_VERSION = '0.2.1';
export const SITE = 'https://saas-retail.ele.me';
export const EXPORT_CODE = 'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2';
export class AppError extends Error {
  constructor(code, message, details = {}) { super(message); this.code = code; this.details = details; }
}
export function fail(code, message, details) { throw new AppError(code, message, details); }
export const uid = () => crypto.randomUUID();
export const sha256 = value => crypto.createHash('sha256').update(value).digest('hex');
export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value !== null && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
export const scopeHash = (code,semanticVersion='D13-all-info-2') => sha256(canonical({sourceId:'D13', variant:'all_information', semanticVersion, buyerWarehouseCodeList:[code], allGoods:true, suggestedQuantityPositiveOnly:false, additionalFilters:{}}));
export const businessDate = (date = new Date()) => new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(date);
export const now = () => new Date().toISOString();
export function parseResponse(text) { return parseLossless(text, undefined, number => number); }
export function countValue(v, name = '计数') {
  if (v === null || v === undefined || !/^\d+$/.test(String(v))) fail('INVALID_COUNT', `${name}缺失或无效`);
  const n = Number(v); if (!Number.isSafeInteger(n)) fail('INVALID_COUNT', `${name}超出可安全表示范围`); return n;
}
export function idValue(v, name = '编码') {
  if (typeof v !== 'string' || !v.trim()) fail('INVALID_ID', `${name}必须为非空文本`);
  if (/REPLACE_|^example-/i.test(v)) fail('EXAMPLE_CONFIG', '请先完成实际账号与仓店配置'); return v;
}
export function redact(value) {
  const walk = (v, key = '') => {
    if (/cookie|authorization|password|token|fileUrl|websocket|endpointURL/i.test(key)) return '[已隐藏]';
    if (Array.isArray(v)) return v.map(x => walk(x));
    if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k,x]) => [k,walk(x,k)]));
    if (typeof v !== 'string') return v;
    return v.replace(/(?:https?|wss?):\/\/[^\s"'<>]+/g, '[地址已隐藏]').replace(/\b1\d{10}\b/g, '[手机号已隐藏]');
  }; return walk(value);
}
export async function writeJson(file, value) {
  await fs.mkdir(path.dirname(file), {recursive:true});
  const tmp = file + '.' + uid() + '.tmp';
  const h = await fs.open(tmp, 'wx');
  try { await h.writeFile(JSON.stringify(value,null,2)+'\n','utf8'); await h.sync(); } finally { await h.close(); }
  await fs.rename(tmp,file);
}
export async function readJson(file) { return JSON.parse((await fs.readFile(file,'utf8')).replace(/^\uFEFF/,'')); }
export async function exists(file) { try { await fs.access(file); return true; } catch { return false; } }
export function safeName(s) {
  const clean = String(s).replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').replace(/[. ]+$/g,'').slice(0,75);
  return !clean || /^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)/i.test(clean) ? '_' + clean : clean;
}
export function relativeSafe(value) {
  if (typeof value !== 'string' || !value || value.includes('\\') || path.posix.isAbsolute(value) || /^[A-Za-z]:/.test(value) || value.split('/').some(p => ['','..','.'].includes(p)||/[<>:"|?*\x00-\x1f]/.test(p)||/[. ]$/.test(p)||/^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)/i.test(p))) fail('UNSAFE_PATH','文件相对路径不安全');
  return value;
}
export function sameIdentity(a,b) { return a?.accountId === b?.accountId && a?.merchantCode === b?.merchantCode; }
export const wait = ms => new Promise(resolve => setTimeout(resolve,ms));
export async function bounded(promise,ms,code,message){let timer;try{return await Promise.race([promise,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new AppError(code,message)),ms);})]);}finally{clearTimeout(timer);}}
