"""Integration checks: real DB/API, synthetic video, no physical camera."""
import tempfile
import time
from pathlib import Path
import cv2
import numpy as np
import server as base
import vision_core as vc
import visionedge_server as web
from edge_runtime import EdgeRuntime

def wait(fn, seconds=6):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if fn(): return
        time.sleep(.04)
    raise AssertionError('Timed out')

def main():
    original_db,original_edge=base.DB_PATH,web.EDGE
    original_history=web.INSPECTIONS.path
    with tempfile.TemporaryDirectory() as td:
        root=Path(td)
        try:
            from inspection_history import InspectionHistory
            web.INSPECTIONS.path=InspectionHistory(root/'history.sqlite').path
            base.DB_PATH=str(root/'db.sqlite')
            base.init_db();base.init_stream_schema();vc.ensure_runtime_revision(base.DB_PATH)
            client=web.app.test_client()
            assert client.get('/').status_code==200
            assert client.post('/api/backend-login',json={}).status_code==404
            pid=client.post('/api/products',json={'serial':'TEST'}).json['id']
            other=client.post('/api/products',json={'serial':'OTHER'}).json['id']
            frame=np.random.default_rng(1).integers(0,255,(120,160,3),dtype=np.uint8)
            image=base.cv2_to_b64(frame)
            region=dict(label='one',x=20,y=20,w=30,h=30,threshold=.8,search_margin=5)
            url=f'/api/products/{pid}/regions'
            def save_region():
                r=client.post(url,json={'regions':[region],'image_b64':image})
                assert r.status_code==200,r.json
            save_region();region['id']=client.get(url).json[0]['id']
            cache=vc.CacheManager(base.DB_PATH,pid);assert cache.initial_load()
            rev=cache._db_version();region['threshold']=.99;save_region()
            assert cache._db_version()>rev
            rev=cache._db_version()
            client.put(f'/api/products/{other}',json={'serial':'OTHER','name':'changed'})
            assert cache._db_version()==rev,'unrelated product must not interrupt inspection'
            assert client.post(url,json={'regions':[]}).status_code==200
            assert cache.force_reload() and not cache.get().regions
            region.pop('id');save_region();region['id']=client.get(url).json[0]['id']
            def save_steps(name):
                r=client.post(f'/api/products/{pid}/sop-definition',json={'config':{'enabled':True},'steps':[
                    {'name':name,'enabled':True,'required':True,'min_consecutive_hits':1,'hold_ms':0,
                     'samples':[{'source_region_id':region['id'],'sample_role':'OK'}]}]})
                assert r.status_code==200,r.json
            save_steps('Original')
            video=root/'source.avi';writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),15,(160,120))
            assert writer.isOpened()
            for _ in range(45):writer.write(frame)
            writer.release()
            rt=EdgeRuntime(base,root/'edge.ini');web.EDGE=rt
            rt.update_config({'backend':'opencv','source':str(video),'rotation':0,'min_free_mb':0,'infer_fps':5,'product_id':pid})
            assert rt.start()['success'];wait(lambda:rt.status()['inference_ready'])
            assert rt.engine.steps_cfg[0]['name']=='Original'
            assert client.get(f'/api/edge/template-frame?product_id={pid}').status_code==200
            assert rt.start_recording('raw')['success']
            cfg_before=rt.cfg.public().copy()
            assert client.put('/api/edge/config',json={'width':3840,'restart':True}).status_code==409
            assert rt.cfg.public()==cfg_before
            assert client.post('/api/edge/stop').status_code==409
            assert not rt.start_recording('result')['success']
            assert client.post('/api/edge/apply',json={'product_id':pid}).status_code==409
            assert rt.stop_recording()['success']
            save_steps('New definition')
            wait(lambda:rt.status()['definition_pending'])
            assert not rt.status()['inference_ready']
            assert not rt.snapshot('result')['success']
            backend_before_apply = rt.backend
            thread_before_apply = rt.thread
            applied=client.post('/api/edge/apply',json={'product_id':pid})
            assert applied.status_code==200,applied.json
            wait(lambda:rt.status()['inference_ready'])
            assert rt.backend is backend_before_apply and rt.thread is thread_before_apply
            assert rt.engine.steps_cfg[0]['name']=='New definition'
            assert not rt.definition_pending
            switched=client.post('/api/edge/apply',json={'product_id':other})
            assert switched.status_code==200,switched.json
            assert rt.cfg.product_id==other and rt.backend is backend_before_apply
            restored=client.post('/api/edge/apply',json={'product_id':pid})
            assert restored.status_code==200,restored.json
            assert rt.cfg.product_id==pid and rt.backend is backend_before_apply
            assert rt.stop()['success']
            assert not rt.status()['has_raw'] and not rt.snapshot('raw')['success']
            assert client.get(f'/api/edge/template-frame?product_id={pid}').status_code==409
            changed=client.post('/api/edge/apply',json={'product_id':other})
            assert changed.status_code==200 and rt.cfg.product_id==other
            rt.update_config({'product_id':9999})
            rt.start();wait(lambda:rt.status()['has_raw'])
            assert not rt.status()['inference_ready'] and rt.status()['warning']
            rt.stop(force=True)
            # A failed stop must not start a second camera owner.
            starts=[]
            rt.stop=lambda: {'success':False,'error':'timeout'}
            rt.start=lambda: starts.append(1)
            assert not rt.restart()['success'] and not starts
            print('EDGE_CONSISTENCY_TEST PASS: revision, empty cache, independent products, live apply, recording guards, stale frames, no login')
        finally:
            if web.EDGE is not original_edge:
                web.EDGE.stop_event.set()
                if web.EDGE.thread: web.EDGE.thread.join(8)
            base.DB_PATH=original_db;web.EDGE=original_edge
            web.INSPECTIONS.path=original_history

if __name__=='__main__':main()
