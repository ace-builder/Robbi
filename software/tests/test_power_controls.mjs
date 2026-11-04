import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const state={},calls=[],notices=[],events=[];
let accept=true,reply=async()=>({message:'Linux accepted shutdown.'});
globalThis.confirm=()=>accept;
globalThis.document={dispatchEvent:event=>events.push(event.type)};
globalThis.powerTest={state,api:(...args)=>{calls.push(args);return reply()},notify:(...args)=>notices.push(args)};
const source=(await readFile(new URL('../web/power-controls.js',import.meta.url),'utf8'))
  .replace("import {api,notify,state} from './core.js';",'const {api,notify,state}=globalThis.powerTest;');
const {requestHostOperation}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const button=()=>({dataset:{},disabled:false,textContent:'Shut down Pi'});

accept=false;
await requestHostOperation('shutdown',button());
assert.equal(calls.length,0,'Cancelling confirmation must not send a power request');
accept=true;
await requestHostOperation('shutdown',button());
assert.equal(calls[0][0],'/shutdown');
assert.equal(state.hostOperation,'shutdown');
assert.deepEqual(events,['robbi:stop']);
assert.equal(notices[0][0],'Linux accepted shutdown.');

delete state.hostOperation;calls.length=0;notices.length=0;events.length=0;
reply=async()=>{throw new Error('Linux rejected the power request.')};
const failed=button();
await requestHostOperation('shutdown',failed);
assert.equal(state.hostOperation,undefined,'Failure must not mark the Pi as shutting down');
assert.deepEqual(notices,[['Linux rejected the power request.',true]]);
assert.equal(events.length,0);
assert.equal(failed.disabled,false);
assert.equal(failed.textContent,'Shut down Pi');

calls.length=0;
let finish;
reply=()=>new Promise(resolve=>finish=resolve);
const pending=button();
const first=requestHostOperation('restart',pending);
await requestHostOperation('restart',pending);
assert.equal(calls.length,1,'Double-click must not submit twice');
assert.equal(calls[0][0],'/restart_service');
finish({message:'Linux accepted restart.'});await first;
assert.equal(state.hostOperation,'restart');
assert.equal(pending.disabled,false);
console.log('Power UI checks passed: cancellation, success, failure, recovery and duplicate submission.');
