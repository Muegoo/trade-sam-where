import copy
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from trade_app.broker import BrokerError, BrokerRejected, MT5Broker, SimulationBroker
from trade_app.monitor import Monitor
from trade_app.server import create_app
from trade_app.store import DEFAULTS, Store


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'test.db')
        self.broker=SimulationBroker('XAUUSD',self.store)
        self.monitor=Monitor(self.store,self.broker)
        self.snap=self.broker.snapshot()
        price=self.snap['tick']['ask']
        self.signal={'key':'ema','name':'EMA','enabled':True,'signal':'BUY',
                     'time':self.snap['bars'][-2]['time'],'entry':price,'sl':round(price-5,2),'tp':round(price+10,2)}
        self.old=self.signal|{'time':self.signal['time']-900}

    def tearDown(self):
        self.store.db.close();self.temp.cleanup()

    def start(self):self.monitor.start(self.snap,[self.old],DEFAULTS)

    def test_only_new_signal_and_no_duplicate_after_close(self):
        self.monitor.start(self.snap,[self.signal],DEFAULTS)
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        self.start()
        self.assertTrue(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        after=self.broker.snapshot()
        self.assertFalse(self.monitor.step(after,[self.signal],DEFAULTS))
        self.broker.close_position(after['positions'][0],after)
        self.assertFalse(self.monitor.step(self.broker.snapshot(),[self.signal],DEFAULTS))
        self.assertEqual(len(self.broker.data['positions']),0)

    def test_risk_and_kill_block_sending(self):
        self.start()
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS|{'max_spread':1}))
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS|{'kill_switch':True}))
        self.assertFalse(self.monitor.enabled)
        self.assertEqual(self.broker.data['positions'],[])

    def test_real_account_account_switch_expiry(self):
        self.snap['account']['is_demo']=False
        with self.assertRaises(BrokerError):self.start()
        self.snap['account']['is_demo']=True
        self.start()
        self.assertFalse(self.monitor.step(self.snap|{'scope':'different'},[self.signal],DEFAULTS))
        self.assertFalse(self.monitor.enabled)
        self.start();self.monitor.expires=time.time()-1
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        self.assertFalse(self.monitor.enabled)

    def test_single_exposure_and_conflicting_signals(self):
        self.start()
        self.snap['account']['margin_mode']=0
        conflict=self.signal|{'key':'smc','signal':'SELL','sl':self.signal['entry']+5}
        self.assertFalse(self.monitor.step(self.snap,[self.signal,conflict],DEFAULTS))
        self.snap['orders']=[{'symbol':'XAUUSD'} for _ in range(DEFAULTS['max_orders'])]
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        self.snap['orders']=[]
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS),'Signals seen while exposed must not replay')

    def test_hedging_strategies_can_trade_opposite_directions_independently(self):
        settings=DEFAULTS|{'max_lot':.01}
        conflict=self.signal|{'key':'smc','signal':'SELL','sl':self.signal['entry']+5}
        self.monitor.start(self.snap,[self.old,self.old|{'key':'smc'}],settings)
        self.snap['account']['margin_mode']=2
        self.assertTrue(self.monitor.step(self.snap,[self.signal,conflict],settings))
        self.assertTrue(self.monitor.step(self.broker.snapshot(),[self.signal,conflict],settings))
        self.assertEqual({p['type'] for p in self.broker.data['positions']},{0,1})
        self.assertEqual({p['magic'] for p in self.broker.data['positions']},{26001,26003})

    def test_one_open_position_per_strategy_and_status_explains_it(self):
        self.start();self.assertTrue(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        snap=self.broker.snapshot()
        self.assertFalse(self.monitor.step(snap,[self.signal|{'time':self.signal['time']+900}],DEFAULTS))
        status=self.monitor.status()['strategy_status'][0]
        self.assertEqual(status['status'],'HOLDING')
        self.assertEqual(status['open_count'],1)
        self.assertEqual(len(self.broker.data['positions']),1)

    def test_fewer_samples_gets_first_slot(self):
        settings=DEFAULTS|{'max_lot':.01}
        other=self.signal|{'key':'smc'}
        self.monitor.start(self.snap,[self.old,self.old|{'key':'smc'}],settings)
        self.snap['deals']=[self.broker.data['deals'][0]|{'magic':26003,'entry':0,'type':0,'position_id':1,'symbol':'XAUUSD'}]
        self.assertTrue(self.monitor.step(self.snap,[self.signal,other],settings))
        self.assertEqual(self.broker.data['positions'][0]['magic'],26001)

    def test_per_strategy_wait_and_risk_reasons(self):
        self.start()
        self.assertFalse(self.monitor.step(self.snap,[self.signal|{'signal':'WAIT','reason':'Await pullback'}],DEFAULTS))
        self.assertEqual(self.monitor.status()['strategy_status'][0]['reason'],'Await pullback')
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS|{'max_spread':1}))
        self.assertEqual(self.monitor.status()['strategy_status'][0]['status'],'RISK_BLOCKED')

    def test_closing_one_strategy_does_not_suppress_another(self):
        self.start();self.assertTrue(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        snap=self.broker.snapshot();self.monitor.step(snap,[self.signal],DEFAULTS)
        self.broker.close_position(snap['positions'][0],snap)
        self.assertTrue(self.monitor.step(self.broker.snapshot(),[self.signal,self.signal|{'key':'smc'}],DEFAULTS))
        self.assertEqual(self.broker.data['positions'][0]['magic'],26001)

    def test_chasing_and_old_history_blocked(self):
        self.start()
        chased=self.signal|{'entry':self.signal['entry']-100}
        self.assertFalse(self.monitor.step(self.snap,[chased],DEFAULTS))
        old=self.signal|{'time':self.signal['time']+1}
        self.assertFalse(self.monitor.step(self.snap,[old],DEFAULTS))
        self.assertEqual(self.broker.data['positions'],[])

    def test_unknown_result_halts_and_durable_claim_blocks_restart(self):
        self.start()
        def fail(*args):raise BrokerError('Unknown transport result')
        self.broker.send=fail
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        self.assertFalse(self.monitor.enabled)
        self.assertTrue(self.store.unresolved_auto(self.snap['scope']))
        restarted=Monitor(self.store,self.broker)
        self.assertFalse(restarted.enabled)
        with self.assertRaises(BrokerError):restarted.start(self.snap,[self.old],DEFAULTS)

    def test_poll_without_browser_and_durable_deduplication(self):
        self.start()
        self.assertTrue(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        restarted=Monitor(self.store,self.broker)
        restarted.start(self.snap,[self.old],DEFAULTS)
        self.assertFalse(restarted.step(self.snap,[self.signal],DEFAULTS))
        self.assertEqual(len(self.broker.data['positions']),1)

    def test_missing_tick_changes_pauses(self):
        self.start();self.monitor.step(self.snap,[self.old],DEFAULTS)
        self.monitor.quote_changed=time.monotonic()-181
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS))

    def test_five_positions_from_distinct_signals_and_no_sixth(self):
        settings=DEFAULTS|{'max_lot':.01,'max_orders':5}
        keys=['ema','smc','supply','bollinger','asian']
        self.monitor.start(self.snap,[self.old|{'key':k} for k in keys],settings)
        for key in keys:
            self.assertTrue(self.monitor.step(self.broker.snapshot(),[self.signal|{'key':key}],settings),self.monitor.message)
        self.assertEqual(len(self.broker.data['positions']),5)
        self.assertFalse(self.monitor.step(self.broker.snapshot(),[self.signal|{'key':'ai'}],settings))
        self.assertEqual(len(self.broker.data['positions']),5)
        self.assertEqual(self.monitor.status()['max_positions'],5)

    def test_netting_still_blocks_adding_to_existing_symbol(self):
        self.start();self.assertTrue(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        snap=self.broker.snapshot();snap['account']['margin_mode']=0
        self.assertFalse(self.monitor.step(snap,[self.signal|{'key':'smc'}],DEFAULTS))
        self.assertEqual(len(self.broker.data['positions']),1)

    def test_total_session_limit_does_not_refill_after_positions_close(self):
        settings=DEFAULTS|{'max_lot':.01}
        self.monitor.start(self.snap,[self.old],settings,max_trades=5)
        for key in ['ema','smc','supply','bollinger','asian']:
            snap=self.broker.snapshot()
            self.assertTrue(self.monitor.step(snap,[self.signal|{'key':key}],settings))
            snap=self.broker.snapshot()
            self.broker.close_position(snap['positions'][0],snap)
        self.assertFalse(self.monitor.step(self.broker.snapshot(),[self.signal|{'key':'smc'}],settings))
        self.assertFalse(self.monitor.enabled)
        self.assertEqual(self.monitor.trades_used,5)
        self.assertTrue(self.monitor.close_on_expiry)
        restarted=Monitor(self.store,self.broker)
        self.assertEqual(restarted.status()['trades_used'],5)
        self.assertEqual(restarted.status()['max_trades'],5)
        self.assertFalse(restarted.enabled)

    def test_unknown_send_consumes_slot_but_explicit_rejection_does_not(self):
        self.monitor.start(self.snap,[self.old],DEFAULTS,max_trades=1)
        original=self.broker.send
        def reject(*args):raise BrokerRejected('Rejected before submission')
        self.broker.send=reject
        self.assertFalse(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        self.assertEqual(self.monitor.trades_used,0)
        def unknown(*args):raise BrokerError('Unknown transport result')
        self.broker.send=unknown
        self.assertFalse(self.monitor.step(self.snap,[self.signal|{'key':'smc'}],DEFAULTS))
        self.assertEqual(self.monitor.trades_used,1)
        self.assertFalse(self.monitor.enabled)
        self.assertEqual(Monitor(self.store,self.broker).trades_used,1)
        self.broker.send=original

    def test_invalid_session_limit_rejected(self):
        for value in [0,-1,101,True,1.5]:
            with self.assertRaises(BrokerError):
                self.monitor.start(self.snap,[self.old],DEFAULTS,max_trades=value)

    def test_unlimited_session_can_open_more_than_five_trades_without_changing_position_cap(self):
        settings=DEFAULTS|{'max_lot':.01,'max_orders':5}
        self.monitor.start(self.snap,[self.old],settings,max_trades=None)
        clock=time.time()
        for index in range(7):
            with patch('trade_app.broker.time.time',return_value=clock+index*900):
                snap=self.broker.snapshot()
                price=snap['tick']['ask']
                signal=self.signal|{'time':snap['bars'][-2]['time'],'entry':price,
                                    'sl':round(price-5,2),'tp':round(price+10,2)}
                self.assertTrue(self.monitor.step(snap,[signal],settings),self.monitor.message)
                after=self.broker.snapshot()
                self.broker.close_position(after['positions'][0],after)
        self.assertEqual(self.monitor.trades_used,7)
        self.assertIsNone(self.monitor.max_trades)
        self.assertTrue(self.monitor.enabled)
        self.assertEqual(self.monitor.max_positions,5)


    def make_expired_position(self):
        self.start()
        self.assertTrue(self.monitor.step(self.snap,[self.signal],DEFAULTS))
        self.monitor.expires=int(time.time()-1)
        self.monitor.persist_session()

    def test_expiry_closes_app_positions_even_during_kill(self):
        self.make_expired_position()
        foreign=copy.deepcopy(self.broker.data['positions'][0]);foreign.update(ticket=999,identifier=999,magic=999)
        self.broker.data['positions'].append(foreign)
        self.assertTrue(self.monitor.step(self.broker.snapshot(),[self.signal],DEFAULTS|{'kill_switch':True}))
        self.assertFalse(self.monitor.enabled)
        self.assertEqual([p['ticket'] for p in self.broker.data['positions']],[999])
        self.monitor.step(self.broker.snapshot(),[],DEFAULTS)
        self.assertFalse(self.monitor.close_on_expiry)

    def test_expiry_schedule_survives_restart_and_manual_stop(self):
        self.make_expired_position()
        restarted=Monitor(self.store,self.broker)
        self.assertFalse(restarted.enabled)
        self.assertTrue(restarted.close_on_expiry)
        restarted.stop()
        self.assertTrue(restarted.step(self.broker.snapshot(),[],DEFAULTS))
        self.assertEqual(self.broker.data['positions'],[])

    def test_expiry_wrong_account_waits_without_closing(self):
        self.make_expired_position()
        self.assertFalse(self.monitor.step(self.broker.snapshot()|{'scope':'different'},[],DEFAULTS))
        self.assertEqual(len(self.broker.data['positions']),1)
        self.assertTrue(self.monitor.close_on_expiry)

    def test_expiry_market_rejection_retries_but_unknown_does_not(self):
        self.make_expired_position()
        real_close=self.broker.close_position
        def rejected(*args):raise BrokerRejected('Market closed before send')
        self.broker.close_position=rejected
        self.assertFalse(self.monitor.step(self.broker.snapshot(),[],DEFAULTS))
        self.monitor.close_retry_at={}
        self.broker.close_position=real_close
        self.assertTrue(self.monitor.step(self.broker.snapshot(),[],DEFAULTS))
        self.assertEqual(self.broker.data['positions'],[])

    def test_expiry_unknown_close_not_sent_twice(self):
        self.make_expired_position()
        calls=[]
        def unknown(*args):calls.append(1);raise BrokerError('Unknown close result')
        self.broker.close_position=unknown
        self.assertFalse(self.monitor.step(self.broker.snapshot(),[],DEFAULTS))
        self.monitor.close_retry_at={}
        self.assertFalse(self.monitor.step(self.broker.snapshot(),[],DEFAULTS))
        self.assertEqual(len(calls),1)

    def test_expiry_partial_fill_closes_remaining_volume(self):
        self.make_expired_position()
        real_close=self.broker.close_position
        def partial(position,snap):
            self.broker.data['positions'][0]['volume']=round(position['volume']/2,2)
            return {'ok':True,'volume':position['volume']/2,'message':'Partial close'}
        self.broker.close_position=partial
        self.assertTrue(self.monitor.step(self.broker.snapshot(),[],DEFAULTS))
        self.broker.close_position=real_close
        self.assertTrue(self.monitor.step(self.broker.snapshot(),[],DEFAULTS))
        self.assertEqual(self.broker.data['positions'],[])


class CloseAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(db_path=Path(self.temp.name)/'test.db')
        self.client=TestClient(self.app)
        self.app.state.refresh()
        self.state=self.client.get('/api/state').json()
        self.headers={'X-Trade-Token':self.state['token']}
        snap=self.app.state.broker.snapshot();price=snap['tick']['ask']
        self.app.state.broker.send({'side':'BUY','kind':'MARKET','entry':price,'lot':.01,'sl':price-10,'tp':price+20,'strategy':'manual'},snap)
        self.app.state.refresh()
        self.position=self.client.get('/api/state').json()['positions'][0]
        self.body={'scope':self.state['scope'],'request_id':'close-test-request-0001','identifier':self.position['identifier'],
                   'volume':self.position['volume'],'side':self.position['type']}

    def tearDown(self):
        self.client.close();self.app.state.store.db.close();self.temp.cleanup()

    def close(self,body=None):
        return self.client.post(f"/api/positions/{self.position['ticket']}/close",json=body or self.body,headers=self.headers)

    def test_early_close_works_during_kill_and_is_idempotent(self):
        self.client.post('/api/kill',json={'active':True},headers=self.headers)
        result=self.close();self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.json()['ok'])
        self.assertEqual(self.close().json(),result.json())
        self.assertEqual(self.app.state.broker.data['positions'],[])
        outs=[d for d in self.app.state.broker.data['deals'] if d['position_id']==self.position['ticket'] and d['entry']==1]
        self.assertEqual(len(outs),1)

    def test_close_revalidates_scope_volume_identifier_and_side(self):
        for change in ({'scope':'other'},{'volume':.02},{'identifier':99},{'side':1}):
            self.assertEqual(self.close(self.body|change).status_code,409)
        self.assertEqual(len(self.app.state.broker.data['positions']),1)

    def test_monitor_endpoint_and_kill_stop(self):
        result=self.client.post('/api/monitor/start',json={'scope':self.state['scope'],'hours':1},headers=self.headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.json()['enabled'])
        self.client.post('/api/kill',json={'active':True},headers=self.headers)
        self.assertFalse(self.client.get('/api/state').json()['monitor']['enabled'])
        self.assertEqual(len(self.app.state.broker.data['positions']),1)

    def test_monitor_api_accepts_total_limit_and_rejects_invalid_limit(self):
        for value in [0,101,True,1.5]:
            result=self.client.post('/api/monitor/start',json={'scope':self.state['scope'],'max_trades':value},headers=self.headers)
            self.assertEqual(result.status_code,422,result.text)
        result=self.client.post('/api/monitor/start',json={'scope':self.state['scope'],'max_trades':5},headers=self.headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['max_trades'],5)
        self.assertEqual(result.json()['trades_used'],0)


class MT5CloseTests(unittest.TestCase):
    def test_close_uses_position_ticket_full_volume_and_opposite_side(self):
        sent=[]
        m=SimpleNamespace(ORDER_TYPE_BUY=0,ORDER_TYPE_SELL=1,ORDER_FILLING_FOK=0,
            ORDER_FILLING_IOC=1,ORDER_FILLING_RETURN=2,SYMBOL_TRADE_EXECUTION_MARKET=2,
            TRADE_ACTION_DEAL=1,ORDER_TIME_GTC=0,TRADE_RETCODE_DONE=10009,
            TRADE_RETCODE_PLACED=10008,TRADE_RETCODE_DONE_PARTIAL=10010)
        m.symbol_info=lambda s:SimpleNamespace(filling_mode=2,trade_exemode=2)
        m.symbol_info_tick=lambda s:SimpleNamespace(time=time.time(),bid=2600,ask=2600.2)
        m.order_check=lambda r:SimpleNamespace(retcode=0)
        m.order_send=lambda r:sent.append(r) or SimpleNamespace(retcode=10010,comment='partial',order=5,deal=6,volume=.01,price=r['price'])
        broker=MT5Broker.__new__(MT5Broker);broker.mt5=m
        result=broker.close_position({'symbol':'XAUUSD','ticket':123,'volume':.02,'type':0,'magic':0},{})
        self.assertTrue(result['ok'])
        self.assertEqual(sent[0]['position'],123)
        self.assertEqual(sent[0]['type'],1)
        self.assertEqual(sent[0]['price'],2600)
        self.assertEqual(sent[0]['volume'],.02)


if __name__=='__main__':unittest.main()
