"""Read a JSON file bundle from stdin; install only managed ai-pantry paths."""
import base64
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone

os.umask(0o077)
home=Path.home()
workspace=home/'.openclaw/workspace'
root=workspace/'apps/ai-pantry'
data=home/'.local/share/ai-pantry'
skill=workspace/'skills/ai-pantry'
units=home/'.config/systemd/user'
marker=root/'.ai-pantry-managed'
bundle=json.load(sys.stdin)
if root.exists() and not marker.exists():
    raise SystemExit('安装目录已有未托管内容，停止以保留原文件')
if skill.exists() and not marker.exists():
    raise SystemExit('已有同名技能，停止以保留原文件')
for name in ('ai-pantry.service','ai-pantry.timer'):
    f=units/name
    if f.exists() and '# Managed by ai-pantry' not in f.read_text():
        raise SystemExit('已有同名任务，停止以保留原文件')
# Validate the entire bundle before mutation.
allowed=('pantry/','tests/','docs/','integrations/openclaw/','scripts/')
for name,content in bundle.items():
    relative=Path(name)
    if relative.is_absolute() or '..' in relative.parts or not (name.startswith(allowed) or name in ('README.md','.gitignore')):
        raise SystemExit('不支持的发布路径')
    base64.b64decode(content,validate=True)
data.mkdir(parents=True,exist_ok=True,mode=0o700)
if marker.exists():
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination=data/'releases'/stamp
    shutil.copytree(root,destination,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
root.mkdir(parents=True,exist_ok=True)
for name,content in bundle.items():
    path=root/name
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(base64.b64decode(content))
marker.write_text('ai-pantry v0.2\n')
skill.mkdir(parents=True,exist_ok=True)
shutil.copy2(root/'integrations/openclaw/ai-pantry/SKILL.md',skill/'SKILL.md')
# Existing account configuration is reused; tokens are not copied or printed.
config=data/'delivery.json'
if not config.exists():
    weixin=home/'.openclaw/openclaw-weixin'
    accounts=json.loads((weixin/'accounts.json').read_text())
    if len(accounts)!=1 or not isinstance(accounts[0],str):
        raise SystemExit('需要明确唯一的微信账号；技能已安装，调度尚未启用')
    account=accounts[0]
    if '/' in account or '..' in account:
        raise SystemExit('无效账号文件名')
    details=json.loads((weixin/'accounts'/f'{account}.json').read_text())
    target=details.get('userId')
    if not target:
        raise SystemExit('账号没有主人ID；技能已安装，调度尚未启用')
    node=home/'.nvm/versions/node/v26.8.2/bin/node'
    cli=home/'.npm-global/bin/openclaw'
    if not node.exists() or not cli.exists():
        raise SystemExit('现有运行路径已变化，请重新检查')
    config.write_text(json.dumps({'node':str(node),'openclaw':str(cli),'account':account,'target':target}))
    config.chmod(0o600)
units.mkdir(parents=True,exist_ok=True)
(units/'ai-pantry.service').write_text(f'''# Managed by ai-pantry
[Unit]
Description=AI Pantry scan and OpenClaw reminders
After=network-online.target openclaw-gateway.service

[Service]
Type=oneshot
WorkingDirectory={root}
ExecStart=/usr/bin/python3 -m pantry.worker
Environment=PATH={home}/.nvm/versions/node/v26.8.2/bin:{home}/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
UMask=0077
TimeoutStartSec=120
''')
(units/'ai-pantry.timer').write_text('''# Managed by ai-pantry
[Unit]
Description=Scan pantry drafts and reminders every five minutes

[Timer]
OnCalendar=*-*-* *:0/5:00
Persistent=true
RandomizedDelaySec=10
AccuracySec=10
Unit=ai-pantry.service

[Install]
WantedBy=timers.target
''')
print(json.dumps({'installed':True,'root':str(root),'skill':str(skill),'delivery_configured':config.exists(),'timer_enabled_by_installer':False}))
