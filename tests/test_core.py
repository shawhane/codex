import unittest
from datetime import datetime, timedelta
from pantry.core import Pantry, TZ


class PantryTest(unittest.TestCase):
    def setUp(self):
        self.app = Pantry(':memory:')
        self.n = 0
        self.at = datetime(2026,10,6,9,30,tzinfo=TZ)

    def tearDown(self):
        self.app.close()

    def call(self, action, at=None, household='home', **kwargs):
        self.n += 1
        return self.app.execute(dict(action=action,request_id=str(self.n),**kwargs),household=household,at=at or self.at)

    def add(self, **changes):
        item = dict(name='牛奶',quantity=2,unit='盒',expiry='2026-10-08',date_source='label',basis='包装有效期2026-10-08')
        item.update(changes)
        return self.call('add',item=item)

    def test_message_replay_and_collision(self):
        c = dict(action='add',request_id='wx:123',item={'name':'苹果'})
        a = self.app.execute(c)
        self.assertEqual(a,self.app.execute(c))
        self.assertEqual(1,len(self.call('list')['items']))
        c['item']['name'] = '梨'
        with self.assertRaises(ValueError):
            self.app.execute(c)

    def test_partial_and_undo(self):
        item = self.add()['item']
        result = self.call('consume',item_id=item['id'],quantity=1)
        self.assertEqual(1,result['item']['quantity'])
        restored = self.call('undo',event_id=result['event_id'])
        self.assertEqual(2,restored['item']['quantity'])

    def test_full_discard(self):
        item = self.add()['item']
        self.call('discard',item_id=item['id'])
        self.assertEqual([],self.call('list')['items'])

    def test_no_cross_household(self):
        item = self.add()['item']
        with self.assertRaises(ValueError):
            self.call('consume',item_id=item['id'],household='other')

    def test_undo_does_not_overwrite_later_edit(self):
        first = self.add()
        self.call('update',item_id=first['item']['id'],changes={'name':'鲜牛奶'})
        with self.assertRaises(ValueError):
            self.call('undo',event_id=first['event_id'])

    def test_unknown_medicine_and_invalid_date(self):
        with self.assertRaises(ValueError):
            self.add(category='medicine',date_source='estimate')
        with self.assertRaises(ValueError):
            self.add(expiry='2026-02-30')
        with self.assertRaises(ValueError):
            self.add(quantity=float('nan'))
        item = self.add(category='medicine',expiry=None,date_source='unknown')['item']
        self.assertIsNone(item['expiry'])

    def test_changed_storage_invalidates_estimate(self):
        item = self.add(date_source='estimate')['item']
        result = self.call('update',item_id=item['id'],changes={'storage':'freezer'})
        self.assertIsNone(result['item']['expiry'])

    def test_timeout_then_late_reply_no_duplicate(self):
        d = self.call('draft',item={'name':'药品','category':'medicine'})
        self.call('tick',at=self.at+timedelta(minutes=59))
        self.assertEqual([],self.call('list')['items'])
        self.call('tick',at=self.at+timedelta(hours=1))
        self.call('tick',at=self.at+timedelta(hours=2))
        self.assertEqual(1,len(self.call('list')['items']))
        result = self.call('resolve',draft_id=d['draft_id'],changes={'expiry':'2027-01-01','date_source':'user','basis':'用户补充'})
        self.assertEqual('2027-01-01',result['item']['expiry'])
        self.assertEqual(1,len(self.call('list')['items']))

    def test_multiple_drafts(self):
        self.call('draft',item={'name':'苹果'})
        self.call('draft',item={'name':'药品','category':'medicine'})
        self.call('tick',at=self.at+timedelta(hours=1))
        self.assertEqual(2,len(self.call('list')['items']))

    def send_due(self, at):
        self.call('tick',at=at)
        messages = self.call('outbox')['deliveries']
        for msg in messages:
            self.call('ack',at=at,delivery_id=msg['id'])
        return messages

    def test_pre_does_not_exhaust_due_then_weekly(self):
        self.add(category='medicine',expiry='2026-11-05')
        self.assertEqual(1,len(self.send_due(self.at)))
        self.assertEqual([],self.send_due(self.at+timedelta(days=1)))
        for days in (30,31,32):
            self.assertEqual('due',self.send_due(self.at+timedelta(days=days))[0]['phase'])
        self.assertEqual([],self.send_due(self.at+timedelta(days=33)))
        weekly = self.send_due(self.at+timedelta(days=34))
        self.assertEqual('weekly',weekly[0]['phase'])
        self.assertEqual(1,len(self.call('list')['items']))
        self.assertEqual([],self.send_due(self.at+timedelta(days=34)))

    def test_failed_delivery_does_not_count(self):
        self.add(expiry='2026-10-06')
        self.call('tick')
        self.call('tick')
        self.assertEqual(1,len(self.call('outbox')['deliveries']))
        self.call('tick',at=self.at+timedelta(days=5))
        messages = self.call('outbox')['deliveries']
        self.assertEqual(1,len(messages))
        self.assertEqual('due',messages[0]['phase'])

    def test_cancel_stale_on_consumption(self):
        item = self.add()['item']
        self.call('tick')
        old = self.call('outbox')['deliveries'][0]
        self.call('consume',item_id=item['id'])
        self.assertEqual([],self.call('outbox')['deliveries'])
        with self.assertRaises(ValueError):
            self.call('ack',delivery_id=old['id'])


if __name__ == '__main__':
    unittest.main()
