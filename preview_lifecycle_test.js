const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const timers = new Map(), requests = [], urls = new Set();
let nextTimer = 0, nextUrl = 0;
const image = {src: '', removeAttribute() { this.src = ''; }, decode: async () => {}};
let decode = async () => {};
const context = {
  Image: class {decode() {return decode();}},
  AbortController, URLSearchParams, Date,
  URL: {createObjectURL() {const u = `blob:${++nextUrl}`; urls.add(u); return u;}, revokeObjectURL(u) {urls.delete(u);}},
  setTimeout(fn, ms) {const id = ++nextTimer; timers.set(id, {fn, ms}); return id;},
  clearTimeout(id) {timers.delete(id);},
  fetch(url, options) {return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/latest_preview.js', 'utf8'), context);
const tick = async () => {for (let i=0; i<12; i++) await Promise.resolve();};
const response = (seq, status=200) => ({ok:true, status, blob:async()=>({}), headers:{get:()=>seq}});
const runNext = () => {const [id, task] = [...timers].find(([,t])=>t.ms<5000); timers.delete(id); task.fn();};
(async () => {
  const p = new context.LatestPreview(image, '/preview');
  p.configure(true, 'raw');
  for(let i=0;i<20;i++) p.configure(true, 'raw');
  assert.equal(requests.length, 1, 'Repeated status refresh cannot duplicate requests');
  requests[0].resolve(response('raw:1')); await tick();
  assert.equal(urls.size, 1);
  runNext(); assert.match(requests[1].url, /after=raw%3A1/);
  p.configure(true, 'result');
  assert(requests[1].options.signal.aborted);
  assert.equal(requests.length, 2, 'New mode waits until old transfer settles');
  assert.equal(urls.size, 0);
  requests[1].resolve(response('old')); await tick();
  assert.equal(image.src, '', 'Late old response cannot repaint');
  runNext(); assert.match(requests[2].url, /mode=result/);
  requests[2].resolve(response('result:1')); await tick();
  assert.equal(urls.size, 1);
  runNext(); requests[3].resolve(response('', 204)); await tick();
  assert.equal(urls.size, 1, 'No-change response retains the displayed frame');
  // Abort while image decoding is stalled, then resume without a parallel request.
  decode = () => new Promise(()=>{});
  const displayed = image.src;
  runNext(); requests[4].resolve(response('result:2')); await tick();
  assert.equal(image.src, displayed, 'Pending decode must not change the displayed frame');
  assert.equal(urls.size, 2, 'At most displayed and pending frame URLs');
  p.configure(false, 'result'); await tick();
  assert.equal(urls.size, 0); assert.equal(timers.size, 0); assert.equal(p.busy, false);
  decode = async()=>{};
  p.configure(true, 'result');
  requests[5].reject(new Error('network')); await tick();
  assert.equal(urls.size, 0); assert([...timers.values()].some(x=>x.ms<=1000));
  p.stop(); assert.equal(timers.size, 0);
  const startIndex=requests.length;
  p.configure(true,'raw');
  for(let i=0;i<1000;i++){
    requests[startIndex+i].resolve(response(`raw:${i}`));await tick();
    assert.equal(urls.size,1,'Only one displayed object URL remains after each frame');
    assert.equal(timers.size,1,'Only one next-frame timer remains');
    if(i<999)runNext();
  }
  const goodFrame=image.src;
  runNext();requests.at(-1).reject(new Error('transient network'));await tick();
  assert.equal(image.src,goodFrame,'Network error must preserve the last good frame');
  assert.equal(urls.size,1);
  decode=async()=>{throw new Error('bad jpeg')};
  runNext();requests.at(-1).resolve(response('bad'));await tick();
  assert.equal(image.src,goodFrame,'Decode error must preserve the last good frame');
  assert.equal(urls.size,1,'Failed pending frame URL must be released');
  let finishDecode;
  decode=()=>new Promise(resolve=>{finishDecode=resolve});
  runNext();requests.at(-1).resolve(response('recovered'));await tick();
  assert.equal(image.src,goodFrame);
  finishDecode();await tick();
  assert.notEqual(image.src,goodFrame,'Successful decoded frame replaces the old frame');
  assert.equal(urls.size,1);
  p.stop();assert.equal(urls.size,0);assert.equal(timers.size,0);
  console.log('PREVIEW_LIFECYCLE PASS: single request, revision polling, mode cancellation, late response, URL cleanup, decode cancellation, retry');
})().catch(e=>{console.error(e);process.exitCode=1;});
