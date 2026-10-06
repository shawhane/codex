"""Live agent-to-MVP test. Runs on the OpenClaw host, never delivers to Weixin."""
import json
import os
import sys
import uuid
from pathlib import Path
import sqlite3
import subprocess

home=Path.home()
data=home/'.local/share/ai-pantry'
config=json.loads((data/'delivery.json').read_text())
run_id=uuid.uuid4().hex[:12]
milk_name='联调牛奶-'+run_id
medicine_name='联调药品-'+run_id
reports=data/'test-reports'/run_id
reports.mkdir(parents=True,exist_ok=True)
env=dict(os.environ)
env['PATH']=str(Path(config['node']).parent)+':'+str(Path(config['openclaw']).parent)+':'+env.get('PATH','')
session='agent:main:ai-pantry-integration-'+run_id


def agent(label,message):
    prompt='继续临期助手集成测试，所有操作仍只用ai-pantry的--test库，不操作正式库，不发消息。'+message
    result=subprocess.run([config['node'],config['openclaw'],'agent','--session-key',session,'--message',prompt,'--json','--timeout','180'],capture_output=True,text=True,timeout=210,env=env)
    if result.returncode:
        raise RuntimeError('OpenClaw命令失败: '+label)
    response=json.loads(result.stdout)
    (reports/(label+'.json')).write_text(json.dumps(response,ensure_ascii=False))
    if response.get('status')!='ok':
        raise RuntimeError('OpenClaw本轮未成功: '+label)
    inner=response.get('result',{})
    print(json.dumps({'case':label,'reply':[p.get('text') for p in inner.get('payloads',[])],'tools':inner.get('meta',{}).get('toolSummary')},ensure_ascii=False),flush=True)


def row():
    with sqlite3.connect(data/'test.sqlite3') as db:
        db.row_factory=sqlite3.Row
        return dict(db.execute("SELECT * FROM items WHERE name=?", (milk_name,)).fetchone())

if '--medicine-only' not in sys.argv:
    agent('01-add',f'帮我登记两盒{milk_name}，2026年10月10日到期，放冷藏，然后查库存。')
    assert row()['quantity']==2
    agent('02-consume',f'我喝掉了一盒{milk_name}，帮我扣掉一盒，然后查一下还剩多少。')
    assert row()['quantity']==1, '未正确扣减'
    agent('03-undo','刚才说错了，撤销上一条用掉一盒牛奶的操作，再查库存。')
    assert row()['quantity']==2, '撤销未恢复数量'
agent('04-medicine',f'帮我登记一盒{medicine_name}，药盒上的有效期没找到，存放位置也不确定。请按临期助手规则处理。')
with sqlite3.connect(data/'test.sqlite3') as db:
    drafts=db.execute("SELECT payload FROM drafts WHERE state='waiting'").fetchall()
    found=[json.loads(d[0]) for d in drafts if json.loads(d[0]).get('name')==medicine_name]
    assert found and found[-1]['expiry'] is None and found[-1]['category']=='medicine', '应创建日期未知的药品草稿'
    assert found[-1]['quantity']==1 and found[-1]['unit']=='盒', '数量单位应与原话一致'
agent('05-resolve','刚才那盒联调药品找到了，有效期是2027年3月31日，放常温，帮我补全。')
with sqlite3.connect(data/'test.sqlite3') as db:
    medicine=db.execute("SELECT expiry,storage,quantity,unit FROM items WHERE name=?",(medicine_name,)).fetchall()
    assert medicine==[('2027-03-31','room',1,'盒')], '补充信息未落到唯一原记录或数量单位错误'
real=data/'inventory.sqlite3'
if real.exists():
    with sqlite3.connect(real) as db:
        assert db.execute('SELECT COUNT(*) FROM items').fetchone()[0]==0,'测试污染正式库存'
print('PASS: 本轮OpenClaw交互和数据库核对通过；正式库存未被测试污染。run_id='+run_id,flush=True)
