let studioPreview=null;
function syncStudioPreview(){
 const image=document.getElementById('studioLive');
 if(!studioPreview)studioPreview=new LatestPreview(image,'/api/edge/preview.jpg');
 studioPreview.configure(libraryMode&&!image.hidden&&!document.hidden,'raw');
}
document.addEventListener('visibilitychange',()=>{if(studioPreview)syncStudioPreview()});
window.addEventListener('pagehide',()=>studioPreview?.stop());
window.addEventListener('pageshow',()=>{if(studioPreview)syncStudioPreview()});
const libraryMode=location.pathname.endsWith('/template-studio');
let libraryRows=[],libraryEdits=new Set(),libraryBusy=false,qtiReport=null,libraryLeaving=false;
async function studioRequest(url,options={}){const r=await fetch(url,{...options,headers:{'Content-Type':'application/json',...options.headers}});const d=await r.json();if(!r.ok||d.ok===false||d.success===false)throw Error(d.error||'操作失敗');return d;}
function libraryPending(){return libraryEdits.size>0||(libraryMode&&VL.regions.length>0);}
function libraryNavigate(path){if((ST.dirty||libraryPending())&&!confirm('尚有未儲存的修改，確定離開？'))return;libraryLeaving=true;location.href=path+'?product_id='+ST.productId;}
async function libraryRefresh(){
 if(!libraryMode)return;
 libraryEdits.clear();document.getElementById('labelTestResults').innerHTML='';
 if(!ST.productId){libraryRows=[];libraryRender();return;}
 const d=await studioRequest('/api/products/'+ST.productId+'/label-library');libraryRows=d.items;libraryRender();
}
function libraryRender(){
 document.getElementById('libraryCount').textContent=String(libraryRows.length);
 document.getElementById('libraryBody').innerHTML=libraryRows.map(r=>`<tr data-library-id="${r.id}">
 <td><img alt="" src="${r.template_b64.startsWith('data:')?r.template_b64:'data:image/jpeg;base64,'+r.template_b64}" style="width:80px;height:60px;object-fit:contain"></td>
 <td><input aria-label="Label 名稱" data-label-field="label" value="${esc(r.label)}"><small data-i18n-skip>#${r.id} · ${r.x},${r.y} · ${r.w}×${r.h}</small></td>
 <td><input aria-label="比對門檻" data-label-field="threshold" type="number" min="0" max="1" step="0.01" value="${r.threshold}"></td>
 <td><input aria-label="位置容許偏移" data-label-field="search_margin" type="number" min="0" max="10000" step="1" value="${r.search_margin}"></td>
 <td><select aria-label="樣板類型" data-label-field="sample_hint">${['OK','NG','NEUTRAL'].map(x=>`<option ${x===r.sample_hint?'selected':''} value="${x}">${x==='NEUTRAL'?'一般':x}</option>`).join('')}</select></td>
 <td><span data-i18n-skip>${r.references.map(x=>esc(x.product_id+' / '+x.name)).join('<br>')||'—'}</span></td>
 <td><button class="btn mini" onclick="librarySave(${r.id})">儲存 Label</button><button class="btn mini danger" onclick="libraryDelete(${r.id})">刪除</button><span class="row-dirty"></span></td></tr>`).join('')||'<tr><td colspan="7">尚無樣板，請先擷取畫面並框選。</td></tr>';
 document.querySelectorAll('#libraryBody [data-label-field]').forEach(el=>el.addEventListener('input',()=>{const row=el.closest('tr');libraryEdits.add(Number(row.dataset.libraryId));row.querySelector('.row-dirty').textContent='●';document.getElementById('libraryStatus').textContent='Label 尚未儲存';}));
}
async function librarySave(id){
 if(libraryBusy)return;
 const r=libraryRows.find(x=>x.id===id),row=document.querySelector(`[data-library-id="${id}"]`),data={version:r.version};
 row.querySelectorAll('[data-label-field]').forEach(el=>data[el.dataset.labelField]=el.value);
 if(r.references.length&&!confirm('此修改會同步更新引用此 Label 的規則；儲存後需重新套用。確定？'))return;
 librarySetBusy(true);studioPreview?.stop();
 try{await studioRequest(`/api/products/${ST.productId}/label-library/${id}`,{method:'PUT',body:JSON.stringify(data)});libraryEdits.delete(id);await libraryReloadOne(id);document.getElementById('libraryStatus').textContent='樣板已儲存，請試跑並套用';toast('樣板已儲存，請試跑並套用');}catch(e){toast(e.message,true)}finally{librarySetBusy(false);}
}
async function libraryReloadOne(id){
 // Preserve unsaved edits in other rows; never silently replace the whole form.
 const edits={};document.querySelectorAll('#libraryBody tr[data-library-id]').forEach(row=>{if(Number(row.dataset.libraryId)!==id&&libraryEdits.has(Number(row.dataset.libraryId))){edits[row.dataset.libraryId]={};row.querySelectorAll('[data-label-field]').forEach(el=>edits[row.dataset.libraryId][el.dataset.labelField]=el.value);}});
 const d=await studioRequest('/api/products/'+ST.productId+'/label-library');libraryRows=d.items;libraryRender();
 for(const [rid,fields] of Object.entries(edits)){const row=document.querySelector(`[data-library-id="${rid}"]`);if(row){for(const [key,v] of Object.entries(fields))row.querySelector(`[data-label-field="${key}"]`).value=v;row.querySelector('.row-dirty').textContent='●';}}
 document.getElementById('labelTestResults').innerHTML='';
 ST.templates=await studioRequest('/api/templates');
}
async function libraryDelete(id){if(libraryBusy||!confirm('刪除此未引用的樣板？'))return;librarySetBusy(true);try{await studioRequest(`/api/products/${ST.productId}/label-library/${id}`,{method:'DELETE',body:JSON.stringify({version:libraryRows.find(x=>x.id===id).version})});libraryEdits.delete(id);await libraryReloadOne(id);toast('樣板已刪除')}catch(e){toast(e.message,true)}finally{librarySetBusy(false)}}
async function librarySaveDraft(i){
 if(libraryBusy)return;const r=VL.regions[i];if(!r||!ST.productId)return;
 const row=document.getElementById('draftLabel_'+i);r.label=row.querySelector('[data-draft="label"]').value.trim();r.threshold=Number(row.querySelector('[data-draft="threshold"]').value);r.search_margin=Number(row.querySelector('[data-draft="margin"]').value);
 if(!r.label||!Number.isFinite(r.threshold)||r.threshold<0||r.threshold>1||!Number.isInteger(r.search_margin)||r.search_margin<0||r.search_margin>10000){toast('名稱、門檻或位置容許值無效',true);return;}
 librarySetBusy(true);try{await studioRequest(`/api/products/${ST.productId}/regions/append`,{method:'POST',body:JSON.stringify({...r,source_image_b64:r._srcB64})});VL.regions.splice(i,1);renderTable();drawOverlay();await libraryReloadOne(-1);document.getElementById('libraryStatus').textContent='樣板已儲存，請試跑並套用';toast('樣板已儲存，請試跑並套用');}catch(e){toast(e.message,true)}finally{librarySetBusy(false);}
}
function libraryDraftTable(){
 document.getElementById('regionBody').innerHTML=VL.regions.map((r,i)=>`<tr id="draftLabel_${i}"><td>${i+1}</td><td>${r._thumbB64?`<img alt="" src="${r._thumbB64}" style="width:48px">`:''}</td><td><input aria-label="Label 名稱" data-draft="label" value="${esc(r.label)}" oninput="VL.regions[${i}].label=this.value;drawOverlay()"></td><td><input aria-label="比對門檻" data-draft="threshold" type="number" min="0" max="1" step="0.01" value="${r.threshold}" oninput="VL.regions[${i}].threshold=Number(this.value)"></td><td><input aria-label="位置容許偏移" data-draft="margin" type="number" min="0" max="10000" value="${r.search_margin||0}" oninput="VL.regions[${i}].search_margin=Number(this.value)"></td><td><button class="btn mini primary" onclick="librarySaveDraft(${i})">儲存 Label</button><button class="btn mini" onclick="delRegion(${i})">刪除</button></td></tr>`).join('');
}
async function libraryTest(){
 if(!ST.productId||libraryBusy)return;
 if(libraryPending()){toast('請先儲存所有 Label 再試跑',true);return;}
 librarySetBusy(true);
 try{const d=await studioRequest(`/api/products/${ST.productId}/label-library/test`,{method:'POST',body:'{}'});
 document.getElementById('labelTestResults').innerHTML='<p>逐 Label 試跑使用各自的位置容許值；不代表包裝或 SOP 合格，不新增生產履歷。</p><table><thead><tr><th>Label</th><th>分數</th><th>門檻</th><th>樣板符合</th></tr></thead><tbody>'+d.results.map(x=>`<tr><td data-i18n-skip>${esc(x.label)}</td><td>${x.score??'—'}</td><td>${x.threshold}</td><td>${x.error?esc(x.error):x.pass?'✓':'—'}</td></tr>`).join('')+'</tbody></table>';}
 catch(e){toast(e.message,true)}finally{librarySetBusy(false)}
}
async function libraryApply(){
 if(!ST.productId||libraryPending()||libraryBusy){toast('請先儲存所有 Label 再套用',true);return;}
 librarySetBusy(true);
 try{
 const d=await studioRequest(`/api/products/${ST.productId}/sop-definition`);
 if(!(d.steps||[]).length){
  const chosen=libraryRows.filter(r=>r.sample_hint!=='NEUTRAL');
  if(!chosen.some(r=>r.sample_hint==='OK'))throw Error('直接檢查至少需要一個 OK Label');
  if(!confirm('建立基本檢查：全部 OK Label 都必須符合，任一 NG 符合則否決；不啟用 SOP。確定套用？'))return;
  await studioRequest(`/api/products/${ST.productId}/sop-definition`,{method:'POST',body:JSON.stringify({config:{enabled:false},packaging:{enabled:false},final_logic_mode:'ALL',steps:[{name:'基本檢查',enabled:true,required:true,logic_mode:'ALL',samples:chosen.map(r=>({source_region_id:r.id,sample_role:r.sample_hint,sample_name:r.label}))}]})});
 }else if(!confirm('將套用既有規則／流程。新增 Label 不會自動加入既有規則，請先確認引用。繼續？'))return;
 await studioRequest('/api/edge/apply',{method:'POST',body:JSON.stringify({product_id:ST.productId})});await libraryReloadOne(-1);document.getElementById('libraryStatus').textContent='設定已套用，請回即時檢測確認';toast('設定已套用，請回即時檢測確認');
 }catch(e){toast(e.message,true)}finally{librarySetBusy(false)}
}
async function cameraRefresh(){try{
 const [c,s]=await Promise.all([studioRequest('/api/edge/config'),studioRequest('/api/edge/status')]);
 const cfg=c.config||c;document.getElementById('qtiMode').value=cfg.camera_controls_mode||'safe';
 qtiReport=s.backend_status?.camera_controls;
 const values=JSON.parse(cfg.camera_control_values||'{}');
 document.querySelectorAll('[data-qti]').forEach(original=>{
  const key=original.dataset.qti,prop=qtiReport?.properties?.[key];let el=original;
  if(prop?.choices?.length&&el.tagName!=='SELECT'){
   el=document.createElement('select');el.dataset.qti=key;el.dataset.default=original.dataset.default;original.replaceWith(el);
  }
  if(el.tagName==='SELECT')el.innerHTML=(prop?.choices||[]).map(x=>`<option value="${x.value}" data-i18n-skip>${esc(x.label)} (${x.value})</option>`).join('');
  el.value=values[key]??prop?.current??prop?.default??el.dataset.default;
  el.parentElement.querySelector('small').textContent=prop?.supported?'設備支援':'尚未確認設備支援';
  if(prop){el.min=prop.min;el.max=prop.max;}
 });cameraMode();
 document.getElementById('qtiState').textContent=s.error||(!s.running?'相機未啟動':s.backend!=='qti'?'目前不是 QTI 相機':qtiReport?.verified?'QTI 設定已讀回，請確認實際影像':'等待設備確認');
 document.getElementById('qtiApplied').textContent=!qtiReport?'尚未確認設備支援':qtiReport?.mode==='off'?'目前保留設備預設設定':qtiReport?.mode==='safe'?'目前只套用白平衡':'手動模式：調整後請儲存並確認實際影像';
 }catch(e){toast(e.message,true)}}
