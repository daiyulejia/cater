"""Read-only startup metadata, not a database or release authenticity check."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from . import __version__


def capture_instance(db_path: str | Path, *, ui_version: str | None,
                     startup_recovery: bool, custom_runtime: bool, instance_id: str) -> dict:
    kind, locator = 'unknown', None
    if custom_runtime:
        kind = 'custom'
    else:
        try:
            configured = os.fspath(db_path)
            if configured == ':memory:':
                kind = 'memory'
            elif isinstance(configured, str) and configured and not configured.startswith('file:'):
                # Lexical startup location only: no filesystem, symlink or DB reads.
                location = os.path.normcase(os.path.abspath(configured))
                locator = hashlib.sha256(location.encode('utf-8')).hexdigest()
                kind = 'file'
        except (TypeError, ValueError, OSError, UnicodeError):
            pass
    return {'schema_version': 1, 'instance_id': instance_id,
        'package_version': __version__, 'backend_build_id': None,
        'ui_version': ui_version, 'startup_recovery': startup_recovery,
        'database': {'kind': kind, 'locator_id': locator}}
