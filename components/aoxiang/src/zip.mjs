import fs from 'node:fs/promises';
import path from 'node:path';
import {crc32} from 'node:zlib';
import yauzl from 'yauzl';
import yazl from 'yazl';
import {pipeline} from 'node:stream/promises';
import {createWriteStream} from 'node:fs';
import {relativeSafe,fail} from './common.mjs';

export async function openArchive(file,{maxUncompressedBytes=4294967296,maxEntries=20000}={}){
  const zip=await new Promise((resolve,reject)=>yauzl.open(file,{lazyEntries:true,autoClose:false,strictFileNames:true},(e,z)=>e?reject(e):resolve(z)));
  const entries=new Map();let total=0;
  try{
    await new Promise((resolve,reject)=>{
      zip.once('error',reject);zip.on('end',resolve);zip.on('entry',entry=>{
        try{
          const name=entry.fileName;
          if(name.endsWith('/')){relativeSafe(name.slice(0,-1));zip.readEntry();return;}
          relativeSafe(name);
          if(((entry.externalFileAttributes>>>16)&0xf000)===0xa000)fail('ZIP_LINK','压缩包不允许符号链接');
          if(entries.has(name))fail('ZIP_DUPLICATE','压缩包存在重复文件名');
          total+=entry.uncompressedSize;
          if(total>maxUncompressedBytes||entries.size>=maxEntries)fail('ZIP_RESOURCE_LIMIT','压缩包超过本机工程保护值');
          entries.set(name,entry);zip.readEntry();
        }catch(e){reject(e);}
      });zip.readEntry();
    });
  }catch(e){zip.close();throw e;}
  const read=async(name,onChunk)=>{
    const entry=entries.get(name);if(!entry)fail('ZIP_MEMBER_MISSING',`缺少压缩包成员：${name}`);
    const stream=await new Promise((resolve,reject)=>zip.openReadStream(entry,(e,s)=>e?reject(e):resolve(s)));
    let bytes=0,crc=0;
    for await(const chunk of stream){bytes+=chunk.length;crc=crc32(chunk,crc);await onChunk(chunk);}
    if(bytes!==entry.uncompressedSize||(crc>>>0)!==(entry.crc32>>>0))fail('ZIP_CRC_FAILED',`压缩包成员校验失败：${name}`);
    return bytes;
  };
  return {entries,read,
    buffer:async(name,limit=67108864)=>{const parts=[];let bytes=0;await read(name,chunk=>{bytes+=chunk.length;if(bytes>limit)fail('ZIP_MEMBER_TOO_LARGE','压缩成员超过内存保护值');parts.push(chunk);});return Buffer.concat(parts);},
    validateAll:async()=>{for(const name of entries.keys())await read(name,()=>{});},
    close:()=>zip.close()};
}
export async function createArchive(file,members){
  const zip=new yazl.ZipFile();
  for(const member of members){relativeSafe(member.relativePath);if(member.buffer!==undefined)zip.addBuffer(Buffer.from(member.buffer),member.relativePath);else zip.addFile(member.file,member.relativePath);}
  await fs.mkdir(path.dirname(file),{recursive:true});
  const saved=pipeline(zip.outputStream,createWriteStream(file,{flags:'wx'}));zip.end();await saved;
}
