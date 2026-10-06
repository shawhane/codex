#!/usr/bin/env python3
"""Trusted OpenClaw CLI bridge. JSON on stdin; no credentials or network needed."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pantry.core import Pantry, now

BUSINESS_ACTIONS = {'list', 'get', 'drafts', 'history', 'add', 'update', 'consume', 'discard', 'undo', 'draft', 'resolve'}


def run(command, data_dir, test=False):
    if not isinstance(command, dict):
        raise ValueError('输入必须是JSON对象')
    if command.get('action') not in BUSINESS_ACTIONS | {'status'}:
        raise ValueError('仅允许库存业务操作；提醒调度由后台处理')
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = 'test' if test else 'production'
    if command.get('action') == 'status':
        attempt = directory/'delivery-attempt.json'
        delivery_state = json.loads(attempt.read_text()).get('state') if attempt.exists() else 'idle'
        return {'ok': True, 'profile': profile, 'today': now().date().isoformat(),
                'timezone': 'Asia/Shanghai', 'actions': sorted(BUSINESS_ACTIONS),
                'reminder_delivery': delivery_state,
                'reminder_needs_attention': delivery_state in ('sending', 'uncertain')}
    command = dict(command)
    if command.get('action') in ('add', 'draft') and isinstance(command.get('item'), dict):
        missing = [key for key in ('category', 'quantity', 'unit', 'storage') if key not in command['item']]
        if missing:
            raise ValueError('请从用户原话提取并显式传入这些字段：' + ', '.join(missing) + '。例如“一盒”是quantity:1、unit:"盒"；不要漏传单位后默认为件。储存未说明用unknown。')
    if command.get('action') == 'add' and isinstance(command.get('item'), dict):
        item = command['item']
        missing_critical = (item.get('category') == 'medicine' and not item.get('expiry')) or (
            not item.get('expiry') and item.get('storage', 'unknown') == 'unknown')
        if missing_critical and command.get('save_unknown') is not True:
            # A prompt alone did not reliably elicit a question in the live integration test.
            command['action'] = 'draft'
    app = Pantry(directory / ('test.sqlite3' if test else 'inventory.sqlite3'))
    try:
        result = app.execute(command, household='home', actor='owner')
        if 'item' in result:
            item = result['item']
            storage = {'unknown':'待核实','room':'常温','fridge':'冷藏','freezer':'冷冻'}[item['storage']]
            result['saved_summary'] = f"#{item['id']} {item['name']}，数量 {item['quantity']:g}{item['unit']}，储存方式：{storage}，日期：{item['expiry'] or '待核实'}，状态：{item['status']}"
            result['missing_fields'] = [key for key,missing in [('expiry',not item['expiry']),('storage',item['storage']=='unknown')] if missing]
        return {'ok': True, 'profile': profile, **result}
    finally:
        app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test', action='store_true', help='隔离测试库，不进入真实提醒')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        # Environment override is only for host deployment/tests, never user JSON.
        data_dir = os.environ.get('PANTRY_DATA_DIR', str(Path.home()/'.local/share/ai-pantry'))
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise ValueError('命令超过64KB')
        result = run(json.loads(raw), data_dir, args.test)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'ok':False, 'error':str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
