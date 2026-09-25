"""Atomic label-workspace saves, persistent source frames, isolated foreign copies."""
import hashlib,json,math
from flask import jsonify,request

def snapshot(db,pid):
 product=db.execute('SELECT reference_img_b64 FROM products WHERE id=?',(pid,)).fetchone()
 if not product:raise LookupError('找不到產品')
 rows=[dict(x) for x in db.execute('SELECT * FROM regions WHERE product_id=? ORDER BY id',(pid,))]
 groups=[dict(x) for x in db.execute('SELECT * FROM capture_groups WHERE product_id=? ORDER BY id',(pid,))]
 payload=dict(regions=rows,captures=groups,image_b64=product['reference_img_b64'])
 payload['version']=hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()
 return payload

def register(app,base,edge):
 @app.route('/api/products/<int:pid>/workspace',methods=['GET','PUT'])
 def label_workspace(pid):
  with edge.control_lock:
   db=base.get_db()
   try:
    db.execute('BEGIN IMMEDIATE' if request.method=='PUT' else 'BEGIN')
    before=snapshot(db,pid)
    if request.method=='GET':
     if request.args.get('compact')=='1' and before['captures'] and before['image_b64']==before['captures'][0]['thumb_b64']:
      response={**before,'image_b64':None}
      return jsonify(response)
     return jsonify(before)
    body=request.get_json(silent=True)
    if not isinstance(body,dict) or not isinstance(body.get('regions'),list):raise ValueError('樣板資料格式錯誤')
    if body.get('version')!=before['version']:return jsonify(error='樣板已被更新，請重新載入後再修改'),409
    if edge.cfg.product_id==pid and (getattr(edge,'inspection_active',lambda:False)() or (edge.packaging and edge.packaging.cycle_id) or edge.status()['recording']):
     return jsonify(error='請先結束目前工件並停止錄影，再修改引用中的樣板'),409
    old={r['id']:r for r in before['regions']};seen=set()
    for r in body['regions']:
     if not isinstance(r,dict):raise ValueError('樣板資料格式錯誤')
     name=str(r.get('label','')).strip();thr=float(r['threshold']);margin=float(r['search_margin'])
     if not name or len(name)>240 or not math.isfinite(thr) or not 0<=thr<=1 or not math.isfinite(margin) or not margin.is_integer() or not 0<=margin<=10000:raise ValueError('名稱、門檻或位置容許值無效')
     if r.get('sample_hint','OK') not in ('OK','NG','NEUTRAL'):raise ValueError('樣板類型無效')
     rid=r.get('id')
     if rid is not None:
      if rid not in old or rid in seen:raise ValueError('樣板編號無效或重複')
      seen.add(rid)
     if rid is None or r.get('source_image_b64'):
      image=base.b64_to_cv2(r.get('source_image_b64') or body.get('image_b64',''))
      coords=[float(r[k]) for k in ('x','y','w','h')]
      if image is None or any(not math.isfinite(v) or not v.is_integer() for v in coords):raise ValueError('樣板影像或座標無效')
      x,y,w,h=map(int,coords)
      if min(x,y)<0 or min(w,h)<2 or x+w>image.shape[1] or y+h>image.shape[0]:raise ValueError('樣板超出原圖範圍')
    deleted=set(old)-seen
    from packaging_cycle import load_settings
    pack=load_settings(db,pid)
    if deleted.intersection(pack.get(k) for k in ('vacant_region_id','ready_region_id','presence_region_id')):
     return jsonify(error='樣板仍被規則或包裝條件引用，請先移除引用'),409
    for rid in deleted:
     if db.execute('SELECT 1 FROM inspection_item_templates t JOIN inspection_items i ON i.id=t.item_id WHERE t.source_region_id=? AND i.product_id=?',(rid,pid)).fetchone() or db.execute('SELECT 1 FROM inspection_rule_items WHERE region_id=?',(rid,)).fetchone():
      return jsonify(error='樣板仍被規則或包裝條件引用，請先移除引用'),409
    result=base._sync_product_regions(db,pid,body)
    if result.get('warnings'):raise ValueError('；'.join(result['warnings']))
    if body.get('clear_reference'):
     if body['regions']:raise ValueError('清除樣板時不可保留 Label')
     db.execute('UPDATE products SET reference_img_b64=NULL WHERE id=?',(pid,))
    # Own-product labels remain editable where engineers expect. Other products
    # retain the imported snapshot, including threshold, geometry and image.
    for row in db.execute('SELECT * FROM regions WHERE product_id=?',(pid,)).fetchall():
     db.execute('''UPDATE inspection_item_templates SET sample_name=?,threshold=?,search_margin=?,x=?,y=?,w=?,h=?,template_b64=?,source_width=?,source_height=?
       WHERE source_region_id=? AND item_id IN (SELECT id FROM inspection_items WHERE product_id=?)''',
       (row['label'],row['threshold'],row['search_margin'],row['x'],row['y'],row['w'],row['h'],row['template_b64'],
        row['source_width'],row['source_height'],row['id'],pid))
    db.commit();return jsonify(ok=True,version=snapshot(db,pid)['version'])
   except LookupError as exc:return jsonify(error=str(exc)),404
   except (ValueError,TypeError,KeyError,OverflowError) as exc:return jsonify(error=str(exc)),400
   finally:db.close()
