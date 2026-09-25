let workspacePreview=null;
function syncWorkspacePreview(){
 const image=document.getElementById('studioLive');
 if(!workspacePreview)workspacePreview=new LatestPreview(image,'/api/edge/preview.jpg');
 workspacePreview.configure(document.getElementById('cameraDialog').open&&!image.hidden&&!document.hidden,'raw');
}
document.addEventListener('visibilitychange',()=>{if(workspacePreview)syncWorkspacePreview()});
window.addEventListener('pagehide',()=>workspacePreview?.stop());
window.addEventListener('pageshow',()=>{if(workspacePreview)syncWorkspacePreview()});
document.getElementById('cameraDialog').addEventListener('close',()=>workspacePreview?.stop());
// The old single-workspace editor keeps ownership of drawing, frames and selection.
let workspaceVersion='',workspaceBaseline='',workspaceBusy=false,workspaceLeaving=false,workspaceSaved=null;
const asImage=s=>!s?'':s.startsWith('data:')?s:'data:image/jpeg;base64,'+s;
async function studioRequest(url,options={}){const r=await fetch(url,{cache:'no-store',...options,headers:{'Content-Type':'application/json',...options.headers}});const d=await r.json();if(!r.ok||d.ok===false||d.success===false)throw Error(d.error||'操作失敗');return d;}
function toast(message,kind='success'){setStatus(message);VisionEdgeNotice.show(message,kind)}
function workspacePayload(){
 const new_capture_groups={};
 const regions=S.regions.map(r=>{
  const out={id:r.id,label:r.label,x:r.x,y:r.y,w:r.w,h:r.h,threshold:r.threshold,search_margin:r.search_margin??0,sample_hint:cleanSampleHint(r.sample_hint)};
  if(r._srcB64)out.source_image_b64=r._srcB64;
  if(typeof r._captureId==='string'&&r._captureId.startsWith('cap_')){
   out.capture_group_temp_key=r._captureId;const c=S.captures.find(c=>c.id===r._captureId);
   if(c)new_capture_groups[c.id]={label:c.ts,thumb_b64:c.b64};
  }else if(r._captureId)out.capture_group_id=r._captureId;
  return out;
 });
 return {regions,new_capture_groups,clear_reference:!!S.clearTemplate};
}
function workspacePending(){return S.editMode&&JSON.stringify(workspacePayload())!==workspaceBaseline;}
function workspaceDirtyUI(){
 document.getElementById('modeLabel').textContent=S.editMode?(workspacePending()?'編輯中 · 尚未儲存':'編輯模式'):'瀏覽模式';
 for(const id of ['workspaceTestBtn','workspaceApplyBtn','imageTestBtn'])document.getElementById(id).disabled=S.editMode||!S.selectedPid;
}
function workspaceLock(b){workspaceBusy=b;document.getElementById('app').inert=b;document.querySelectorAll('#cameraDialog button,#cameraDialog select,#cameraDialog input').forEach(e=>{if(b){e.dataset.wasDisabled=String(e.disabled);e.disabled=true}else{e.disabled=e.dataset.wasDisabled==='true'}})}
async function workspaceInit(){
 try{await loadProducts();const pid=Number(new URLSearchParams(location.search).get('product_id'));const p=S.products.find(p=>p.id===pid)||S.products[0];if(p)await selectProduct(p);else applyEditModeUI();}
 catch(e){toast(e.message,'error')}
 document.getElementById('regionBody').addEventListener('input',workspaceDirtyUI);
}
fetchRegions=async function(pid){
 const d=await studioRequest(`/api/products/${pid}/workspace?compact=1`);workspaceVersion=d.version;
 S.clearTemplate=false;
 S.regions=d.regions.map(r=>({...r,_captureId:r.capture_group_id||null,_thumbB64:asImage(r.template_b64),_sel:false}));
 S.captures=d.captures.map(g=>({id:g.id,b64:asImage(g.thumb_b64),ts:g.label||String(g.id)}));
 S.activeCaptureId=null;S.activeCaptureB64=null;S.srcB64=asImage(d.image_b64);
 if(S.captures.length){const c=S.captures[0];S.activeCaptureId=c.id;S.activeCaptureB64=c.b64;S.srcB64=c.b64;}
 if(S.srcB64)await displayImg(S.srcB64);
 workspaceBaseline=JSON.stringify(workspacePayload());renderCaptureStrip();renderTable();drawOverlay();workspaceDirtyUI();
};
selectProduct=async function(p){
 if(workspaceBusy||p.id===S.selectedPid)return;
 if(workspacePending()&&!await workspaceConfirm('尚有未儲存的修改，確定離開？'))return;
 workspaceLock(true);
 try{resetAll();workspaceSaved=null;S.selectedPid=p.id;S.editMode=false;await fetchRegions(p.id);renderProductList();applyEditModeUI();setStatus(p.serial);}
 catch(e){S.selectedPid=null;toast(e.message,'error')}finally{workspaceLock(false)}
};
toggleEditMode=async function(){
 if(workspaceBusy)return;
 if(!S.selectedPid){toast('請先建立或選擇產品','warning');return;}
 if(S.editMode){await cancelEdit();return;}
 workspaceSaved={regions:S.regions.map(r=>({...r})),captures:S.captures.map(c=>({...c})),srcB64:S.srcB64,activeCaptureId:S.activeCaptureId,activeCaptureB64:S.activeCaptureB64};
 S.editMode=true;workspaceBaseline=JSON.stringify(workspacePayload());applyEditModeUI();
};
cancelEdit=async function(){
 if(workspacePending()&&!await workspaceConfirm('放棄尚未儲存的修改並重新載入？'))return;
 workspaceLock(true);try{
  const saved=workspaceSaved;resetAll();S.editMode=false;
  if(saved){
   S.regions=saved.regions;S.captures=saved.captures;S.srcB64=saved.srcB64;
   S.activeCaptureId=saved.activeCaptureId;S.activeCaptureB64=saved.activeCaptureB64;
   if(S.srcB64)await displayImg(S.srcB64);
   renderCaptureStrip();renderTable();drawOverlay();workspaceDirtyUI();
  }else await fetchRegions(S.selectedPid);
  workspaceSaved=null;applyEditModeUI();toast('已取消修改');
 }catch(e){toast(e.message,'error')}finally{workspaceLock(false)}
};
const legacyApplyEdit=applyEditModeUI;
applyEditModeUI=function(){legacyApplyEdit();document.getElementById('editModeBtn').textContent=S.editMode?'結束編輯':'編輯';renderCaptureStrip();DC.style.pointerEvents='auto';workspaceDirtyUI();labelControls()};
const legacyTable=renderTable;
renderTable=function(){legacyTable();workspaceDirtyUI()};
saveRegions=async function(){
 if(workspaceBusy||!S.editMode||!S.selectedPid)return;
 const payload=workspacePayload();
 if(payload.regions.some(r=>!r.label.trim()||!Number.isFinite(r.threshold)||r.threshold<0||r.threshold>1||!Number.isInteger(r.search_margin)||r.search_margin<0||r.search_margin>10000)){toast('名稱、門檻或位置容許值無效','warning');return;}
 workspaceLock(true);
 try{
  const imageChanged=!workspaceSaved||S.srcB64!==workspaceSaved.srcB64;
  const needsFallback=payload.regions.some(r=>!r.id&&!r.source_image_b64);
  const body={...payload,version:workspaceVersion};
  if((imageChanged||needsFallback)&&S.srcB64)body.image_b64=S.srcB64;
  await studioRequest(`/api/products/${S.selectedPid}/workspace`,{method:'PUT',body:JSON.stringify(body)});
  S.editMode=false;workspaceSaved=null;await fetchRegions(S.selectedPid);await loadProducts();applyEditModeUI();toast(payload.clear_reference?'樣板已清除，產品與流程已保留':'樣板已儲存，請試跑並套用');
 }catch(e){toast(e.message,'error')}finally{workspaceLock(false)}
};
async function workspaceNavigate(path){if(workspaceBusy)return;if(workspacePending()&&!await workspaceConfirm('尚有未儲存的修改，確定離開？'))return;workspaceLeaving=true;location.href=path+'?product_id='+(S.selectedPid||0)}
const legacyAdd=openAddProduct;
openAddProduct=async function(){if(workspacePending()&&!await workspaceConfirm('尚有未儲存的修改，確定離開？'))return;legacyAdd()};
async function workspaceCapture(){
 if(workspaceBusy||!S.selectedPid||!S.editMode)return;
 workspaceLock(true);try{
  const d=await studioRequest(`/api/edge/template-frame?product_id=${S.selectedPid}`);
  _stopLabelUiLoop();_stopServerLabelPlay();clearTimeout(S.lblFallbackTimer);S.lblFallbackFile=null;
  LV.pause();S.lblVideoReady=false;S.lblServerMode=false;showVideoBar(false);_showMovingLabelVideo(false);
  _clearActivePin();S.srcB64=d.image_b64;await displayImg(d.image_b64);pinCurrentFrame();document.getElementById('backToLiveBtn').style.display='none';
  toast('相機畫面已固定，可以框選樣板');
 }catch(e){toast(e.message,'error')}finally{workspaceLock(false)}
}
async function workspaceTest(){
 if(workspaceBusy||S.editMode||!S.selectedPid)return;
 workspaceLock(true);try{
  const d=await studioRequest(`/api/products/${S.selectedPid}/label-library/test`,{method:'POST',body:'{}'});
  document.getElementById('labelTestResults').innerHTML='<table><thead><tr><th>Label</th><th>分數</th><th>門檻</th><th>樣板符合</th></tr></thead><tbody>'+d.results.map(x=>`<tr><td data-i18n-skip>${esc(x.label)}</td><td>${x.score??'—'}</td><td>${x.threshold}</td><td>${x.error?esc(x.error):x.pass?'✓':'—'}</td></tr>`).join('')+'</tbody></table>';
  document.getElementById('testDialog').showModal();
 }catch(e){toast(e.message,'error')}finally{workspaceLock(false)}
}
async function workspaceApply(){
 if(workspaceBusy||S.editMode||!S.selectedPid)return;workspaceLock(true);
 try{
  const d=await studioRequest(`/api/products/${S.selectedPid}/sop-definition`);
  if(!(d.steps||[]).length){
   const rows=S.regions.filter(r=>cleanSampleHint(r.sample_hint)!=='NEUTRAL');
   if(!rows.some(r=>cleanSampleHint(r.sample_hint)==='OK'))throw Error('直接檢查至少需要一個 OK Label');
   if(!await workspaceConfirm('建立基本檢查：全部 OK Label 都必須符合，任一 NG 符合則否決；不啟用 SOP。確定套用？'))return;
   await studioRequest(`/api/products/${S.selectedPid}/sop-definition`,{method:'POST',body:JSON.stringify({config:{enabled:false},packaging:{enabled:false},final_logic_mode:'ALL',steps:[{name:'基本檢查',enabled:true,required:true,logic_mode:'ALL',samples:rows.map(r=>({source_region_id:r.id,sample_role:cleanSampleHint(r.sample_hint),sample_name:r.label}))}]})});
  }else if(!await workspaceConfirm('將套用既有規則／流程。新增 Label 不會自動加入既有規則，請先確認引用。繼續？'))return;
  await studioRequest('/api/edge/apply',{method:'POST',body:JSON.stringify({product_id:S.selectedPid})});toast('設定已套用，請回即時檢測確認');
 }catch(e){toast(e.message,'error')}finally{workspaceLock(false)}
}
async function workspaceCameraOpen(){
 if(workspaceBusy)return;
 document.getElementById('cameraDialog').showModal();syncWorkspacePreview();
 await cameraRefresh();
}
async function workspaceCameraClose(){
 workspacePreview?.stop();
 document.getElementById('cameraDialog').close();
}
async function cameraRefresh(){
 try{
  const s=await studioRequest('/api/edge/status');
  const b=s.backend_status||{};
  const size=b.width&&b.height?` · ${b.width}×${b.height}`:'';
  document.getElementById('cameraState').textContent=s.error||
   (!s.running?'相機未啟動':`${s.backend||'相機'}${size} · ${s.frame_fresh?'影像正常':'等待影像'}`);
 }catch(e){document.getElementById('cameraState').textContent=e.message}
}
async function cameraStart(){
 if(workspaceBusy)return;
 workspaceLock(true);
 try{
  await studioRequest('/api/edge/start',{method:'POST'});

  document.getElementById('studioLive').hidden=false;syncWorkspacePreview();
  for(let i=0;i<12;i++){
   const s=await studioRequest('/api/edge/status');
   if(s.error)throw Error(s.error);
   if(s.frame_fresh)break;
   await new Promise(r=>setTimeout(r,500));
  }
 }catch(e){document.getElementById('cameraState').textContent=e.message}
 finally{workspaceLock(false);await cameraRefresh()}
}
window.addEventListener('beforeunload',e=>{if(workspacePending()&&!workspaceLeaving){e.preventDefault();e.returnValue='';}});
document.getElementById('cameraDialog').addEventListener('cancel',e=>{e.preventDefault();workspaceCameraClose()});

