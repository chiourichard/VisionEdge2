// Keep the operator's result/raw preference through camera startup and reconnects.
function effectiveLiveView(){return viewMode==='result'&&lastStatus.inference_ready&&lastStatus.frame_fresh?'result':'raw'}
function labelScoreModel(r){
 const number=v=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v));
 const valid=number(r.score)&&number(r.threshold)&&!r.error;
 const score=Number(r.score),threshold=number(r.threshold)?Number(r.threshold):NaN,ng=r.sample_role==='NG';
 const hit=valid&&(typeof r.matched==='boolean'?r.matched:score>=threshold);
 return {valid,score,threshold,ng,hit,color:!valid?'unknown':ng?(hit?'bad':'good'):(hit?'good':'bad'),
  text:!valid?'無有效分數':ng?(hit?'NG 命中':'NG 未命中'):(hit?'符合':'未符合')};
}
function renderLabelScores(d){
 const root=document.getElementById('labelScores');if(!root)return;
 if(!d.inference_ready||!d.frame_fresh||d.definition_pending){root.innerHTML='<p class="subtle">等待有效檢測結果</p>';return}
 const rows=Array.isArray(d.results)?d.results:[];
 if(!rows.length){root.innerHTML='<p class="subtle">尚無 Label 比對結果</p>';return}
 root.innerHTML=rows.map(r=>{const m=labelScoreModel(r),pct=v=>Math.max(0,Math.min(100,v*100));return `<div class="label-score ${m.color}">
  <div class="score-name" data-i18n-skip>${esc(r.label||r.sample_id||r.id||'Label')}</div>
  <div class="score-track" aria-hidden="true"><span class="score-fill" style="width:${m.valid?pct(m.score):0}%"></span>${m.valid?`<i class="score-threshold" style="left:${pct(m.threshold)}%"></i>`:''}</div>
  <div class="score-values"><span>分數</span> <b data-i18n-skip>${m.valid?m.score.toFixed(3):'—'}</b> / <span>門檻</span> <b data-i18n-skip>${Number.isFinite(m.threshold)?m.threshold.toFixed(3):'—'}</b></div>
  <div class="score-state">${m.text}</div>${r.error?`<div class="score-error">${esc(r.error)}</div>`:''}</div>`}).join('');
}
function setLiveExpanded(on){
 const card=document.querySelector('.camera-card');card.classList.toggle('live-expanded',on);
 document.body.classList.toggle('live-expanded-open',on);
 mountFloatingFlow(on);
 document.getElementById('expandLive').textContent=on?'縮小畫面':'放大畫面';
 document.getElementById('expandLive').setAttribute('aria-expanded',String(on));
}
async function toggleLiveExpanded(){
 const card=document.querySelector('.camera-card');
 if(card.classList.contains('live-expanded')){
  if(document.fullscreenElement===card)try{await document.exitFullscreen()}catch{}
  setLiveExpanded(false);return;
 }
 setLiveExpanded(true);
 // Window-sized expansion remains usable if native fullscreen is unavailable.
 if(card.requestFullscreen)try{await card.requestFullscreen()}catch{}
}
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement)setLiveExpanded(false)});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!document.fullscreenElement)setLiveExpanded(false)});

