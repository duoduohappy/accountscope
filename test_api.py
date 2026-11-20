import tempfile,unittest
from pathlib import Path
from fastapi.testclient import TestClient
import app as service
from core import Store
from paper import PaperEngine
from unittest.mock import patch

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);service.store=Store(Path(self.tmp.name)/'api.sqlite');service.paper=PaperEngine(service.store);self.client=TestClient(service.app)
        env=patch.dict('os.environ',{'ACCOUNTSCOPE_PUBLIC_AUTO':'0'});env.start();self.addCleanup(env.stop)
    def test_demo_recovery_and_export(self):
        with self.client as c:
            self.assertEqual(c.get('/').status_code,200)
            for kind in ('normal','duplicate','gap','reconcile','stale'):
                self.assertEqual(c.post('/api/demo/'+kind).status_code,200)
            self.assertIsNone(c.get('/api/state?scope=demo').json()['equity'])
            c.post('/api/demo/recover');self.assertTrue(c.get('/api/state?scope=demo').json()['complete'])
            self.assertTrue(c.post('/api/demo/restart').json()['matched'])
            self.assertTrue(c.get('/api/state?scope=demo').json()['events'])
    def test_origin_and_unknown_collector(self):
        with self.client as c:
            self.assertEqual(c.post('/api/demo/normal',headers={'Origin':'https://other.invalid'}).status_code,403)
            self.assertEqual(c.post('/api/collect/missing/once',json={}).status_code,400)
    def test_internal_websocket(self):
        with self.client as c:
            c.post('/api/demo/normal')
            with c.websocket_connect('/ws?scope=demo') as ws:self.assertEqual(len(ws.receive_json()['accounts']),2)
    def test_no_credentials_paper_business_loop(self):
        with self.client as c:
            c.post('/api/paper/mode',json={'mode':'offline'});c.post('/api/paper/refresh',json={})
            identifier=c.get('/api/state').json()['paper']['books'][0]['id']
            for side,qty,key in [('buy','.01','b'),('sell','.004','s')]:
                r=c.post('/api/paper/trade',json={'account':identifier,'side':side,'quantity':qty,'request_id':key});self.assertEqual(r.status_code,200,r.text)
            self.assertTrue(c.post('/api/paper/reconcile',json={'account':identifier}).json()['matched'])
            self.assertTrue(c.post('/api/paper/replay',json={'account':identifier}).json()['matched'])
            self.assertEqual(len(c.get('/api/export').json()['paper']['fills']),2)
if __name__=='__main__':unittest.main()
