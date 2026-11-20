import copy,tempfile,time,unittest
from pathlib import Path
from core import Store,number,normalize
from adapters import balance_snapshot

def event(seq=1,kind='snapshot',identifier=None):
    return {'account':'a','event_id':identifier or str(seq),'sequence':seq,'kind':kind,'timestamp':time.time(),'balances':[{'asset':'USDT','free':'100','locked':'0','debt':'0','usd_price':'1'}],'positions':[]}
class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(Path(self.tmp.name)/'db.sqlite');self.s.register('a','fixture','spot','s','demo')
    def test_decimal_and_nonfinite(self):
        self.assertEqual(number('.1')+number('.2'),number('.3'))
        for x in ('NaN','Infinity',True):
            with self.assertRaises(ValueError):number(x)
    def test_duplicate_and_collision(self):
        e=event();self.s.ingest(e);self.assertEqual(self.s.ingest(e)['status'],'duplicate');e['balances'][0]['free']='99'
        with self.assertRaises(ValueError):self.s.ingest(e)
        self.assertEqual(self.s.view()['equity'],'100')
    def test_gap_blocks_following_until_snapshot(self):
        self.s.ingest(event());self.assertEqual(self.s.ingest(event(3,'update'))['status'],'gap')
        self.assertIsNone(self.s.view()['equity']);self.assertEqual(self.s.ingest(event(2,'update'))['status'],'gap')
        self.s.ingest(event(4));self.assertTrue(self.s.view()['complete'])
    def test_old_sequence_and_event_time_do_not_rewind(self):
        e=event(3);self.s.ingest(e);self.assertEqual(self.s.ingest(event(2))['status'],'stale')
        older=event(4);older['timestamp']=e['timestamp']-10;self.assertEqual(self.s.ingest(older)['status'],'stale')
    def test_absolute_patch_is_not_a_balance_delta(self):
        self.s.ingest(event());e=event(2,'update');e['balances'][0]['free']='90';self.s.ingest(e);self.assertEqual(self.s.view()['equity'],'90')
    def test_snapshot_removes_absent_assets(self):
        self.s.ingest(event());e=event(2);e['balances']=[];self.s.ingest(e);self.assertEqual(self.s.view()['equity'],'0')
    def test_reconciliation_does_not_mutate_state(self):
        self.s.ingest(event());e=event(2);e['balances'][0]['free']='98';r=self.s.reconcile('a',e);self.assertFalse(r['matched']);self.assertEqual(self.s.view()['equity'],'100')
    def test_replay_persistence_and_gaps(self):
        self.s.ingest(event());self.s.ingest(event(3,'update'));self.s.ingest(event(4));reopened=Store(self.s.path);self.assertTrue(reopened.replay('a')['matched'])
    def test_missing_price_staleness_and_mixed_modes(self):
        e=event();e['balances'][0]['usd_price']=None;self.s.ingest(e);self.assertIsNone(self.s.view()['equity'])
        e=event(2);e['timestamp']=time.time()-120;self.s.ingest(e);self.assertFalse(self.s.view()['complete'])
        self.s.ingest(event(3));self.s.register('live','fixture','spot','s','live');e=event();e['account']='live';self.s.ingest(e);self.assertIsNone(self.s.view()['equity'])
    def test_linear_pnl_and_unknown_margin(self):
        s=Store(Path(self.tmp.name)/'swap.sqlite');s.register('a','fixture','linear_swap','s','demo');e=event();e['positions']=[{'symbol':'X/USDT','side':'short','quantity':'2','entry':'10','mark':'8','multiplier':'3','linear':True}];s.ingest(e)
        a=s.view()['accounts'][0];self.assertEqual(number(a['equity']),number(112));self.assertIsNone(a['margin_ratio']);self.assertEqual(number(a['gross']),number(48))
    def test_invalid_products_and_positions(self):
        e=event();e['positions']=[{'symbol':'X','side':'long','quantity':'1','entry':'1','mark':'1','linear':False}]
        with self.assertRaises(ValueError):normalize(e)
        e=event();e['balances'].append(e['balances'][0])
        with self.assertRaises(ValueError):normalize(e)
    def test_ccxt_snapshot_validates_totals(self):
        raw={'free':{'USDT':99},'used':{'USDT':1},'total':{'USDT':100}};e=balance_snapshot(raw,{'USDT':'1'},'a',1);self.s.ingest(e);self.assertEqual(number(self.s.view()['equity']),number(100));raw['total']['USDT']=101
        with self.assertRaises(ValueError):balance_snapshot(raw,{},'a',2)
if __name__=='__main__':unittest.main()
