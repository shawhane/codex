"""Single-host timer: durable scan, backup and OpenClaw delivery."""
import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from .core import Pantry, TZ, now, stamp


def save_attempt(path, value):
    temp = path.with_suffix('.tmp')
    with open(temp, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def send_openclaw(config, text):
    # Fixed argv; no shell or model involved. Reuses the existing channel plugin.
    args = [config['node'], config['openclaw'], 'message', 'send',
            '--channel', 'openclaw-weixin', '--account', config['account'],
            '--target', config['target'], '--message', text, '--json']
    completed = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if completed.returncode:
        raise RuntimeError('OpenClaw发送失败；请检查现有渠道状态，原始输出未写日志')
    # CLI may prefix JSON with diagnostic lines. Require a structured result.
    raw = completed.stdout.strip()
    result = None
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char == '{':
            try:
                result, end = decoder.raw_decode(raw[index:])
                if not raw[index+end:].strip():
                    break
                result = None
            except ValueError:
                pass
    if not isinstance(result, dict):
        raise RuntimeError('OpenClaw未返回可确认的JSON发送结果')
    if result.get('error') or result.get('ok') is False or result.get('dryRun'):
        raise RuntimeError('OpenClaw未确认真实发送成功')
    payload = result.get('payload', result)
    if not isinstance(payload, dict) or payload.get('error') or payload.get('ok') is False:
        raise RuntimeError('OpenClaw发送结果异常')
    receipt = payload.get('result', payload)
    if not isinstance(receipt, dict) or receipt.get('error') or receipt.get('ok') is False or receipt.get('dryRun'):
        raise RuntimeError('OpenClaw消息回执异常')
    if not any(receipt.get(key) for key in ('messageId', 'message_id', 'id')):
        raise RuntimeError('OpenClaw未提供消息回执，保留待核对状态')
    return result


def run_once(data_dir, config, sender=send_openclaw, at=None, dry_run=False):
    at = (at or now()).astimezone(TZ)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(data_dir/'worker.lock','a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status':'already_running'}
        app = Pantry(data_dir/'inventory.sqlite3')
        try:
            attempt_path = data_dir/'delivery-attempt.json'
            if attempt_path.exists() and not dry_run:
                previous = json.loads(attempt_path.read_text())
                if previous['state'] in ('sending','uncertain'):
                    return {'status':'needs_reconciliation','delivery_ids':previous['delivery_ids']}
                if previous['state']=='delivered':
                    for ident in previous['delivery_ids']:
                        row = app.db.execute('SELECT state FROM deliveries WHERE id=?', (ident,)).fetchone()
                        if row and row['state'] != 'cancelled':
                            app.execute({'action':'ack','request_id':'ack:'+str(ident),'delivery_id':ident},at=at)
                    previous['state']='acknowledged'
                    save_attempt(attempt_path, previous)
                    return {'status':'reconciled','sent':0}
            # Scheduler scans have no external writes and may safely re-evaluate current state.
            app.execute({'action':'tick','request_id':'tick:'+stamp(at)}, at=at)
            backup_dir = data_dir/'backups'
            backup_dir.mkdir(exist_ok=True, mode=0o700)
            backup = backup_dir/(at.date().isoformat()+'.sqlite3')
            if not backup.exists():
                with sqlite3.connect(backup) as dest:
                    app.db.backup(dest)
            queue = app.execute({'action':'outbox'})['deliveries']
            after_reminder_time = (at.hour,at.minute) >= (9,30)
            selected = [d for d in queue if 8 <= at.hour < 22 and (d['phase']=='fallback' or after_reminder_time)]
            if dry_run or not selected:
                return {'status':'preview' if dry_run else 'idle','queued':len(queue),'eligible':len(selected)}
            # Durable attempt state makes an ambiguous external send a manual reconcile,
            # rather than silently retrying a possibly delivered message.
            # Avoid oversized group payloads. Remaining rows are sent on the next timer run.
            batch, length = [], 0
            for entry in selected:
                if batch and length + len(entry['body']) > 2500:
                    break
                batch.append(entry)
                length += len(entry['body'])+1
            attempt={'state':'sending','delivery_ids':[d['id'] for d in batch],'at':stamp(at)}
            save_attempt(attempt_path, attempt)
            text='临期助手提醒\n'+'\n'.join(d['body'] for d in batch)
            try:
                sender(config,text)
            except Exception:
                attempt['state']='uncertain'
                save_attempt(attempt_path, attempt)
                raise
            attempt['state']='delivered'
            save_attempt(attempt_path, attempt)
            for entry in batch:
                app.execute({'action':'ack','request_id':'ack:'+str(entry['id']),'delivery_id':entry['id']},at=at)
            attempt['state']='acknowledged'
            save_attempt(attempt_path, attempt)
            return {'status':'sent','sent':len(batch)}
        finally:
            app.close()
    finally:
        lock.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',default=str(Path.home()/'.local/share/ai-pantry'))
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    os.umask(0o077)
    try:
        config_path=Path(args.data_dir)/'delivery.json'
        config=json.loads(config_path.read_text()) if config_path.exists() else {}
        if not args.dry_run and not all(config.get(k) for k in ('node','openclaw','account','target')):
            raise ValueError('缺少现有OpenClaw投递配置')
        result=run_once(args.data_dir,config,dry_run=args.dry_run)
        print(json.dumps(result,ensure_ascii=False))
        return 1 if result.get('status')=='needs_reconciliation' else 0
    except Exception as exc:
        # No credentials, recipient identifiers, message bodies or subprocess output in logs.
        print(json.dumps({'status':'error','type':type(exc).__name__,'hint':'检查临期助手投递状态；若发送结果不明确，需核对后恢复'}))
        return 1


if __name__=='__main__':
    sys.exit(main())
