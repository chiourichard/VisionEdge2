// Exercise the workspace's async transaction lifecycle without camera/browser dependencies.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{style:{},dataset:{},textContent:'',disabled:false,addEventListener(){}});return elements.get(id)};
let saved={version:'v1',regions:[{id:1,label:'A',x:10,y:20,w:30,h:40,threshold:.87,search_margin:8,capture_group_id:1,template_b64:'crop'}],captures:[{id:1,label:'Frame A',thumb_b64:'frameA'}],image_b64:'reference'};
let renders=0,puts=0,gets=0,fail=false;
element('app').inert=false;
const ctx={console,URLSearchParams,location:{search:'?product_id=1'},setTimeout,clearTimeout,
 VisionEdgeNotice:{show(){}},
 document:{getElementById:element,querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener(){}},
 S:{selectedPid:1,editMode:false,regions:[],captures:[],products:[{id:1,serial:'P1'}]},DC:{style:{}},
 cleanSampleHint:x=>x||'OK',setStatus:m=>element('status').textContent=m,
 loadProducts:async()=>{},renderProductList(){},resetAll(){ctx.S.regions=[];ctx.S.captures=[]},
 displayImg:async()=>{await Promise.resolve();renders++},renderCaptureStrip(){},renderTable(){},drawOverlay(){},applyEditModeUI(){},labelControls(){},
 fetchRegions(){},selectProduct(){},toggleEditMode(){},cancelEdit(){},saveRegions(){},openAddProduct(){},pinCurrentFrame(){},
 fetch:async(url,opts={})=>{
  if(opts.method==='PUT'){puts++;if(fail)return {ok:false,json:async()=>({error:'conflict'})};const p=JSON.parse(opts.body);assert.equal(p.version,saved.version);assert.equal(p.image_b64,undefined,'Metadata edits must not reupload the reference frame');saved={...saved,regions:p.regions,version:'v2'}}else gets++;
  return {ok:true,json:async()=>structuredClone(saved)};
 }};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(__dirname+'/static/workspace.js','utf8'),ctx);
(async()=>{
 await ctx.fetchRegions(1);assert.equal(ctx.S.regions[0].label,'A');assert.equal(ctx.S.srcB64,'data:image/jpeg;base64,frameA');
 await ctx.toggleEditMode();assert.equal(ctx.S.editMode,true);assert.equal(element('app').inert,false);assert.equal(element('workspaceTestBtn').disabled,true);assert.equal(gets,1,'Entering edit reuses loaded workspace');
 ctx.S.regions[0].label='Discard me';assert.equal(ctx.workspacePending(),true);
 vm.runInContext('workspaceConfirm=async()=>false',ctx);await ctx.cancelEdit();assert.equal(ctx.S.regions[0].label,'Discard me');
 vm.runInContext('workspaceConfirm=async()=>true',ctx);await ctx.cancelEdit();assert.equal(ctx.S.editMode,false);assert.equal(ctx.S.regions[0].label,'A');assert.equal(ctx.S.selectedPid,1);assert.equal(gets,1,'Cancel restores the local snapshot');
 await ctx.toggleEditMode();ctx.S.regions[0].threshold=0;ctx.S.regions[0].label='New name';fail=true;await ctx.saveRegions();assert.equal(ctx.S.editMode,true);assert.equal(ctx.S.regions[0].threshold,0);assert.equal(element('app').inert,false);
 fail=false;await ctx.saveRegions();assert.equal(ctx.S.editMode,false);assert.equal(ctx.S.regions[0].threshold,0);assert.equal(ctx.S.regions[0].label,'New name');assert.equal(element('workspaceTestBtn').disabled,false);assert.equal(puts,2);assert.equal(gets,2,'Save reloads server-assigned IDs');assert(renders>=3);
 console.log('WORKSPACE_LIFECYCLE PASS: saved-frame edit, cancel/stay/discard, failed-save retention, successful-save reload, zero threshold, UI unlock');
})().catch(e=>{console.error(e);process.exitCode=1});
