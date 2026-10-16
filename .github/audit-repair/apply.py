"""Apply an explicitly authorized, checksum-verified source delta on the repair branch."""
import base64
import hashlib
import json
from pathlib import Path
import zlib

root = Path.cwd().resolve()
folder = root / '.github/audit-repair'
encoded = ''.join((folder / f'delta-{i:02d}.txt').read_text().strip() for i in range(1, 6))
raw = zlib.decompress(base64.b64decode(encoded, validate=True))
assert hashlib.sha256(raw).hexdigest() == 'dafbb02f4142462ea20704c7edf967f032a7c4bbf93bc2e374326c7212fd7dc8', 'Source bundle checksum mismatch'
entries = json.loads(raw)
assert len(entries) == 31
writes = []
seen = set()
for entry in entries:
    name = entry['path']
    assert name not in seen
    seen.add(name)
    assert name.startswith(('app/', 'tests/')) or name == '.github/workflows/recovery-check.yml'
    target = root / name
    assert not target.is_symlink()
    assert root in target.resolve().parents
    previous = target.read_text() if target.exists() else ''
    previous_hash = hashlib.sha256(previous.encode()).hexdigest()
    if previous_hash == entry['after']:
        continue
    assert previous_hash == entry['before'], f'Source changed: {name}'
    assert bool(entry['new']) == (not target.exists()), f'Unexpected file existence: {name}'
    content = previous
    for start, end, insertion in reversed(entry['edits']):
        assert 0 <= start <= end <= len(previous)
        content = content[:start] + insertion + content[end:]
    assert hashlib.sha256(content.encode()).hexdigest() == entry['after'], f'Result changed: {name}'
    writes.append((target, content))
for target, content in writes:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
print(f'Verified {len(entries)} files; applied {len(writes)} exact source changes.')
