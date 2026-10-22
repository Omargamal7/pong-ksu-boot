#!/usr/bin/env bash
# Replace the unreliable Azure Ubuntu mirror without changing other repositories.
set -euo pipefail
SUDO=()
if [ "$(id -u)" != 0 ]; then SUDO=(sudo); fi
"${SUDO[@]}" python3 - <<'PY'
from pathlib import Path
paths = [Path('/etc/apt/sources.list'), *Path('/etc/apt').glob('apt-mirrors*.txt')]
for pattern in ('*.list', '*.sources'):
    paths.extend(Path('/etc/apt/sources.list.d').glob(pattern))
for path in paths:
    if not path.is_file():
        continue
    original = path.read_text()
    changed = original.replace('http://azure.archive.ubuntu.com/ubuntu', 'https://archive.ubuntu.com/ubuntu')
    changed = changed.replace('https://azure.archive.ubuntu.com/ubuntu', 'https://archive.ubuntu.com/ubuntu')
    if changed != original:
        path.write_text(changed)
        print('Switched Ubuntu Azure mirror to archive.ubuntu.com:', path)
PY
