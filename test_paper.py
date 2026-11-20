import asyncio,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from core import Store,number,packed
from paper import PaperEngine,parse_quote,VENUES

class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(Path(self.tmp.name)/'paper.db');self.p=PaperEngine(self.s)
        with self.s.connect() as c:c.execute("UPDATE paper_settings SET value='offline'")
        asyncio.run(self.p.refresh());self.id=self.p.accounts()[0]
    def test_fee_cost_basis_realized_and_account_equation(self):
        buy=self.p.trade(self.id,'buy','.01','buy');b=self.p.book(self.id)
        self.assertEqual(number(b['cost_basis']),number(buy['quantity'])*number(buy['price'])+number(buy['fee']))
        sell=self.p.trade(self.id,'sell','.004','sell');b=self.p.book(self.id);q=self.p.quote(b['venue']);value=number(b['cash'])+number(b['quantity'])*number(q['price'])
        self.assertEqual(value-number(b['initial']),number(b['realized'])+number(b['quantity'])*number(q['price'])-number(b['cost_basis']))
        self.assertEqual(number(b['fees']),number(buy['fee'])+number(sell['fee']))
    def test_duplicate_does_not_charge_twice(self):
        self.p.trade(self.id,'buy','.01','same');before=self.p.book(self.id);r=self.p.trade(self.id,'buy','.01','same')
        self.assertTrue(r['duplicate']);self.assertEqual(before,self.p.book(self.id))
        with self.assertRaises(ValueError):self.p.trade(self.id,'buy','.02','same')
    def test_budget_and_inventory_checks(self):
        with self.assertRaises(ValueError):self.p.trade(self.id,'buy','10','expensive')
        with self.assertRaises(ValueError):self.p.trade(self.id,'sell','.01','short')
        with self.assertRaises(ValueError):self.p.trade(self.id,'buy','0.000000001','precision')
    def test_stale_quote_rejects_trade_and_holding_refresh(self):
        self.p.trade(self.id,'buy','.01','buy');q=self.p.quote(self.p.book(self.id)['venue']);q['timestamp']-=120
        with self.s.connect() as c:c.execute('INSERT INTO public_quotes(mode,venue,body) VALUES(?,?,?)',('offline',q['venue'],packed(q)))
        with self.assertRaises(ValueError):self.p.trade(self.id,'sell','.001','old')
        with self.assertRaises(ValueError):self.p.sync(self.id)
    def test_reconcile_corruption_recovery_and_replay(self):
        self.p.trade(self.id,'buy','.01','buy');self.assertTrue(self.p.reconcile(self.id)['matched'])
        self.assertFalse(self.p.fault('reconcile')['matched']);self.assertFalse(self.s.replay(self.id)['matched'])
        self.p.fault('recover');self.assertTrue(self.p.reconcile(self.id)['matched']);self.assertTrue(self.s.replay(self.id)['matched'])
    def test_modes_have_separate_books_and_history(self):
        self.p.trade(self.id,'buy','.01','buy')
        with self.s.connect() as c:c.execute("UPDATE paper_settings SET value='public'")
        self.p.initialize();self.assertEqual(self.p.book(self.p.accounts()[0])['quantity'],'0')
        self.assertEqual(self.s.view('paper-public')['history'],[])
        with self.assertRaises(ValueError):self.p.book(self.id)
    def test_public_failure_never_falls_back_to_synthetic(self):
        with self.s.connect() as c:c.execute("UPDATE paper_settings SET value='public'")
        with patch('paper.requests.get',side_effect=ConnectionError):asyncio.run(self.p.refresh())
        self.assertTrue(all(b['quote'] is None for b in self.p.view()['books']));self.assertEqual(len(self.p.errors),len(VENUES))
    def test_public_parsers_timestamp_symbol_and_source(self):
        now=time.time();p={'retCode':0,'time':int(now*1000),'result':{'list':[{'symbol':'BTCUSDT','lastPrice':'65000'}]}}
        self.assertEqual(parse_quote('bybit','BTC/USDT',p,now)['source'],'real_public')
        p['time']-=120000
        with self.assertRaises(ValueError):parse_quote('bybit','BTC/USDT',p,now)
        with self.assertRaises(ValueError):parse_quote('binance','BTC/USDT',{'symbol':'ETHUSDT','lastPrice':'1','closeTime':now*1000},now)
    def test_restart_persists_venue_ledger_and_projection(self):
        self.p.trade(self.id,'buy','.01','buy');other=PaperEngine(Store(self.s.path));self.assertEqual(other.book(self.id),self.p.book(self.id));self.assertTrue(other.store.replay(self.id)['matched'])
    def test_valuation_uses_quote_age_not_just_snapshot_age(self):
        self.p.trade(self.id,'buy','.01','buy');q=self.p.quote(self.p.book(self.id)['venue']);q['timestamp']=time.time()-50
        with self.s.connect() as c:c.execute('INSERT INTO public_quotes(mode,venue,body) VALUES(?,?,?)',('offline',q['venue'],packed(q)))
        self.p.sync(self.id);now=time.time()
        with patch('core.time.time',return_value=now+15):self.assertFalse(self.s.view('paper-offline')['complete'])

if __name__=='__main__':unittest.main()
