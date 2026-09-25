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
 const key=[d.product_id,d.active_revision,d.infer_seq,d.inference_ready,d.frame_fresh,d.definition_pending].join('|');
 if(root.dataset.renderKey===key)return;
 root.dataset.renderKey=key;
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