let floatingFlow=null,floatingHomes=[],floatingPosition=null;
function clampFloatingFlow(){
 if(!floatingFlow||floatingFlow.hidden)return;
 const host=document.querySelector('.camera-card'),header=host.querySelector('.cardhead');
 const top=header.offsetHeight+8,margin=8;
 floatingFlow.style.maxHeight=Math.max(80,host.clientHeight-top-margin)+'px';
 const maxX=Math.max(margin,host.clientWidth-floatingFlow.offsetWidth-margin);
 const maxY=Math.max(top,host.clientHeight-floatingFlow.offsetHeight-margin);
 const pos=floatingPosition||{x:maxX,y:top};
 floatingPosition={x:Math.max(margin,Math.min(maxX,pos.x)),y:Math.max(top,Math.min(maxY,pos.y))};
 floatingFlow.style.left=floatingPosition.x+'px';floatingFlow.style.top=floatingPosition.y+'px';
}
function toggleFloatingFlow(){
 const body=document.getElementById('floatingFlowBody'),button=document.getElementById('floatingFlowToggle');
 body.hidden=!body.hidden;button.textContent=body.hidden?'展開':'收起';button.setAttribute('aria-expanded',String(!body.hidden));
 floatingFlow.classList.toggle('folded',body.hidden);clampFloatingFlow();
}
function mountFloatingFlow(on){
 if(on&&!floatingFlow){
  floatingFlow=document.createElement('section');floatingFlow.id='floatingFlow';
  floatingFlow.setAttribute('aria-label','檢測操作面板');
  floatingFlow.innerHTML='<div class="floating-flow-handle"><button type="button" id="floatingFlowDrag" aria-label="拖曳面板；方向鍵移動">⠿ <span>檢測操作</span></button><span id="floatingFlowSummary" role="status"></span><button type="button" id="floatingFlowToggle" aria-controls="floatingFlowBody" aria-expanded="true">收起</button></div><div id="floatingFlowDiagram"></div><div id="floatingFlowBody"></div>';
  document.querySelector('.camera-card').appendChild(floatingFlow);
  document.getElementById('floatingFlowToggle').onclick=toggleFloatingFlow;
  const handle=document.getElementById('floatingFlowDrag');let drag=null;
  handle.addEventListener('pointerdown',e=>{if(e.button!==0)return;clampFloatingFlow();drag={id:e.pointerId,x:e.clientX,y:e.clientY,...{left:floatingPosition.x,top:floatingPosition.y}};handle.setPointerCapture(e.pointerId);e.preventDefault()});
  handle.addEventListener('pointermove',e=>{if(!drag||drag.id!==e.pointerId)return;floatingPosition={x:drag.left+e.clientX-drag.x,y:drag.top+e.clientY-drag.y};clampFloatingFlow()});
  const finish=()=>{drag=null};handle.addEventListener('pointerup',finish);handle.addEventListener('pointercancel',finish);handle.addEventListener('lostpointercapture',finish);
  handle.addEventListener('keydown',e=>{const delta={ArrowLeft:[-20,0],ArrowRight:[20,0],ArrowUp:[0,-20],ArrowDown:[0,20]}[e.key];if(!delta)return;e.preventDefault();clampFloatingFlow();floatingPosition={x:floatingPosition.x+delta[0],y:floatingPosition.y+delta[1]};clampFloatingFlow()});
 }
 if(!floatingFlow)return;
 if(on){
  if(!floatingHomes.length){
   const cards=[document.getElementById('packagingCard'),document.getElementById('flow').closest('section'),document.getElementById('manualInspectionCard')];
   for(const card of cards){const marker=document.createComment('floating-panel-home');card.before(marker);floatingHomes.push({card,marker});document.getElementById('floatingFlowBody').appendChild(card)}
  }
  floatingFlow.hidden=false;updateFloatingFlow(lastStatus);clampFloatingFlow();
 }else{
  for(const {card,marker} of floatingHomes){marker.replaceWith(card)}floatingHomes=[];floatingFlow.hidden=true;
 }
}
// Use current rule results, not latched SOP completion, for a single step.
function floatingFlowSummaryModel(d){
 const sop=d.sop||{},alarm=!!(sop.alarm?.active||d.packaging?.alarm?.active);
 const result=(text,tone='neutral',name='')=>({text,tone,name,alarm});
 if(!d.inference_ready||!d.frame_fresh||d.definition_pending)return result('等待有效檢測結果');
 const p=d.packaging;
 const waiting={WAIT_CLEAR:'請先清空工作區',WAIT_READY:'等待下一箱',FAULT:'檢測已停止'};
 if(p&&waiting[p.state])return result(waiting[p.state],p.state==='FAULT'?'bad':'neutral');
 const steps=Array.isArray(sop.steps)?sop.steps:[];
 if(steps.length===1){
  const rule=(d.rules||[]).find(r=>r.id!=null&&String(r.id)===String(steps[0].id));
  if(typeof rule?.pass!=='boolean')return result('等待有效檢測結果');
  return result(rule.pass?'PASS':'FAIL',rule.pass?'good':'bad');
 }
 if(p?.state==='READY_TO_REMOVE')return result('齊套確認完成，請取走','good');
 if(sop.steps_complete||sop.complete)return result('步驟已完成',alarm?'neutral':'good');
 const index=steps.findIndex(s=>String(s.id)===String(sop.current_step_id));
 if(index>=0)return result('目前步驟','neutral',`${index+1} / ${steps.length} · ${steps[index].name||sop.current_step_name||''}`);
 return result(steps.length?'等待檢測':'尚未設定步驟');
}
function floatingFlowDiagramHTML(d){
 const sop=d.sop||{},steps=Array.isArray(sop.steps)?sop.steps:[];
 if(steps.length<2)return '';
 const fresh=!!(d.inference_ready&&d.frame_fresh&&!d.definition_pending);
 const waiting=['WAIT_CLEAR','WAIT_READY','FAULT'].includes(d.packaging?.state);
 return `<ol class="compact-flow" style="--step-columns:${Math.min(4,steps.length)}">`+steps.map((s,i)=>{
  const done=fresh&&!waiting&&s.status==='DONE';
  const current=fresh&&!waiting&&!done&&String(s.id)===String(sop.current_step_id);
  const alarm=fresh&&sop.alarm?.active&&String(sop.alarm.step_id)===String(s.id);
  const state=done?'done':current?'current':'pending';
  const label=done?'完成':current?'目前步驟':'未完成';
  const name=String(s.name||'');
  return `<li class="compact-step ${state}${alarm?' alarm':''}"${current?' aria-current="step"':''}><span class="compact-node" aria-hidden="true">${done?'✓':i+1}</span><span class="compact-step-name" data-i18n-skip>${i+1}. ${esc(name)}</span><span class="compact-step-state">${!fresh?'等待有效檢測結果':label}</span>${alarm?'<span class="compact-step-alarm" aria-label="警報">!</span>':''}</li>`;
 }).join('')+'</ol>';
}
function updateFloatingFlow(d){
 if(!floatingFlow||floatingFlow.hidden)return;
 const summary=document.getElementById('floatingFlowSummary'),m=floatingFlowSummaryModel(d);
 const html=`<span class="floating-summary-state ${m.tone}">${esc(m.text)}</span>${m.name?`<span class="floating-summary-name" data-i18n-skip>${esc(m.name)}</span>`:''}${m.alarm?'<span class="floating-summary-alarm">⚠ 警報待確認</span>':''}`;
 if(summary.innerHTML!==html)summary.innerHTML=html;
 const diagram=document.getElementById('floatingFlowDiagram'),diagramHTML=floatingFlowDiagramHTML(d);
 if(diagram.innerHTML!==diagramHTML)diagram.innerHTML=diagramHTML;
 floatingFlow.classList.toggle('has-diagram',!!diagramHTML);
 clampFloatingFlow();
}
window.addEventListener('resize',clampFloatingFlow);
document.addEventListener('fullscreenchange',clampFloatingFlow);
