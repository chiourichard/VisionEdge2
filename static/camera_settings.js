const el=id=>document.getElementById(id);
const basicNames=new Set(['white_balance_automatic','white_balance_temperature','auto_exposure','exposure_time_absolute','focus_automatic_continuous','focus_absolute','brightness','power_line_frequency']);
let cameraState={},capabilities={},savedValues={},initialValues={},baseline='',busy=false,leaving=false;
const preview=new LatestPreview(el('preview'),'/api/edge/preview.jpg');
const controls=()=>[...document.querySelectorAll('[data-v4l2]')];
const form=()=>JSON.stringify([el('mode').value,...controls().map(input=>[input.dataset.v4l2,input.value])]);
const dirty=()=>!!baseline&&form()!==baseline;
function say(message,kind){el('cameraState').textContent=message;if(kind)VisionEdgeNotice.show(message,kind)}
async function request(url,options={}){const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(20000),...options,headers:{'Content-Type':'application/json',...options.headers}});const data=await response.json();if(!response.ok||data.success===false||data.ok===false)throw Error(data.error||'操作失敗');return data}
function ask(message){return new Promise(resolve=>{const dialog=document.createElement('dialog'),copy=document.createElement('p');copy.textContent=message;dialog.appendChild(copy);for(const [label,answer] of [['取消',false],['確認',true]]){const button=document.createElement('button');button.textContent=label;button.onclick=()=>{dialog.close();dialog.remove();resolve(answer)};dialog.appendChild(button)}dialog.oncancel=event=>{event.preventDefault();dialog.close();dialog.remove();resolve(false)};document.body.appendChild(dialog);dialog.showModal()})}
function syncPreview(){const fresh=!!cameraState.frame_fresh;el('preview').hidden=!fresh;preview.configure(fresh&&!document.hidden,'raw')}
function renderEnabled(){
 const active=el('mode').value==='manual';
 for(const input of controls()){const spec=capabilities[input.dataset.v4l2];input.disabled=busy||!active||!spec||spec.flags.some(flag=>['disabled','read-only','grabbed'].includes(flag))}
 for(const id of ['mode','start','refresh'])el(id).disabled=busy;
 el('discard').disabled=busy||!dirty();
 el('apply').disabled=busy||!dirty()||!Object.keys(capabilities).length||!!cameraState.recording||cameraState.backend==='qti';
}
function renderFields(){
 el('basic').replaceChildren();el('advanced').replaceChildren();initialValues={};
 for(const [name,spec] of Object.entries(capabilities)){
  const label=document.createElement('label'),title=document.createElement('span');title.textContent=spec.label;label.appendChild(title);
  const input=document.createElement(spec.type==='menu'||spec.type==='bool'?'select':'input');input.dataset.v4l2=name;
  if(spec.type==='menu')for(const choice of spec.choices){const option=document.createElement('option');option.value=String(choice.value);option.textContent=choice.label;input.appendChild(option)}
  else if(spec.type==='bool')for(const [value,caption] of [['1','開啟'],['0','關閉']]){const option=document.createElement('option');option.value=value;option.textContent=caption;input.appendChild(option)}
  else{input.type='number';input.min=String(spec.min);input.max=String(spec.max);input.step=String(spec.step||1)}
  input.value=String(savedValues[name]??spec.current??spec.default??'');initialValues[name]=input.value;input.oninput=renderEnabled;input.onchange=renderEnabled;label.appendChild(input);
  const hint=document.createElement('small');hint.textContent=`目前 ${spec.current??'—'} · 範圍 ${spec.min}–${spec.max}`+(spec.flags.includes('inactive')?' · 自動模式下暫未生效':'');label.appendChild(hint);
  el(basicNames.has(name)?'basic':'advanced').appendChild(label);
 }
 el('unsupported').textContent=Object.keys(capabilities).length?'未列出的項目由相機或驅動程式決定，無法從此頁調整。':'未取得可用的 V4L2 控制。';
}
async function loadCamera(){
 const [config,status,control]=await Promise.all([request('/api/edge/config'),request('/api/edge/status'),request('/api/edge/camera-controls')]);
 cameraState=status;capabilities=control.properties||{};savedValues=control.values||{};
 el('mode').value=control.mode||'off';renderFields();baseline=form();renderEnabled();syncPreview();
 el('device').textContent=control.device||'—';
 const report=status.backend_status?.camera_controls;
 if(control.error)say(control.error,'error');
 else if(report?.errors&&Object.keys(report.errors).length)say('部分相機控制未套用：'+Object.entries(report.errors).map(([name,error])=>`${name}: ${error}`).join('；'),'error');
 else say(status.error||(!status.running?'相機未啟動；設定可先儲存，下次啟動時套用':status.frame_fresh?'相機就緒，請確認實際影像':'等待相機影像'));
}
async function refreshCamera(){if(busy)return;if(dirty()&&!await ask('重新讀取會放棄尚未儲存的修改，確定繼續？'))return;busy=true;renderEnabled();try{await loadCamera()}catch(error){say(error.message,'error')}finally{busy=false;renderEnabled()}}
async function discardCamera(){if(busy)return;busy=true;try{await loadCamera();VisionEdgeNotice.show('已放棄修改','success')}catch(error){say(error.message,'error')}finally{busy=false;renderEnabled()}}
async function waitReady(){for(let i=0;i<20;i++){cameraState=await request('/api/edge/status');if(cameraState.error)throw Error(cameraState.error);if(cameraState.frame_fresh)return;await new Promise(resolve=>setTimeout(resolve,500))}throw Error('相機尚未就緒，請查看設備狀態')}
async function startPreview(){if(busy)return;if(dirty()&&!await ask('啟動預覽會重新讀取並放棄尚未儲存的修改，確定繼續？'))return;busy=true;renderEnabled();try{if(!cameraState.running)await request('/api/edge/start',{method:'POST'});await waitReady();await loadCamera();VisionEdgeNotice.show('相機預覽已就緒','success')}catch(error){say(error.message,'error')}finally{busy=false;renderEnabled()}}
async function applyCamera(){
 if(busy||el('apply').disabled)return;
 const values={...savedValues};if(el('mode').value==='off')Object.keys(values).forEach(key=>delete values[key]);
 else for(const input of controls())if(input.value!==initialValues[input.dataset.v4l2]){if(!input.value||!input.checkValidity()){input.reportValidity();return}values[input.dataset.v4l2]=Number(input.value)}
 if(!await ask('套用相機設定會中斷取像並重新啟動相機，確定繼續？'))return;
 busy=true;renderEnabled();let stored=false;
 try{const wasRunning=!!cameraState.running;await request('/api/edge/camera-controls',{method:'PUT',body:JSON.stringify({mode:el('mode').value,values,restart:wasRunning})});stored=true;
  if(wasRunning)await waitReady();await loadCamera();say(wasRunning?'設定已儲存，相機已恢復取像；請重新驗證樣板':'設定已儲存，下次啟動相機時套用；請重新驗證樣板','success');
 }catch(error){say((stored?'設定已儲存，但相機尚未恢復：':'套用失敗：')+error.message,'error')}finally{busy=false;renderEnabled()}
}
async function leaveCamera(path){if(busy)return;if(dirty()&&!await ask('尚有未儲存的修改，確定離開？'))return;leaving=true;location.href=path+'?product_id='+(new URLSearchParams(location.search).get('product_id')||cameraState.product_id||0)}
window.addEventListener('beforeunload',event=>{if((dirty()||busy)&&!leaving){event.preventDefault();event.returnValue=''}});
document.addEventListener('visibilitychange',syncPreview);window.addEventListener('pagehide',()=>preview.stop());window.addEventListener('pageshow',syncPreview);
setInterval(async()=>{if(busy||document.hidden)return;try{cameraState=await request('/api/edge/status');syncPreview();renderEnabled()}catch{cameraState={...cameraState,frame_fresh:false};syncPreview()}},1500);
refreshCamera();