async function cameraStart(){if(libraryBusy)return;librarySetBusy(true);try{await studioRequest('/api/edge/start',{method:'POST'});document.getElementById('studioLive').hidden=false;syncStudioPreview();for(let i=0;i<12;i++){const s=await studioRequest('/api/edge/status');if(s.error)throw Error(s.error);if(s.frame_fresh)break;await new Promise(r=>setTimeout(r,500));}await cameraRefresh()}catch(e){toast(e.message,true)}finally{librarySetBusy(false)}}
async function cameraApply(){
 if(libraryBusy)return;
 if(VL.regions.length&&!confirm('變更相機將清除尚未儲存的框選，確定？'))return;
 const values={};document.querySelectorAll('[data-qti]').forEach(el=>{if(!el.disabled)values[el.dataset.qti]=Number(el.value)});
 librarySetBusy(true);studioPreview?.stop();
 try{await studioRequest('/api/edge/config',{method:'PUT',body:JSON.stringify({restart:true,camera_controls_mode:document.getElementById('qtiMode').value,camera_control_values:JSON.stringify(values)})});const state=await studioRequest('/api/edge/status');if(!state.running)await studioRequest('/api/edge/start',{method:'POST'});resetAll();document.getElementById('studioLive').hidden=false;syncStudioPreview();document.getElementById('qtiState').textContent='設定已儲存，等待相機啟動；請重新確認影像及樣板';
 for(let i=0;i<12;i++){await new Promise(r=>setTimeout(r,500));const s=await studioRequest('/api/edge/status');if(s.error)throw Error(s.error);if(s.frame_fresh){await cameraRefresh();break;}}
 }catch(e){document.getElementById('qtiState').textContent=e.message;toast(e.message,true)}finally{librarySetBusy(false)}}
window.addEventListener('beforeunload',e=>{if(libraryPending()&&!libraryLeaving){e.preventDefault();e.returnValue='';}});
document.addEventListener('DOMContentLoaded',()=>{if(libraryMode){document.title='VisionEdge · 取像與樣板';cameraRefresh()}});

function librarySetBusy(value){
 libraryBusy=value;document.querySelector('main').inert=value;
 document.querySelectorAll('header button,header select').forEach(el=>el.disabled=value);
}
function cameraMode(){
 const mode=document.getElementById('qtiMode').value;
 document.querySelectorAll('[data-qti]').forEach(el=>el.disabled=!qtiReport?.properties?.[el.dataset.qti]?.supported||mode==='off'||(mode==='safe'&&el.dataset.qti!=='white_balance_mode'));
}
