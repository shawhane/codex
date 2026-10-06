import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess
from pantry.core import Pantry, TZ
from pantry.worker import run_once, send_openclaw

spec=importlib.util.spec_from_file_location('bridge',Path(__file__).resolve().parents[1]/'integrations/openclaw/pantry_bridge.py')
bridge=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.at=datetime(2026,10,6,9,30,tzinfo=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self):
        p=Pantry(self.root/'inventory.sqlite3')
        result=p.execute({'action':'add','request_id':'1','item':{'name':'牛奶','expiry':'2026-10-06','date_source':'user','basis':'测试'}},at=self.at)
        p.close()
        return result

    def test_bridge_isolation_and_restrictions(self):
        result=bridge.run({'action':'add','request_id':'x','item':{'name':'测试物品','category':'food','quantity':1,'unit':'件','storage':'unknown'}},self.root,test=True)
        self.assertEqual('test',result['profile'])
        self.assertEqual([],bridge.run({'action':'list'},self.root)['items'])
        with self.assertRaises(ValueError):
            bridge.run({'action':'ack','delivery_id':1},self.root)

    def test_send_ack_and_restart(self):
        self.add()
        sent=[]
        send=lambda cfg,txt:sent.append(txt)
        self.assertEqual(1,run_once(self.root,{},send,self.at)['sent'])
        run_once(self.root,{},send,self.at+timedelta(minutes=5))
        self.assertEqual(1,len(sent))
        self.assertTrue((self.root/'backups/2026-10-06.sqlite3').exists())
        with sqlite3.connect(self.root/'backups/2026-10-06.sqlite3') as db:
            self.assertEqual(1,db.execute('SELECT COUNT(*) FROM items').fetchone()[0])

    def test_ambiguous_send_no_ack_no_repeat(self):
        self.add()
        def broken(cfg,txt):
            raise TimeoutError('ambiguous transport')
        with self.assertRaises(TimeoutError):
            run_once(self.root,{},broken,self.at)
        result=run_once(self.root,{},broken,self.at+timedelta(minutes=5))
        self.assertEqual('needs_reconciliation',result['status'])
        p=Pantry(self.root/'inventory.sqlite3')
        self.assertEqual(1,len(p.execute({'action':'outbox'})['deliveries']))
        p.close()

    def test_crash_after_delivery_ack_recovers_without_send(self):
        self.add()
        run_once(self.root,{},at=self.at,dry_run=True)
        (self.root/'delivery-attempt.json').write_text(json.dumps({'state':'delivered','delivery_ids':[1],'at':self.at.isoformat()}))
        def forbidden(cfg,txt):
            self.fail('must not resend confirmed delivery')
        result=run_once(self.root,{},forbidden,self.at+timedelta(days=1))
        self.assertEqual('reconciled',result['status'])
        p=Pantry(self.root/'inventory.sqlite3')
        self.assertEqual('sent',p.db.execute('SELECT state FROM deliveries WHERE id=1').fetchone()[0])
        p.close()

    def test_quiet_hours_and_preview(self):
        self.add()
        def forbidden(cfg,txt):
            self.fail('must not send')
        self.assertEqual(0,run_once(self.root,{},forbidden,self.at.replace(hour=23))['eligible'])
        self.assertEqual('preview',run_once(self.root,{},forbidden,self.at,dry_run=True)['status'])

    def test_cli_transport_validates_receipt(self):
        cfg=dict(node='/node',openclaw='/openclaw',account='a',target='t')
        with patch('pantry.worker.subprocess.run',return_value=CompletedProcess([],0,'log\n{"payload":{"messageId":"m"}}','')) as call:
            send_openclaw(cfg,'hello $(never_execute)')
            self.assertNotIn('shell',call.call_args.kwargs)
            self.assertIn('hello $(never_execute)',call.call_args.args[0])
        with patch('pantry.worker.subprocess.run',return_value=CompletedProcess([],0,'{"payload":{"result":{"messageId":"nested"}}}','')):
            send_openclaw(cfg,'hello')
        for response in ('{}','{"payload":{"ok":false}}','{"dryRun":true,"payload":{"messageId":"fake"}}'):
            with patch('pantry.worker.subprocess.run',return_value=CompletedProcess([],0,response,'')):
                with self.assertRaises(RuntimeError):
                    send_openclaw(cfg,'hello')

    def test_missing_fields_and_bad_changes(self):
        for command in ({'action':'add','item':None,'request_id':'bad'}, {'action':'update','item_id':1,'changes':None,'request_id':'bad'}):
            with self.assertRaises(ValueError):
                bridge.run(command,self.root)

    def test_unknown_medicine_forces_question_unless_explicit(self):
        item={'name':'药品','category':'medicine','quantity':1,'unit':'盒','storage':'unknown'}
        first=bridge.run({'action':'add','item':item,'request_id':'auto'},self.root,True)
        self.assertIn('draft_id',first)
        self.assertIn('question',first)
        self.assertEqual([],bridge.run({'action':'list'},self.root,True)['items'])
        second=bridge.run({'action':'add','item':item,'request_id':'explicit','save_unknown':True},self.root,True)
        self.assertIsNone(second['item']['expiry'])
        self.assertIn('storage',second['missing_fields'])
        self.assertIn('储存方式：待核实',second['saved_summary'])


if __name__=='__main__':
    unittest.main()
