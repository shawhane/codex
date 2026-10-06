import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')


def now():
    return datetime.now(TZ)


def stamp(value):
    if value.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return value.astimezone(TZ).isoformat(timespec='seconds')


class Pantry:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS items (
          id INTEGER PRIMARY KEY, household TEXT NOT NULL, name TEXT NOT NULL,
          category TEXT NOT NULL, quantity REAL NOT NULL, unit TEXT NOT NULL,
          storage TEXT NOT NULL, expiry TEXT, date_source TEXT NOT NULL,
          basis TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          created_by TEXT NOT NULL, created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS requests (
          household TEXT NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL,
          payload TEXT NOT NULL, result TEXT NOT NULL,
          PRIMARY KEY(household,actor,request_id)
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, household TEXT NOT NULL, actor TEXT NOT NULL,
          item_id INTEGER NOT NULL REFERENCES items(id), before_json TEXT,
          after_version INTEGER NOT NULL, undone INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS drafts (
          id INTEGER PRIMARY KEY, household TEXT NOT NULL, actor TEXT NOT NULL,
          payload TEXT NOT NULL, deadline TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'waiting', item_id INTEGER REFERENCES items(id)
        );
        CREATE TABLE IF NOT EXISTS deliveries (
          id INTEGER PRIMARY KEY, household TEXT NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id),
          phase TEXT NOT NULL, period TEXT NOT NULL, body TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'queued', sent_at TEXT,
          UNIQUE(household,item_id,phase,period)
        );
        CREATE INDEX IF NOT EXISTS items_household ON items(household,status,expiry);
        ''')

    def close(self):
        self.db.close()

    def execute(self, command, household='home', actor='owner', at=None):
        if not isinstance(command, dict):
            raise ValueError('输入必须为 JSON 对象')
        at = at or now()
        stamp(at)
        if not household or not actor:
            raise ValueError('缺少家庭或操作人')
        action = command.get('action')
        if action == 'get':
            return {'item': self._get(command['item_id'], household)}
        if action == 'drafts':
            rows = self.db.execute("SELECT * FROM drafts WHERE household=? AND actor=? AND state IN ('waiting','fallback_saved') ORDER BY id DESC LIMIT 30", (household, actor)).fetchall()
            return {'drafts': [dict(dict(r), payload=json.loads(r['payload'])) for r in rows]}
        if action == 'history':
            return {'events': [dict(r) for r in self.db.execute('SELECT id,item_id,after_version,undone FROM events WHERE household=? AND actor=? ORDER BY id DESC LIMIT 20', (household, actor))]}
        if action == 'list':
            return {'items': [dict(r) for r in self.db.execute(
                "SELECT * FROM items WHERE household=? AND status='active' ORDER BY expiry IS NULL,expiry,id", (household,))]}
        if action == 'outbox':
            return {'deliveries': [dict(r) for r in self.db.execute(
                "SELECT * FROM deliveries WHERE household=? AND state='queued' ORDER BY id", (household,))]}
        rid = command.get('request_id')
        if not isinstance(rid, str) or not rid.strip():
            raise ValueError('写操作必须提供稳定的 request_id（使用渠道原消息 ID）')
        payload = json.dumps(command, sort_keys=True, ensure_ascii=False)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = self.db.execute('SELECT * FROM requests WHERE household=? AND actor=? AND request_id=?',
                                  (household, actor, rid)).fetchone()
            if old:
                if old['payload'] != payload:
                    raise ValueError('同一 request_id 不能执行不同操作')
                self.db.commit()
                return json.loads(old['result'])
            result = self._dispatch(command, household, actor, at)
            self.db.execute('INSERT INTO requests VALUES (?,?,?,?,?)',
                            (household, actor, rid, payload, json.dumps(result, ensure_ascii=False)))
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def _validate(self, data):
        if not isinstance(data, dict):
            raise ValueError('item 必须为 JSON 对象')
        name = data.get('name', '')
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError('物品名称不能为空，最多128字')
        q = data.get('quantity', 1)
        if isinstance(q, bool) or not isinstance(q, (int, float)) or not 0 < q <= 1000000:
            raise ValueError('数量必须为正数且不超过1000000')
        category = data.get('category', 'food')
        if category not in ('food', 'medicine', 'other'):
            raise ValueError('无效品类')
        storage = data.get('storage', 'unknown')
        if storage not in ('room', 'fridge', 'freezer', 'unknown'):
            raise ValueError('无效存储方式')
        expiry = data.get('expiry')
        source = data.get('date_source', 'unknown')
        if source not in ('label', 'user', 'estimate', 'unknown'):
            raise ValueError('无效日期来源')
        if expiry is not None:
            if not isinstance(expiry, str):
                raise ValueError('日期必须为 YYYY-MM-DD')
            try:
                parsed = datetime.strptime(expiry, '%Y-%m-%d').date()
            except ValueError:
                raise ValueError('日期必须为有效的 YYYY-MM-DD')
            if parsed.isoformat() != expiry or source == 'unknown':
                raise ValueError('日期格式或来源不完整')
        elif source != 'unknown':
            raise ValueError('没有日期时来源必须为 unknown')
        if category == 'medicine' and source == 'estimate':
            raise ValueError('药品不可估算有效期，请保留待核实')
        basis = data.get('basis', '')
        unit = data.get('unit', '件')
        if not isinstance(basis, str) or not isinstance(unit, str) or not unit.strip():
            raise ValueError('单位和日期依据必须为文本')
        if source != 'unknown' and not basis.strip():
            raise ValueError('请记录日期依据：包装文字、用户说明或估算规则')
        return dict(name=name.strip(), category=category, quantity=q, unit=unit,
                    storage=storage, expiry=expiry, date_source=source, basis=basis)

    def _get(self, item_id, household):
        row = self.db.execute('SELECT * FROM items WHERE id=? AND household=?', (item_id, household)).fetchone()
        if row is None:
            raise ValueError('找不到该家庭的物品')
        return dict(row)

    def _event(self, household, actor, item_id, before, version):
        return self.db.execute('INSERT INTO events(household,actor,item_id,before_json,after_version) VALUES (?,?,?,?,?)',
                               (household, actor, item_id, json.dumps(before) if before else None, version)).lastrowid

    def _add(self, data, household, actor, at):
        data = self._validate(data)
        cols = list(data)
        row_id = self.db.execute(
            'INSERT INTO items(household,created_by,created_at,' + ','.join(cols) + ') VALUES (' + ','.join(['?'] * (len(cols)+3)) + ')',
            [household, actor, stamp(at)] + list(data.values())).lastrowid
        event = self._event(household, actor, row_id, None, 1)
        return {'item': self._get(row_id, household), 'event_id': event}

    def _change(self, item_id, changes, household, actor):
        if not isinstance(changes, dict):
            raise ValueError('changes 必须为 JSON 对象')
        old = self._get(item_id, household)
        if old['status'] != 'active':
            raise ValueError('物品已处理，恢复请撤销原操作')
        allowed = {'name','category','quantity','unit','storage','expiry','date_source','basis','status'}
        if not changes or set(changes) - allowed:
            raise ValueError('存在不支持修改的字段')
        if changes.get('status', 'active') not in ('active','consumed','discarded'):
            raise ValueError('无效处理状态')
        if 'storage' in changes and changes['storage'] != old['storage'] and old['date_source'] == 'estimate' and 'expiry' not in changes:
            changes = dict(changes, expiry=None, date_source='unknown', basis='储存方式改变，原估算已失效')
        new = dict(old, **changes)
        # Fully consumed entries retain their previous quantity for audit.
        self._validate(new)
        version = old['version'] + 1
        cols = list(changes)
        self.db.execute('UPDATE items SET ' + ','.join(k+'=?' for k in cols) + ',version=? WHERE id=?',
                        list(changes.values()) + [version, item_id])
        if changes:
            self.db.execute("UPDATE deliveries SET state='cancelled' WHERE item_id=? AND state='queued'", (item_id,))
        return {'item': self._get(item_id, household), 'event_id': self._event(household, actor, item_id, old, version)}

    def _dispatch(self, c, h, actor, at):
        action = c['action']
        if action == 'add':
            return self._add(c['item'], h, actor, at)
        if action == 'update':
            return self._change(c['item_id'], c['changes'], h, actor)
        if action in ('consume','discard'):
            old = self._get(c['item_id'], h)
            amount = c.get('quantity', old['quantity'])
            if isinstance(amount, bool) or not isinstance(amount, (int,float)) or not 0 < amount <= old['quantity']:
                raise ValueError('处理数量必须大于0且不超过库存')
            changes = {'quantity': old['quantity'] - amount} if amount < old['quantity'] else {'status': 'consumed' if action == 'consume' else 'discarded'}
            return self._change(old['id'], changes, h, actor)
        if action == 'undo':
            ev = self.db.execute('SELECT * FROM events WHERE id=? AND household=? AND actor=?', (c['event_id'],h,actor)).fetchone()
            if not ev or ev['undone']:
                raise ValueError('操作不存在、非本人操作或已撤销')
            item = self._get(ev['item_id'], h)
            if item['version'] != ev['after_version']:
                raise ValueError('物品已有后续修改，不能覆盖，请明确修改内容')
            before = json.loads(ev['before_json']) if ev['before_json'] else {'status':'cancelled'}
            fields = {k:v for k,v in before.items() if k not in ('id','household','created_by','created_at','version')}
            self.db.execute('UPDATE items SET '+','.join(k+'=?' for k in fields)+',version=version+1 WHERE id=?', list(fields.values())+[item['id']])
            self.db.execute('UPDATE events SET undone=1 WHERE id=?', (ev['id'],))
            self.db.execute("UPDATE deliveries SET state='cancelled' WHERE item_id=? AND state='queued'",(item['id'],))
            return {'item': self._get(item['id'],h)}
        if action == 'draft':
            data = self._validate(c['item'])
            draft = self.db.execute('INSERT INTO drafts(household,actor,payload,deadline) VALUES (?,?,?,?)',
                (h,actor,json.dumps(data),stamp(at+timedelta(hours=1)))).lastrowid
            return {'draft_id': draft, 'question': '请补充有效期或储存方式；一小时后仍缺日期将按待核实登记。'}
        if action == 'resolve':
            d = self.db.execute('SELECT * FROM drafts WHERE id=? AND household=? AND actor=?',(c['draft_id'],h,actor)).fetchone()
            if not d:
                raise ValueError('找不到待补充记录')
            if d['item_id']:
                result = self._change(d['item_id'],c['changes'],h,actor)
                self.db.execute("UPDATE drafts SET state='resolved' WHERE id=?", (d['id'],))
                return result
            data = dict(json.loads(d['payload']), **c['changes'])
            result = self._add(data,h,actor,at)
            self.db.execute("UPDATE drafts SET state='resolved',item_id=? WHERE id=?",(result['item']['id'],d['id']))
            return result
        if action == 'tick':
            return self._tick(h,at)
        if action == 'ack':
            delivery = self.db.execute('SELECT * FROM deliveries WHERE id=? AND household=?',(c['delivery_id'],h)).fetchone()
            if not delivery or delivery['state'] == 'cancelled':
                raise ValueError('提醒不存在或已取消')
            self.db.execute("UPDATE deliveries SET state='sent',sent_at=COALESCE(sent_at,?) WHERE id=?",(stamp(at),delivery['id']))
            return {'delivery_id':delivery['id'],'state':'sent'}
        raise ValueError('不支持的 action')

    def _queue(self, h, item, phase, period, body):
        self.db.execute("INSERT INTO deliveries(household,item_id,phase,period,body) VALUES (?,?,?,?,?) ON CONFLICT(household,item_id,phase,period) DO UPDATE SET body=excluded.body,state='queued' WHERE deliveries.state='cancelled'",
                        (h,item['id'],phase,period,body))

    def _tick(self,h,at):
        today = at.astimezone(TZ).date()
        for d in self.db.execute("SELECT * FROM drafts WHERE household=? AND state='waiting' AND deadline<=?",(h,stamp(at))).fetchall():
            item = self._add(json.loads(d['payload']),h,d['actor'],at)['item']
            self.db.execute("UPDATE drafts SET state='fallback_saved',item_id=? WHERE id=?",(item['id'],d['id']))
            self._queue(h,item,'fallback',str(d['id']),f"#{item['id']} {item['name']} 已登记；缺失日期保持待核实，可随时补充。")
        for row in self.db.execute("SELECT * FROM items WHERE household=? AND status='active'",(h,)).fetchall():
            item = dict(row)
            # Do not accumulate stale daily notifications during a delivery outage.
            self.db.execute("UPDATE deliveries SET state='cancelled' WHERE item_id=? AND phase!='fallback' AND state='queued' AND period NOT LIKE ?",
                            (item['id'], today.isoformat()+'%'))
            date = datetime.strptime(item['expiry'],'%Y-%m-%d').date() if item['expiry'] else None
            if date is None:
                if today.weekday() != 0:
                    continue
                phase = 'unknown'
            else:
                days = (date-today).days
                if days > (30 if item['category']=='medicine' else 2):
                    continue
                phase = 'pre' if days > 0 else 'due'
                sent = self.db.execute("SELECT COUNT(*) FROM deliveries WHERE item_id=? AND phase=? AND state='sent' AND period LIKE ?",
                                       (item['id'],phase,'%|'+item['expiry'])).fetchone()[0]
                if phase == 'pre' and sent >= 1:
                    continue
                if phase == 'due' and sent >= 3:
                    if today.weekday() != 0:
                        continue
                    phase = 'weekly'
            # A successful daily/weekly reminder must never repeat on the same local date.
            already = self.db.execute("SELECT 1 FROM deliveries WHERE item_id=? AND phase IN ('pre','due','weekly','unknown') AND state='sent' AND substr(sent_at,1,10)=?",(item['id'],today.isoformat())).fetchone()
            if already:
                continue
            label = '预计处理日' if item['date_source']=='estimate' else '有效期'
            body = f"#{item['id']} {item['name']} {item['quantity']:g}{item['unit']}：" + (f"{label} {item['expiry']}" if date else '有效期待核实')
            body += '。请确认是否用完或丢弃；未回复会保留记录。'
            self._queue(h,item,phase,today.isoformat()+'|'+(item['expiry'] or 'unknown'),body)
        return {'queued': self.db.execute("SELECT COUNT(*) FROM deliveries WHERE household=? AND state='queued'",(h,)).fetchone()[0]}
