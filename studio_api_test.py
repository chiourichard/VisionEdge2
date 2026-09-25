"""Library integration tests use a temporary database and synthetic camera frame."""
import tempfile,threading,time
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import server as base
from studio_api import register

def main():
 with tempfile.TemporaryDirectory() as td:
  base.DB_PATH=str(Path(td)/'test.sqlite');base.init_db();base.init_stream_schema();base.vc.ensure_runtime_revision(base.DB_PATH)
  frame=np.random.default_rng(8).integers(0,255,(120,200,3),dtype=np.uint8)
  active=[False]
  edge=SimpleNamespace(control_lock=threading.RLock(),lock=threading.RLock(),cfg=SimpleNamespace(product_id=0,source='/dev/video0',backend='opencv',camera_controls_mode='off',camera_control_values='{}'),actual_backend='opencv',packaging=None,latest_raw=frame,inspection_active=lambda:active[0],status=lambda:dict(recording=False,frame_fresh=True),_method=lambda:base.cv2.TM_CCOEFF_NORMED)
  def update_config(values,restart=False):
   edge.cfg.camera_controls_mode=values['camera_controls_mode']
   edge.cfg.camera_control_values=base.json.dumps(values['camera_control_values'])
   return {'success':True}
  edge.update_config=update_config
  register(base.app,base,edge);c=base.app.test_client()
  capabilities={'available':True,'device':'/dev/video0','properties':{'brightness':{'current':128,'min':0,'max':255,'step':1,'type':'int','flags':[],'choices':[]}}}
  with patch('v4l2_controls.query',return_value=capabilities):
   assert c.get('/api/edge/camera-controls').json['properties']['brightness']['current']==128
   assert c.put('/api/edge/camera-controls',json={'mode':'manual','values':{'brightness':180}}).status_code==200
   assert edge.cfg.camera_controls_mode=='manual'
   assert c.put('/api/edge/camera-controls',json={'mode':'manual','values':{'brightness':300}}).status_code==400
  p=c.post('/api/products',json={'serial':'LIBRARY'}).json['id']
  p2=c.post('/api/products',json={'serial':'REFERENCING'}).json['id']
  row=c.post(f'/api/products/{p}/regions/append',json=dict(label='A',x=30,y=20,w=40,h=30,threshold=.8,search_margin=5,source_image_b64=base.cv2_to_b64(frame))).json['region']
  rid=row['id'];url=f'/api/products/{p}/label-library'
  assert c.get(f'/api/products/{p}/sop-definition').json['steps']==[], 'Saving label must not create SOP'
  definition=dict(config={'enabled':False},steps=[dict(name='Group',samples=[dict(source_region_id=rid,sample_role='OK')])])
  res=c.post(f'/api/products/{p2}/sop-definition',json=definition);assert res.status_code==200,res.json
  old=c.get(url).json['items'][0];assert old['references'][0]['product_id']==p2
  body=dict(version=old['version'],label='New A',threshold=0,search_margin=12,sample_hint='NG')
  edge.cfg.product_id=p;active[0]=True
  assert c.put(f'{url}/{rid}',json=body).status_code==409
  active[0]=False
  assert c.put(f'{url}/{rid}',json={**body,'search_margin':1.5}).status_code==400
  assert c.put(f'{url}/{rid}',json={**body,'threshold':float('nan')}).status_code==400
  assert c.put(f'{url}/{rid}',json=body).status_code==200
  assert c.put(f'{url}/{rid}',json=body).status_code==409,'Stale update must not overwrite'
  new=c.get(url).json['items'][0];assert new['version']!=old['version'] and new['label']=='New A'
  sample=c.get(f'/api/products/{p2}/sop-definition').json['steps'][0]['samples'][0]
  assert sample['sample_name']!='New A' and sample['threshold']==.8 and sample['search_margin']==5,sample
  assert sample['sample_role']=='OK','Library hint must not silently invert existing rule'
  assert c.delete(f'{url}/{rid}',json={'version':new['version']}).status_code==409
  edge.latest_raw=base.cv2.resize(frame,(100,60),interpolation=base.cv2.INTER_AREA)
  res=c.post(url+'/test',json={});assert res.status_code==200,res.json
  result=res.json['results'][0]
  assert res.json['scope']=='labels_only' and result['pass'],res.json
  assert result['x']==15 and result['y']==10 and result['match_size']==[20,15],result
  assert result['score']>.9,result
  uploaded=c.post(f'{url}/{rid}/image-test',json={'image_b64':base.cv2_to_b64(edge.latest_raw)})
  assert uploaded.status_code==200,uploaded.json
  assert uploaded.json['inference_device']=='cpu' and uploaded.json['scope']=='single_label_only'
  assert uploaded.json['result']['pass'] and uploaded.json['result']['match_size']==[20,15]
  assert uploaded.json['image_b64'].startswith('data:image/jpeg;base64,')
  wrong_ratio=base.cv2.resize(frame,(100,70))
  uploaded=c.post(f'{url}/{rid}/image-test',json={'image_b64':base.cv2_to_b64(wrong_ratio)})
  assert uploaded.status_code==200 and uploaded.json['result']['error'].startswith('相機與樣板來源長寬比不同')
  assert c.post(f'/api/products/{p2}/sop-definition',json=dict(config={'enabled':False},steps=[])).status_code==200
  assert c.delete(f'{url}/{rid}',json={'version':new['version']}).status_code==200
  assert c.get(url).json['items']==[]
 print('STUDIO_API PASS: independent library, foreign copy isolation, stale/active guards, label test, delete references')

if __name__=='__main__':main()
