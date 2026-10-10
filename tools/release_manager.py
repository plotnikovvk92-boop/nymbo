#!/usr/bin/env python3
"""Explicit target backup/install/rollback. No bot imports or webhook changes.
Stop only the selected Passenger application before install/rollback using its
hosting control. SQLite remains in place; backup() works with WAL.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

PACKAGE=Path(__file__).resolve().parents[1]
SOURCE=PACKAGE/'gymochka_app'
CODE=tuple(sorted(p.name for p in SOURCE.glob('*.py')))+('requirements.txt',)


def atomic_copy(src,dest):
    temp=dest.with_name(dest.name+'.nyambo-install-tmp')
    if temp.exists():raise RuntimeError('Existing install temporary file; inspect before continuing')
    shutil.copy2(src,temp)
    os.replace(temp,dest)


def sqlite_snapshot(database,dest):
    if not database.is_file():raise RuntimeError('Database missing; refuses to create an empty replacement')
    if dest.exists():raise RuntimeError('Snapshot destination already exists')
    uri=database.resolve().as_uri()+'?mode=ro'
    with sqlite3.connect(uri,uri=True,timeout=30) as src,sqlite3.connect(dest) as dst:
        src.backup(dst)
        result=dst.execute('PRAGMA quick_check').fetchone()[0]
        if result!='ok':raise RuntimeError('SQLite backup integrity check failed')
    dest.chmod(0o600)


def backup(app,database,entry,destination):
    if not app.is_dir() or not entry.is_file():raise RuntimeError('Explicit app or Passenger entrypoint missing')
    if destination.exists():raise RuntimeError('Backup destination must be new')
    # Never store credentials/client backups under a publicly served root.
    destination.mkdir(parents=True,mode=0o700)
    destination.chmod(0o700)
    code=destination/'code';code.mkdir(mode=0o700)
    files=[]
    for name in CODE:
        p=app/name
        if p.is_symlink():raise RuntimeError('Symlink code needs manual inspection')
        if p.is_file():shutil.copy2(p,code/name);files.append(name)
    shutil.copy2(entry,destination/'passenger_wsgi.py')
    if (app/'.env').is_file():
        shutil.copy2(app/'.env',destination/'env.private');(destination/'env.private').chmod(0o600)
    sqlite_snapshot(database,destination/'database.sqlite3')
    manifest={'app':str(app.resolve()),'database':str(database.resolve()),'entry':str(entry.resolve()),
              'files':files,'created_at':datetime.now(timezone.utc).isoformat(),
              'database_sha256':hashlib.sha256((destination/'database.sqlite3').read_bytes()).hexdigest()}
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2));(destination/'manifest.json').chmod(0o600)
    print('Backup created and SQLite quick_check passed: '+str(destination))


def install(app,entry,backup_dir,stopped):
    if not stopped:raise RuntimeError('Stop the selected application first; --service-stopped acknowledges this')
    manifest=json.loads((backup_dir/'manifest.json').read_text())
    if str(app.resolve())!=manifest['app'] or str(entry.resolve())!=manifest['entry']:raise RuntimeError('Backup belongs to a different application')
    if not (backup_dir/'database.sqlite3').is_file():raise RuntimeError('Verified database snapshot missing')
    # All Python syntax is checked before the first replacement.
    for name in CODE:
        p=SOURCE/name
        if p.suffix=='.py':compile(p.read_text(),str(p),'exec')
    body=(PACKAGE/'passenger_wsgi.py').read_text()
    original='APP_DIR = "/home/p/plotnixu/plotnixu.beget.tech/gymochka_app"'
    if body.count(original)!=1:raise RuntimeError('Unexpected Passenger template')
    body=body.replace(original,'APP_DIR = '+repr(str(app.resolve())))
    compile(body,str(entry),'exec')
    # Keep all .env, DB, fonts and legal files unchanged. Legal drafts require review.
    for name in CODE:atomic_copy(SOURCE/name,app/name)
    temp=entry.with_name(entry.name+'.nyambo-install-tmp')
    if temp.exists():raise RuntimeError('Existing entrypoint temporary file')
    temp.write_text(body);temp.chmod(entry.stat().st_mode & 0o777);os.replace(temp,entry)
    print('Code installed. Configuration, database, legal publications and fonts retained. Restart not performed.')


def rollback(backup_dir,stopped):
    if not stopped:raise RuntimeError('Stop the selected application first')
    m=json.loads((backup_dir/'manifest.json').read_text());app=Path(m['app']);entry=Path(m['entry'])
    for name in m['files']:atomic_copy(backup_dir/'code'/name,app/name)
    atomic_copy(backup_dir/'passenger_wsgi.py',entry)
    print('Code rollback completed. Live database and .env retained. Restart not performed.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('backup','install','rollback'))
    parser.add_argument('--app',type=Path);parser.add_argument('--db',type=Path);parser.add_argument('--entry',type=Path)
    parser.add_argument('--backup',type=Path,required=True);parser.add_argument('--service-stopped',action='store_true')
    a=parser.parse_args()
    try:
        if a.action=='rollback':rollback(a.backup,a.service_stopped)
        else:
            if not a.app or not a.entry:raise RuntimeError('--app and --entry are required')
            if a.action=='backup':
                if not a.db:raise RuntimeError('--db is required')
                backup(a.app,a.db,a.entry,a.backup)
            else:install(a.app,a.entry,a.backup,a.service_stopped)
    except Exception as e:
        print('Operation stopped: '+str(e),file=sys.stderr);return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
