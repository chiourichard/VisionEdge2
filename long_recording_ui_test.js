const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/static/edge_dashboard.html','utf8');
for(const m of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))new vm.Script(m[1]);
const elements=new Map();
function element(id){if(!elements.has(id))elements.set(id,{value:'',checked:false,textContent:'',disabled:false,classList:{toggle(){}}});return elements.get(id)}
let config={width:1920,height:1080,recording_bitrate:2500000,recording_fps:15,recording_max_height:1080,recording_segment_seconds:600},saved=null;
const errors=[];let previewStops=0,previewRestarts=0;
const ctx={$:element,A:'/api/edge',lastStatus:{recording:false,running:false},settingsBusy:false,settingsDirty:false,settingsBaseline:'',
 preview:{stop(){previewStops++}},reloadStream(){previewRestarts++},ensureOption:(e,v)=>e.value=String(v),fmtGb:String,message:(m,type)=>{if(type==='err')errors.push(m)},pollOnce:async()=>{},
 req:async(url,options)=>{if(options){saved=JSON.parse(options.body);config={...config,...saved};return {success:true}}return {config}},console};
vm.createContext(ctx);
const from=html.indexOf('function updateBackendFields()'),to=html.indexOf('async function pollOnce()',from);
vm.runInContext(html.slice(from,to),ctx);
(async()=>{
 await ctx.loadConfig();assert.equal(element('recording_segment_seconds').value,'600');
 for(const [key,value] of Object.entries({recording_fps:'10',recording_max_height:'720',recording_segment_seconds:'300',recording_bitrate:'3000000'}))element(key).value=value;
 ctx.markSettingsDirty();assert(ctx.settingsDirty);
 ctx.lastStatus.recording=true;await ctx.applySettings();assert.equal(saved,null,'recording settings must be locked');
 ctx.lastStatus.recording=false;await ctx.applySettings();
 assert.equal(saved.recording_fps,10);assert.equal(saved.recording_max_height,720);assert.equal(saved.recording_segment_seconds,300);assert.equal(saved.recording_bitrate,3000000);
 assert(!ctx.settingsDirty);assert.deepEqual(errors,[]);assert.equal(previewStops,1);assert.equal(previewRestarts,1);
 console.log('LONG_RECORDING_UI PASS: syntax, config load/save, dirty state, recording guard');
})().catch(e=>{console.error(e);process.exitCode=1});
