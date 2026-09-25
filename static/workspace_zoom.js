// Zoom changes the viewport only. Region coordinates and source captures remain original pixels.
let labelImage=null,labelSource=null,labelScale=null,labelPan=false,labelDrag=null;
function labelReady(){return !!labelImage&&S.imgLoaded&&!(S.lblServerMode?S.lblServerPlaying:(LV&&!LV.paused));}
function labelControls(){
 const ready=labelReady();
 document.querySelectorAll('[data-label-control]').forEach(el=>el.disabled=!ready);
 document.getElementById('labelZoomValue').textContent=ready?Math.round(labelDisplayScale()*100)+'%':'—';
 document.getElementById('labelPan').setAttribute('aria-pressed',String(labelPan));
 DC.style.cursor=labelPan?'grab':'crosshair';
}
function labelDisplayScale(){const w=document.getElementById('canvasWrap');return labelScale??Math.min(1,Math.max(1,w.clientWidth-24)/labelImage.width,Math.max(1,w.clientHeight-24)/labelImage.height);}
function labelRender(){
 if(!labelImage)return;
 const zoom=labelDisplayScale();S.scale=Math.min(1,zoom);
 const dw=Math.max(1,Math.round(labelImage.width*S.scale)),dh=Math.max(1,Math.round(labelImage.height*S.scale));
 [MC,OC,DC].forEach(c=>{c.width=dw;c.height=dh;c.style.width=Math.round(labelImage.width*zoom)+'px';c.style.height=Math.round(labelImage.height*zoom)+'px';});
 Mctx.drawImage(labelImage,0,0,dw,dh);drawOverlay();labelControls();
 setStatus(`Image: ${labelImage.width}×${labelImage.height}px  Scale: ${Math.round(zoom*100)}%`);
}
function labelZoom(value){
 if(!labelReady()||S.drawing)return;
 const wrap=document.getElementById('canvasWrap'),rect=DC.getBoundingClientRect(),wr=wrap.getBoundingClientRect();
 const ux=(wr.left+wrap.clientWidth/2-rect.left)/rect.width,uy=(wr.top+wrap.clientHeight/2-rect.top)/rect.height;
 labelScale=value==='fit'?null:Math.max(.05,Math.min(2,typeof value==='number'?value:labelDisplayScale()*(value==='in'?1.25:.8)));
 labelRender();
 const nr=DC.getBoundingClientRect();
 wrap.scrollLeft+=nr.left+ux*nr.width-(wr.left+wrap.clientWidth/2);
 wrap.scrollTop+=nr.top+uy*nr.height-(wr.top+wrap.clientHeight/2);
}
function labelTogglePan(){if(!labelReady())return;S.drawing=false;S.drawCur=null;Dctx.clearRect(0,0,DC.width,DC.height);labelPan=!labelPan;labelControls();}
document.addEventListener('DOMContentLoaded',()=>{
 const wrap=document.getElementById('canvasWrap');
 DC.addEventListener('mousedown',e=>{
  if(!labelReady())return;
  if(e.button===1||(e.button===0&&labelPan)){
   e.preventDefault();e.stopImmediatePropagation();S.drawing=false;
   labelDrag={x:e.clientX,y:e.clientY,left:wrap.scrollLeft,top:wrap.scrollTop};DC.style.cursor='grabbing';
  }else if(e.button!==0)e.stopImmediatePropagation();
 },true);
 window.addEventListener('mousemove',e=>{if(labelDrag){wrap.scrollLeft=labelDrag.left-(e.clientX-labelDrag.x);wrap.scrollTop=labelDrag.top-(e.clientY-labelDrag.y);}});
 window.addEventListener('mouseup',()=>{labelDrag=null;labelControls();});
 window.addEventListener('blur',()=>{labelDrag=null;S.drawing=false;S.drawCur=null;Dctx.clearRect(0,0,DC.width,DC.height);});
 wrap.addEventListener('wheel',e=>{if(e.ctrlKey&&labelReady()){e.preventDefault();labelZoom(e.deltaY<0?'in':'out');}},{passive:false});
 labelControls();
});
