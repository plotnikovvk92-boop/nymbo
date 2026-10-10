#!/usr/bin/env python3
"""Read-only diagnostics. Never imports bot.py or prints credential values/user rows."""
import ast, hashlib, json, os, pathlib, sqlite3, subprocess, sys, urllib.request, platform
from importlib import metadata
ROOT = pathlib.Path('/home/p/plotnixu/plotnixu.beget.tech')

def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            key, value = line.removeprefix('export ').split('=', 1)
            values[key.strip()] = value.strip().strip('\"\'')
    return values

def api(url, data=None):
    try:
        req = urllib.request.Request(url, data=data, headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r: return json.load(r)
    except Exception: return None  # Exception text can contain the token URL.

result = {'python': sys.version.split()[0], 'libc':platform.libc_ver(), 'root_exists': ROOT.is_dir(), 'apps': [], 'passenger': []}
for p in sorted(ROOT.rglob('passenger_wsgi.py')) if ROOT.exists() else []:
    if any(part in ('venv','backups','node_modules') for part in p.parts): continue
    try:
        tree = ast.parse(p.read_text())
        app_dir = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id=='APP_DIR' for x in node.targets):
                try: app_dir=ast.literal_eval(node.value)
                except Exception: pass
        result['passenger'].append({'entry':str(p), 'APP_DIR':app_dir})
    except Exception: result['passenger'].append({'entry':str(p),'error':'unreadable_or_invalid'})
result['package_versions']={}
for name in ('aiogram','aiohttp','aiosqlite','python-dotenv','reportlab','cryptography'):
    try:result['package_versions'][name]=metadata.version(name)
    except metadata.PackageNotFoundError:result['package_versions'][name]='not_installed'
values_by_dir = {}
for name in ('gymochka_app', 'nyambo_app'):
    path=ROOT/name
    e=read_env(path/'.env')
    values_by_dir[name]=e
    token=e.get('BOT_TOKEN','')
    item={'directory':str(path),'exists':path.is_dir(), 'env_present':(path/'.env').is_file(),
          'mode_from_dotenv':e.get('TEST_MODE','UNSET'),
          'set_names':[k for k in ('BOT_TOKEN','WEBHOOK_SECRET_TOKEN','YOOKASSA_SHOP_ID','YOOKASSA_SECRET_KEY','ADMIN_USER_IDS') if e.get(k)],
          'shop_id': e.get('YOOKASSA_SHOP_ID','UNSET'),
          'key_mode': 'test' if e.get('YOOKASSA_SECRET_KEY','').startswith('test_') else ('non-test' if e.get('YOOKASSA_SECRET_KEY') else 'UNSET'),
          'admin_numeric_ids_configured':bool(e.get('ADMIN_USER_IDS'))}
    db=pathlib.Path(e.get('DB_PATH','bot.db'))
    if not db.is_absolute(): db=path/db
    item['database']=str(db.resolve())
    item['database_exists']=db.is_file()
    if db.is_file():
        try:
            with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as conn:
                item['db_tables']=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        except Exception: item['db_error']='read_only_open_failed'
    item['configured_webhook_url']=e.get('WEBHOOK_URL','UNSET')
    item['return_url']=e.get('YOOKASSA_RETURN_URL','UNSET')
    if token:
        me=api('https://api.telegram.org/bot'+token+'/getMe')
        wh=api('https://api.telegram.org/bot'+token+'/getWebhookInfo')
        item['telegram_api_ok']=bool(me and me.get('ok'))
        if me and me.get('ok'): item['bot_username']=me['result'].get('username')
        if wh and wh.get('ok'):
            w=wh['result']; item['actual_webhook_url']=w.get('url'); item['pending_update_count']=w.get('pending_update_count')
    for file in ('bot.py','config.py','database.py','payment_service.py','requirements.txt'):
        p=path/file
        if p.is_file(): item.setdefault('code_sha256',{})[file]=hashlib.sha256(p.read_bytes()).hexdigest()
    result['apps'].append(item)
a,b=[values_by_dir[x] for x in ('gymochka_app','nyambo_app')]
result['same_bot_token']=bool(a.get('BOT_TOKEN') and a.get('BOT_TOKEN')==b.get('BOT_TOKEN'))
# Process counts, never raw argv (may contain tokens) or individual PID/user data.
r=subprocess.run(['ps','-eo','args='],capture_output=True,text=True)
lines=r.stdout.splitlines()
result['process_counts']={name:sum(name in line and 'diagnose_beget' not in line for line in lines) for name in ('Passenger','passenger','bot.py','gymochka_app','nyambo_app')}
print(json.dumps(result,ensure_ascii=False,indent=2))