function workspaceExpand(){document.body.classList.toggle('expanded-label');document.getElementById('workspaceExpand').textContent=document.body.classList.contains('expanded-label')?'收回標記':'展開標記';if(labelImage)labelRender()}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('expanded-label'))workspaceExpand()});
const originalPin=pinCurrentFrame;pinCurrentFrame=function(){S.clearTemplate=false;originalPin();if(labelImage&&labelScale===null)labelRender()};

async function workspaceClearTemplates(){
 if(workspaceBusy||!S.editMode)return;
 if(!await workspaceConfirm('清除這個產品的全部樣板、Label 與取樣畫面？按「儲存樣板」才會生效；取消編輯可以還原。產品與流程會保留，仍被引用的 Label 會阻止儲存。'))return;
 resetAll();S.clearTemplate=true;renderTable();labelControls();workspaceDirtyUI();
 toast('樣板已暫時清除；請儲存或取消修改');
}


// App-owned confirmation stays visible even when the editor is temporarily locked.
function workspaceConfirm(message){return new Promise(resolve=>{
 const d=document.createElement('dialog');d.className='workspace-confirm';
 const p=document.createElement('p');p.textContent=message;d.appendChild(p);
 const no=document.createElement('button'),yes=document.createElement('button');
 no.className='btn';yes.className='btn primary';no.textContent='取消';yes.textContent='確認';
 const done=value=>{d.close();d.remove();resolve(value)};
 no.onclick=()=>done(false);yes.onclick=()=>done(true);d.oncancel=e=>{e.preventDefault();done(false)};
 d.append(no,yes);document.body.appendChild(d);d.showModal();no.focus();
})}
