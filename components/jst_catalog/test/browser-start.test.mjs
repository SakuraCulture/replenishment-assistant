import test from 'node:test';
import assert from 'node:assert/strict';
import {dedicatedLaunchArgs} from '../src/accounts.mjs';

test('restart keeps selected dedicated profile and exposes only an ephemeral loopback endpoint',()=>{
 const args=dedicatedLaunchArgs({backend:'jst_dedicated',userDataRoot:'C:/dedicated/hb/User Data',profileDirectory:'Default'});
 assert.deepEqual(args,['--user-data-dir=C:/dedicated/hb/User Data','--profile-directory=Default',
  '--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--no-first-run','--no-default-browser-check','about:blank']);
 assert(!args.some(x=>/disable.*security|no-sandbox|disable-blink|enable-automation/.test(x)));
});
test('ordinary browser bindings cannot use dedicated startup',()=>{
 assert.throws(()=>dedicatedLaunchArgs({backend:'existing_edge',userDataRoot:'C:/normal',profileDirectory:'Default'}),{code:'BROWSER_ISOLATION'});
});
