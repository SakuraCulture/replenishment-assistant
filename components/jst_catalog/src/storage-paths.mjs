// Equivalence is limited to host-verified same-PC storage moves. A generic
// symlink or a different browser/account is never permission to change binding.
import path from 'node:path';
import {canonical} from './common.mjs';

export function relocatedBinding(binding,aliases=[]){
  if(!binding||typeof binding!=='object')return binding;
  const result={...binding};
  for(const key of ['userDataRoot','actualProfilePath']){
    if(typeof result[key]!=='string')continue;
    const input=path.resolve(result[key]);
    for(const entry of aliases){
      if(typeof entry?.from!=='string'||typeof entry?.to!=='string'||!path.isAbsolute(entry.from)||!path.isAbsolute(entry.to))continue;
      const from=path.resolve(entry.from),to=path.resolve(entry.to),relative=path.relative(from,input);
      if(from===path.parse(from).root||to===path.parse(to).root||relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative))continue;
      result[key]=path.join(to,relative);break;
    }
  }
  return result;
}

export function sameBinding(a,b,aliases=[]){
  if(canonical(a)===canonical(b))return true;
  if(!aliases.length)return false;
  return canonical(relocatedBinding(a,aliases))===canonical(relocatedBinding(b,aliases));
}
