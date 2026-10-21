import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from trade_app.broker import BrokerError, MT5Broker
from trade_app.session_stats import SessionRecorder


class SessionStatisticsTests(unittest.TestCase):
    def test_session_includes_midnight_exit_and_excludes_old_and_foreign_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)
            plan={'date':'2026-10-21','symbol':'TEST','expected_scope':'demo:test',
                  'session_requested_at':1000,'expires_at':2000}
            current={'connected':True,'scope':'demo:test','account':{'balance':1000,'equity':1000,'currency':'USD'},
                     'info':{'trade_contract_size':100,'currency_profit':'USD'},
                     'tick':{'time':1010,'bid':100,'ask':100.1},'bars':[],
                     'positions':[{'ticket':1,'identifier':1,'symbol':'TEST','magic':26003,
                                   'price_open':100,'volume':.01,'sl':95,'tp':110}], 'journal':[]}
            recorder=SessionRecorder(output,plan,current)
            def deal(pid,at,entry,net=0,magic=26003):
                return {'ticket':pid*10+entry,'position_id':pid,'time':at,'entry':entry,
                        'net':net,'profit':net,'type':0 if entry==0 else 1,'volume':.01,
                        'price':100 if entry==0 else 110,'symbol':'TEST','magic':magic,
                        'strategy':'EMA','side':'BUY','local_time':'test','comment':'test'}
            current['journal']=[deal(1,1005,0),deal(2,900,0),deal(3,1006,0,magic=999)]
            recorder.capture(current,1010)
            current['journal'] += [deal(1,2001,1,10),deal(2,2001,1,500),deal(3,2001,1,300,magic=999)]
            current['positions']=[]
            current['account']={'balance':1810,'equity':1810,'currency':'USD'}
            current['tick']['time']=2002
            result=recorder.capture(current,2002)
            self.assertEqual(result['session_entries'],1)
            self.assertEqual(result['closed_positions'],1)
            self.assertEqual(result['session_net'],10)
            self.assertEqual(result['sum_r'],2)
            self.assertEqual(result['session_metrics']['trades'],1)
            saved=json.loads((output/'session-results.json').read_text())
            self.assertEqual({r['position_id'] for r in saved['deals']},{1})
            self.assertIn('2001',(output/'session-trades.csv').read_text(encoding='utf-8-sig'))

    def test_connection_events_record_gap_once_without_fabricating_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            plan={'expected_scope':'demo:test','symbol':'TEST','session_requested_at':1,
                  'expires_at':10000,'date':'test'}
            state={'connected':True,'scope':'demo:test','account':{'balance':1000,'equity':1000},
                   'tick':{'time':100,'bid':100,'ask':101},'journal':[],'positions':[]}
            recorder=SessionRecorder(Path(directory),plan,state)
            recorder.capture(state,100)
            recorder.capture({'connected':False,'error':'IPC timeout'},110)
            recorder.capture({'connected':False,'error':'IPC timeout'},120)
            state['tick']['time']=140
            result=recorder.capture(state,140)
            self.assertEqual(result['disconnected_seconds_observed'],30)
            self.assertEqual(len(recorder.quotes),2)
            self.assertEqual(len((Path(directory)/'connection-events.jsonl').read_text().splitlines()),3)


class ReconnectTests(unittest.TestCase):
    def test_failed_ipc_is_discarded_and_reconnect_is_backed_off_without_orders(self):
        broker=object.__new__(MT5Broker)
        broker.mt5=Mock()
        broker.mt5.initialize.side_effect=[False,True]
        broker.mt5.last_error.return_value=(-10005,'IPC timeout')
        with patch('trade_app.broker.time.monotonic',return_value=10):
            with self.assertRaises(BrokerError):broker.connect()
            with self.assertRaises(BrokerError):broker.connect()
        broker.mt5.shutdown.assert_called_once()
        self.assertEqual(broker.mt5.initialize.call_count,1)
        with patch('trade_app.broker.time.monotonic',return_value=14):broker.connect()
        self.assertTrue(broker._initialized)
        self.assertEqual(broker.mt5.initialize.call_count,2)
        broker.mt5.order_send.assert_not_called()


if __name__=='__main__':unittest.main()
