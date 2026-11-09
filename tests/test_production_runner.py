"""The source must pass the preflight of the photo-boss.ru production runner.

migration/full_production_main.py refuses to start when a required file is
missing or when one of its narrow overlays no longer finds its anchor: the
Telegram SDK tag in index.html, the original login view in account.js and the
old Render address in voice-training.js (both rewritten at serve time).
"""
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    spec = importlib.util.spec_from_file_location('pb_full_production_main',
                                                  ROOT / 'migration' / 'full_production_main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_passes_production_preflight(tmp_path, monkeypatch):
    runner = load_runner()
    source = tmp_path / 'source'
    shutil.copytree(ROOT / 'app', source / 'app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    files = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in source.rglob('*') if path.is_file()}
    manifest = tmp_path / 'source-manifest.json'
    manifest.write_text(json.dumps({'baseCommit': runner.BASE_COMMIT, 'commit': '0' * 40,
                                    'release': 'preflight-test', 'files': files}))
    # Stand-in of the pinned SDK size; the server checks the real file's digest.
    sdk = tmp_path / 'telegram-web-app.js'
    sdk.write_bytes(b'/* Telegram SDK stand-in */'.ljust(119731, b' '))
    monkeypatch.setattr(runner, 'SOURCE', source)
    monkeypatch.setattr(runner, 'MANIFEST', manifest)
    monkeypatch.setattr(runner, 'SDK', sdk)
    monkeypatch.setattr(runner, 'SDK_SHA256', hashlib.sha256(sdk.read_bytes()).hexdigest())

    assert runner.verify_staged_files()['files'] == files
    assert runner.ORIGIN + '/app/' in runner.voice_script()
    assert runner.OLD_ORIGIN not in runner.voice_script()
    assert runner.LOCAL_SDK in runner.index_html()
    assert runner.LOGIN_ENTRY in runner.login_script()


def test_standalone_schema_sql_is_one_transaction():
    sql = (ROOT / 'migration' / '3.63-standalone-schema.sql').read_text()
    statements = [line for line in sql.splitlines() if line.strip() and not line.lstrip().startswith('--')]
    assert statements[0] == 'BEGIN;' and statements[-1] == 'COMMIT;'
    assert 'CONCURRENTLY' not in sql and 'DROP ' not in sql.upper()
