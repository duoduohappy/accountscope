import os,unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import app
import test_api

class DeploymentTests(unittest.TestCase):
    setUp=test_api.ApiTests.setUp
    def test_health_and_https_origin(self):
        with self.client as c:
            self.assertEqual(c.get('/healthz').json()['status'],'ok')
            self.assertEqual(c.post('/api/paper/mode',json={'mode':'offline'},headers={'Origin':'https://testserver'}).status_code,200)
            self.assertEqual(c.post('/api/paper/mode',json={'mode':'offline'},headers={'Origin':'https://testserver.evil.invalid'}).status_code,403)
    def test_free_hosted_guard_and_ephemeral_notice(self):
        with patch.dict(os.environ,{'ACCOUNTSCOPE_HOSTED_DEMO':'1','ACCOUNTSCOPE_EPHEMERAL':'1'}):
            app.write_windows.clear()
            with self.client as c:
                self.assertEqual(c.post('/api/collect/private/start',json={}).status_code,403)
                self.assertTrue(c.get('/api/state').json()['deployment']['ephemeral'])
                with c.websocket_connect('/ws',headers={'Origin':'https://testserver'}) as ws:self.assertTrue(ws.receive_json()['deployment']['hosted_demo'])
                app.write_windows['shared']=[app.time.monotonic()]*60
                self.assertEqual(c.post('/api/paper/mode',json={'mode':'offline'}).status_code,429)
            app.write_windows.clear()
    def test_blueprint_free_no_billable_resources(self):
        from pathlib import Path
        s=(Path(__file__).parent/'render.yaml').read_text(encoding='utf-8')
        self.assertIn('plan: free',s);self.assertNotIn('disk:',s);self.assertNotIn('databases:',s)
        self.assertIn('healthCheckPath: /healthz',s)

if __name__=='__main__':unittest.main()
