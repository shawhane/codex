"""Isolated deterministic demonstration; does not touch real inventory or send messages."""
import json
from datetime import datetime, timedelta
from .core import Pantry, TZ


def main():
    app = Pantry(':memory:')
    at = datetime(2026,10,6,9,30,tzinfo=TZ)
    commands = [
        {'action':'add','item':{'name':'鲜牛奶','quantity':2,'unit':'盒','expiry':'2026-10-08','date_source':'user','basis':'演示用户提供'}},
        {'action':'consume','item_id':1,'quantity':1},
        {'action':'undo','event_id':2},
        {'action':'draft','item':{'name':'药品','category':'medicine'}},
        {'action':'tick'},
        {'action':'list'},
        {'action':'outbox'},
    ]
    try:
        for n, command in enumerate(commands):
            command['request_id'] = 'demo:'+str(n)
            result = app.execute(command,at=at+timedelta(hours=2) if n>=4 else at)
            print(command['action']+': '+json.dumps(result,ensure_ascii=False))
    finally:
        app.close()


if __name__ == '__main__':
    main()
