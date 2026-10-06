import argparse
import json
import os
import sys
from pathlib import Path
from .core import Pantry


def main():
    parser = argparse.ArgumentParser(description='临期助手：读取 stdin 的一个 JSON 命令，输出一个 JSON 结果')
    parser.add_argument('--db', default='.local/pantry.sqlite3')
    parser.add_argument('--household', default='home')
    parser.add_argument('--actor', default='owner')
    args = parser.parse_args()
    os.umask(0o077)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    app = Pantry(args.db)
    try:
        command = json.load(sys.stdin)
        if not isinstance(command, dict):
            raise ValueError('输入必须为 JSON 对象')
        result = app.execute(command, args.household, args.actor)
        print(json.dumps({'ok':True, **result}, ensure_ascii=False))
    except (ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)}, ensure_ascii=False))
        return 1
    finally:
        app.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
