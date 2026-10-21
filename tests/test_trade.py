import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from trade_app.analytics import APP_MANUAL_MAGIC, MAGICS, analytics, closed_trades, journal, metrics
from trade_app.broker import BrokerError, MT5Broker, SimulationBroker
from trade_app.risk import day_net, preview
from trade_app.server import create_app
from trade_app.store import DEFAULTS, Store
from trade_app.strategies import scan


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/"test.db")
        self.broker = SimulationBroker("XAUUSD", self.store)
        self.snap = self.broker.snapshot()
        price = self.snap["tick"]["ask"]
        self.order = {"side": "BUY", "kind": "MARKET", "entry": None,
                      "sl": round(price-5,2), "tp": round(price+10,2), "lot": .02, "strategy": "manual"}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def result(self, settings=None, snap=None, order=None):
        return preview(order or self.order, snap or self.snap, settings or DEFAULTS, self.broker)

    def test_market_auto_sizing_and_rr(self):
        p = self.result(order=self.order|{"lot": None})
        self.assertTrue(p["allowed"], p["errors"])
        self.assertLessEqual(p["lot"], DEFAULTS["max_lot"])
        self.assertAlmostEqual(p["rr"], 2)
        self.assertLessEqual(p["risk_percent"], DEFAULTS["risk_percent"])

    def test_kill_and_real_account_block(self):
        self.assertFalse(self.result(settings=DEFAULTS|{"kill_switch": True})["allowed"])
        snap = copy.deepcopy(self.snap)
        snap["account"]["is_demo"] = False
        self.assertFalse(self.result(snap=snap)["allowed"])

    def test_stale_quotes_and_spread(self):
        snap=copy.deepcopy(self.snap);snap["tick"]["time"] = int(time.time())-500
        self.assertFalse(self.result(snap=snap)["allowed"])
        self.assertFalse(self.result(settings=DEFAULTS|{"max_spread": 1})["allowed"])
        snap=copy.deepcopy(self.snap);snap['tick']['time']=int(time.time())+10800
        self.assertFalse(self.result(snap=snap)['allowed'])

    def test_lot_steps_max_and_risk(self):
        for lot in (.025, 1):
            self.assertFalse(self.result(order=self.order|{"lot":lot})["allowed"])
        self.assertFalse(self.result(settings=DEFAULTS|{"risk_percent": .001})["allowed"])

    def test_stop_and_limit_directions(self):
        ask=self.snap["tick"]["ask"]
        self.assertFalse(self.result(order=self.order|{"kind":"LIMIT", "entry":round(ask+1,2)})["allowed"])
        self.assertFalse(self.result(order=self.order|{"kind":"STOP", "entry":round(ask-1,2)})["allowed"])
        self.assertTrue(self.result(order=self.order|{"kind":"LIMIT", "entry":round(ask-1,2)})["allowed"])
        self.assertTrue(self.result(order=self.order|{"kind":"STOP", "entry":round(ask+1,2)})["allowed"])

    def test_daily_loss_includes_entry_costs_and_excludes_deposits(self):
        snap=copy.deepcopy(self.snap)
        snap["deals"]=[{"type":0,"time":int(time.time()),"profit":-200,"commission":-1},
                       {"type":2,"time":int(time.time()),"profit":10000}]
        self.assertEqual(day_net(snap["deals"]),-201)
        self.assertFalse(self.result(snap=snap)["allowed"])

    def test_existing_pending_reserves_downside(self):
        snap=copy.deepcopy(self.snap)
        snap["orders"]=[{"ticket":5,"type":2,"symbol":"XAUUSD","price_open":2600,"volume":.1,"sl":2580.5,"profit":0}]
        result=self.result(snap=snap)
        self.assertFalse(result["allowed"])
        self.assertAlmostEqual(result["reserved_risk"],195)

    def test_unprotected_position_and_account_count_block(self):
        snap=copy.deepcopy(self.snap)
        snap["positions"]=[{"ticket":1,"type":0,"symbol":"XAUUSD","price_open":2600,"volume":.01,"sl":0,"profit":0}]
        self.assertFalse(self.result(snap=snap)["allowed"])
        self.assertFalse(self.result(snap=snap,settings=DEFAULTS|{"max_orders":1})["allowed"])

    def test_netting_existing_symbol_blocks(self):
        snap=copy.deepcopy(self.snap)
        snap["account"]["margin_mode"]=0
        snap["orders"]=[{"ticket":5,"type":2,"symbol":"XAUUSD","price_open":2600,"volume":.01,"sl":2595}]
        self.assertTrue(any("Netting" in x for x in self.result(snap=snap)["errors"]))


def deal(ticket, entry, volume, profit=0, commission=-1, pid=1, magic=0, side=0):
    return {"ticket":ticket,"order":ticket,"position_id":pid,"time":int(time.time())+ticket,
            "type":side,"entry":entry,"volume":volume,"profit":profit,"commission":commission,
            "swap":0,"fee":0,"price":2600,"symbol":"XAUUSD","magic":magic,"comment":""}


