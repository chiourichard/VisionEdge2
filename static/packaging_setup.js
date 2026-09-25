function packLoad(cfg){
 cfg=cfg||{};
 document.getElementById('packEnabled').checked=!!cfg.enabled;
 for(const [id,key,fallback] of [['packMode','mode','box'],['packStable','stable_sec',.8],['packFinal','final_sec',1],['packMargin','margin',8]])document.getElementById(id).value=cfg[key]??fallback;
 packOptions();
 for(const [id,key] of [['packVacant','vacant_region_id'],['packReady','ready_region_id'],['packPresence','presence_region_id']])document.getElementById(id).value=String(cfg[key]||0);
 ST.steps.forEach(s=>s.final_visible=cfg.enabled?(cfg.final_step_numbers||[]).includes(s.step_no):s.required!==false);
 packChange(false);
}
function packOptions(){
 const options=ST.templates.filter(t=>Number(t.product_id)===Number(ST.productId)).map(t=>`<option value="${Number(t.id)}">${esc(t.label||t.sample_name||'樣板')} #${Number(t.id)}</option>`).join('');
 for(const id of ['packVacant','packReady','packPresence']){const el=document.getElementById(id),value=el.value;el.innerHTML='<option value="0">請選擇樣板</option>'+options;el.value=value||'0';}
}
function packChange(dirty=true){
 const enabled=document.getElementById('packEnabled').checked;
 document.body.classList.toggle('pack-setup-on',enabled);
 document.getElementById('packFields').hidden=!enabled;
 document.getElementById('packReadyField').hidden=document.getElementById('packMode').value==='fixture';
 if(enabled){ST.steps.forEach(s=>s.allow_out_of_order=false);setOn('swEnabled',true);setOn('swStrict',true);setOn('swLatch',true);document.getElementById('autoReset').value='MANUAL';}
 document.getElementById('autoReset').disabled=enabled;
 for(const id of ['swEnabled','swStrict','swLatch']){const el=document.getElementById(id);el.classList.toggle('disabled',enabled);el.setAttribute('aria-disabled',String(enabled));}
 renderSteps();
 if(dirty)markDirty();
}
function packBody(){return{
 enabled:document.getElementById('packEnabled').checked,mode:document.getElementById('packMode').value,
 vacant_region_id:Number(document.getElementById('packVacant').value),ready_region_id:Number(document.getElementById('packReady').value),presence_region_id:Number(document.getElementById('packPresence').value),
 stable_sec:Number(document.getElementById('packStable').value),final_sec:Number(document.getElementById('packFinal').value),margin:Number(document.getElementById('packMargin').value),
 final_step_numbers:ST.steps.flatMap((s,i)=>s.enabled&&s.final_visible!==false?[i+1]:[])
}}
async function packAssign(i){return studioMutation(async()=>{
 const region=VL.regions[i];if(!region||!ST.productId)return;
 const role=document.getElementById('vlPackRole_'+i).value;
 const res=await fetch(`${appBase()}/api/products/${ST.productId}/regions/append`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:region.label,x:Math.round(region.x),y:Math.round(region.y),w:Math.round(region.w),h:Math.round(region.h),threshold:region.threshold,search_margin:Number(document.getElementById('packMargin').value)||8,source_image_b64:region._srcB64})});
 const d=await res.json();if(!res.ok||!d.ok)throw Error(d.error||'樣板儲存失敗');
 ST.templates.push({...d.region,product_id:ST.productId});packOptions();document.getElementById(role).value=String(d.region.id);
 VL.regions.splice(i,1);renderTable();drawOverlay();markDirty();toast('已設定循環樣板，請儲存並套用');
})}
