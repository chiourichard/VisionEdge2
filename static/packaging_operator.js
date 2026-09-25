let packOffset=0,packBusy=false;
const packLabels={WAIT_CLEAR:'請先清空工作區',WAIT_READY:'等待下一箱',ACTIVE:'依序放入物品',READY_TO_REMOVE:'齊套確認完成，請取走',FAULT:'檢測已停止'};
const packOutcomes={IN_PROGRESS:'檢測中',READY:'等待取走',OK:'合格',INCOMPLETE:'未完成即取走',INTERRUPTED:'已中止'};
function renderPackaging(d){
 const p=d.packaging,active=!!p;
 $('packagingCard').hidden=!active;$('manualInspectionCard').hidden=active;
 for(const id of ['sopReset','sopFinish','sopAck'])$(id).hidden=active;
 if(!active)return;
 const fresh=d.inference_ready&&!d.definition_pending;
 const alarm=d.sop?.alarm?.active;
 const title=!fresh?'影像未就緒':p.state==='FAULT'?packLabels.FAULT:alarm?'請處理檢測異常':p.pause_reason?'等待確認':packLabels[p.state]||p.state;
 $('packState').textContent=title;
 const next=!fresh?'請確認相機與設定，本畫面不代表目前產品已通過。':p.error||p.pause_reason||(alarm?d.sop.alarm.message:
 p.state==='WAIT_CLEAR'?'請讓空台或空治具完整入鏡，系統確認清空後才會接受新的一箱。':
 p.state==='WAIT_READY'?(p.mode==='fixture'?'治具已清空，放入第一個物品即可開始。':'請放入空箱，並保持箱緣定位特徵可見。'):
 p.state==='ACTIVE'?(d.sop?.current_step_name?'下一步：'+d.sop.current_step_name:'正在確認最後應可見的物品。'):
 p.state==='READY_TO_REMOVE'?'取走後會自動保存本箱結果；同一箱留在畫面中不會重複計數。':'');
 $('packNext').textContent=next;
 $('packCycleId').textContent=p.cycle_id?'ID '+p.cycle_id.slice(0,12):'';
 $('packSignals').innerHTML=[['vacant','清空'],['ready','空箱'],['presence','定位']].filter(([key])=>p.mode!=='fixture'||key!=='ready').map(([key,label])=>`<span class="${fresh&&p.signals?.[key]?'hit':''}"><span>${esc(label)}</span> · ${fresh&&p.signals?.[key]?'✓':'—'}</span>`).join('');
 $('packAck').hidden=!alarm;$('packAck').disabled=packBusy||!fresh||p.state==='FAULT';$('packCancel').disabled=packBusy||!p.cycle_id||p.state==='FAULT';
 $('verdict').textContent=title;$('verdict').className='state '+(fresh&&p.state==='READY_TO_REMOVE'&&!p.pause_reason&&!alarm?'good':alarm||p.state==='FAULT'?'bad':'warn');
}
async function packAction(action){
 if(packBusy)return;
 if(action==='cancel'&&!confirm('中止此箱會保留中止紀錄，並要求清空工作區。確定中止？'))return;
 packBusy=true;renderPackaging(lastStatus);
 try{const d=await req(A+'/packaging/'+action,{method:'POST'});if(d.success===false)throw Error('操作未完成，請確認儲存空間');await pollOnce()}catch(e){message(e.message,'err')}finally{packBusy=false;renderPackaging(lastStatus)}
}
async function loadPackHistory(reset=false){
 if(reset)packOffset=0;
 try{const d=await req(A+'/packaging/history?offset='+packOffset);
 $('packHistoryBody').innerHTML=d.items.map(r=>`<tr><td>${esc(new Date(r.created*1000).toLocaleString())}</td><td><span data-i18n-skip>${esc(r.product_id)} / ${esc(r.id.slice(0,12))}</span></td><td>${esc(packOutcomes[r.status]||r.status)}</td><td><button class="mini" data-cycle="${esc(r.id)}">查看步驟證據</button></td></tr>`).join('')||'<tr><td colspan="4" class="history-empty">尚無自動包裝紀錄</td></tr>';
 $('packPrev').disabled=packOffset===0;$('packNextPage').disabled=!d.more;$('packPage').textContent=String(packOffset/30+1);
 }catch(e){message(e.message,'err')}
}
async function showPackEvidence(id){
 try{const d=await req(A+'/packaging/history/'+encodeURIComponent(id));
 $('packEvents').hidden=false;
 $('packEvents').innerHTML='<h3>本箱事件與證據</h3>'+d.events.map(e=>{const names={ALARM:'檢測異常',START:'開始本箱',FINAL_VERIFIED:'齊套確認',FINAL_REVOKED:'齊套條件已失效',REMOVED:'工作區已清空',INTERRUPTED:'中止'};const label=names[e.kind]||e.kind.replace('STEP_','步驟 ');const url=A+'/packaging/evidence/'+encodeURIComponent(e.id);return `<p><span data-i18n-skip>${esc(new Date(e.created*1000).toLocaleTimeString())}</span> · <span>${esc(label)}</span> · ${e.has_image?`<a target="_blank" rel="noopener" href="${url}/raw">原圖</a> · <a target="_blank" rel="noopener" href="${url}/result">結果</a> · `:''}<a target="_blank" rel="noopener" href="${url}/metadata">明細</a></p>`}).join('');
 }catch(e){message(e.message,'err')}
}
document.addEventListener('DOMContentLoaded',()=>{$('packHistoryBody').addEventListener('click',e=>{const b=e.target.closest('[data-cycle]');if(b)showPackEvidence(b.dataset.cycle)})});