class AnalyticsTests(unittest.TestCase):
    def test_partial_closes_form_one_trade_with_entry_cost(self):
        rows=journal([deal(1,0,.1),deal(2,1,.04,10),deal(3,1,.06,-5)],{})
        trades=closed_trades(rows,[])
        self.assertEqual(len(trades),1)
        self.assertEqual(trades[0]["net"],2)
        self.assertEqual(metrics(trades)["trades"],1)

    def test_open_and_incomplete_histories_excluded(self):
        self.assertEqual(closed_trades(journal([deal(1,0,.1),deal(2,1,.04,10)],{}),[{"ticket":1}]),[])
        self.assertEqual(closed_trades(journal([deal(2,1,.1,10)],{}),[]),[])

    def test_reversal_cost_allocation(self):
        rows=journal([deal(1,0,.1),deal(2,2,.2,20,commission=-2,side=1),deal(3,1,.1,-5,side=0)],{})
        trades=closed_trades(rows,[])
        self.assertEqual([t["net"] for t in trades],[18,-7])
        self.assertEqual(sum(t["net"] for t in trades),sum(d["net"] for d in rows))

    def test_cashflow_and_magic_classification(self):
        deposit=deal(1,0,0,10000);deposit["type"]=2
        rows=journal([deposit,deal(2,0,.1,magic=APP_MANUAL_MAGIC),deal(3,0,.1,magic=99),deal(4,0,.1,magic=MAGICS["ema"])],{"99":"My EA"})
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[0]["source"],"Manual")
        self.assertEqual(rows[1]["strategy"],"My EA")
        self.assertEqual(rows[2]["strategy"],"EMA Trend Pullback")

    def test_drawdown_and_streak(self):
        m=metrics([{"net":v} for v in (20,-5,-10,3,-1)])
        self.assertEqual(m["drawdown"],15)
        self.assertEqual(m["max_loss_streak"],2)
        self.assertAlmostEqual(m["profit_factor"],23/16)

    def test_strategy_comparison_keeps_floating_separate_from_realized(self):
        rows=journal([deal(1,0,.01,magic=MAGICS['ema'])],{})
        result=analytics(rows,[{'ticket':1,'identifier':1,'magic':MAGICS['ema'],'volume':.01,'profit':9}],[])
        ema=next(s for s in result['strategies'] if s['strategy']=='EMA Trend Pullback')
        self.assertEqual(ema['open_count'],1)
        self.assertEqual(ema['floating_pnl'],9)
        self.assertEqual(ema['trades'],0)
        self.assertEqual(ema['net'],0)

    def test_scanner_disabling_and_valid_stop_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/"test.db")
            broker=SimulationBroker("XAUUSD",store)
            bars=broker.snapshot()["bars"][:-1]
            self.assertTrue(all(s["signal"]=="WAIT" for s in scan(bars,DEFAULTS|{"enabled_strategies":[]})))
            for s in scan(bars,DEFAULTS,{"validated":True,"up_probability":.8}):
                if s["signal"]=="BUY":self.assertTrue(s["sl"]<s["entry"]<s["tp"])
                if s["signal"]=="SELL":self.assertTrue(s["tp"]<s["entry"]<s["sl"])
            store.db.close()


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"app.db"
        self.app=create_app(db_path=self.path)
        self.client=TestClient(self.app)
        self.app.state.refresh()
        self.state=self.client.get('/api/state').json()
        self.headers={"X-Trade-Token":self.state["token"]}

    def tearDown(self):
        self.client.close()
        self.app.state.store.db.close()
        self.temp.cleanup()

    def proposal(self, kind="MARKET", strategy="manual"):
        price=self.app.state.broker.snapshot()["tick"]["ask"]
        return {"side":"BUY","kind":kind,"entry":round(price-1,2) if kind=="LIMIT" else None,
                "sl":round(price-5,2),"tp":round(price+10,2),"lot":.02,"strategy":strategy}

    def test_app_state_and_no_order_on_scan(self):
        self.assertTrue(self.state["connected"],self.state.get("error"))
        self.assertEqual(len(self.state["signals"]),6)
        self.assertEqual(self.state["orders"],[])
        self.assertEqual(self.state["positions"],[])
        self.assertEqual(self.client.get('/').status_code,200)

    def test_csrf_host_and_schema(self):
        self.assertEqual(self.client.post('/api/preview',json=self.proposal()).status_code,403)
        self.assertEqual(self.client.post('/api/preview',json=self.proposal(),headers=self.headers|{"Origin":"http://evil.example"}).status_code,403)
        self.assertEqual(self.client.get('/api/state',headers={"Host":"evil.example"}).status_code,403)
        self.assertEqual(self.client.post('/api/preview',json=self.proposal()|{"lot":-1},headers=self.headers).status_code,422)

    def test_preview_and_send_are_distinct_and_idempotent(self):
        order=self.proposal()
        p=self.client.post('/api/preview',json=order,headers=self.headers).json()
        self.assertTrue(p["allowed"],p)
        self.assertEqual(len(self.app.state.broker.data["positions"]),0)
        body=order|{"scope":p["scope"],"request_id":"same-request-00001"}
        r=self.client.post('/api/orders',json=body,headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        r2=self.client.post('/api/orders',json=body,headers=self.headers)
        self.assertEqual(r.json(),r2.json())
        self.assertEqual(len(self.app.state.broker.data["positions"]),1)

    def test_send_rechecks_kill_and_account_scope(self):
        order=self.proposal()|{"scope":"different","request_id":"scope-test-0000001"}
        self.assertEqual(self.client.post('/api/orders',json=order,headers=self.headers).status_code,409)
        self.client.post('/api/kill',json={"active":True},headers=self.headers)
        order["scope"]=self.state["scope"]
        self.assertEqual(self.client.post('/api/orders',json=order,headers=self.headers).status_code,409)

    def test_kill_cancels_app_pending_persists_and_allows_resume(self):
        order=self.proposal("LIMIT")|{"scope":self.state["scope"],"request_id":"kill-test-00000001"}
        r=self.client.post('/api/orders',json=order,headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        self.app.state.broker.data["orders"].append({"ticket":55,"type":2,"magic":999,"price_open":1,"sl":.5,"tp":2,"volume":.01,"symbol":"XAUUSD"})
        killed=self.client.post('/api/kill',json={"active":True},headers=self.headers).json()
        self.assertEqual(len(killed["cancelled"]),1)
        self.assertEqual([o["ticket"] for o in self.app.state.broker.data["orders"]],[55])
        second=Store(self.path)
        self.assertTrue(second.settings()["kill_switch"])
        second.db.close()
        self.client.post('/api/kill',json={"active":False},headers=self.headers)
        self.assertFalse(self.app.state.store.settings()["kill_switch"])

    def test_filter_csv_matches_journal_and_protects_formulas(self):
        self.app.state.broker.data["deals"][0]["comment"]="=HYPERLINK(1)"
        self.app.state.refresh()
        query='?source=Manual&hour_start=00:00&hour_end=23:59'
        rows=self.client.get('/api/journal'+query).json()
        export=self.client.get('/api/journal.csv'+query)
        import csv,io
        csv_rows=list(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff'))))
        self.assertEqual(len(rows),len(csv_rows))
        self.assertTrue(all(r["source"]=="Manual" for r in csv_rows))
        self.assertTrue(csv_rows[0]["comment"].startswith("'="))

    def test_settings_validation_and_store_scope(self):
        settings={k:v for k,v in DEFAULTS.items() if k!='kill_switch'}
        response=self.client.put('/api/settings',json=settings|{"asian_start":15,"asian_end":7},headers=self.headers)
        self.assertEqual(response.status_code,422)
        self.app.state.store.sample('other-account',100,{"balance":1,"equity":2})
        self.assertEqual(len(self.app.state.store.curve('other-account')),1)
        self.assertTrue(all(p["balance"]!=1 for p in self.state["curve"]))


class MT5AdapterTests(unittest.TestCase):
    def test_request_translation_order_check_and_partial_result(self):
        fake=SimpleNamespace(ORDER_TYPE_BUY=0,ORDER_TYPE_BUY_LIMIT=2,ORDER_TYPE_BUY_STOP=4,
             ORDER_FILLING_RETURN=2,ORDER_FILLING_FOK=0,ORDER_FILLING_IOC=1,
             SYMBOL_TRADE_EXECUTION_MARKET=2,TRADE_ACTION_DEAL=1,TRADE_ACTION_PENDING=5,
             ORDER_TIME_GTC=0,TRADE_RETCODE_DONE=10009,TRADE_RETCODE_PLACED=10008,TRADE_RETCODE_DONE_PARTIAL=10010)
        sent=[]
        fake.order_check=lambda request:SimpleNamespace(retcode=0)
        fake.order_send=lambda request:sent.append(request) or SimpleNamespace(retcode=10010,comment='Partial',order=10,deal=11,volume=.01,price=2600)
        broker=MT5Broker.__new__(MT5Broker);broker.mt5=fake;broker.symbol='XAUUSD'
        proposal={"side":"BUY","kind":"MARKET","lot":.02,"entry":2600,"sl":2595,"tp":2610,"strategy":"manual"}
        snap={"info":{"filling_mode":2,"trade_exemode":2}}
        self.assertTrue(broker.send(proposal,snap)["ok"])
        self.assertEqual(sent[-1]["type_filling"],1)
        self.assertEqual(sent[-1]["magic"],APP_MANUAL_MAGIC)
        broker.send(proposal|{"kind":"LIMIT"},snap)
        self.assertEqual(sent[-1]["type"],2)
        self.assertEqual(sent[-1]["type_filling"],2)
        fake.order_check=lambda request:SimpleNamespace(retcode=10016,comment='Invalid stops')
        with self.assertRaises(BrokerError):broker.send(proposal,snap)
        self.assertEqual(len(sent),2)


if __name__ == '__main__':
    unittest.main()
