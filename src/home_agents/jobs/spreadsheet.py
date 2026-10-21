"""Spreadsheet work (S1.10.3): always on a copy; overwriting an original needs approval."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path


class NeedsApproval(Exception):
    pass


def working_copy(original: Path, work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / f"{original.stem}.copy-{datetime.now():%Y%m%d-%H%M%S}{original.suffix}"
    shutil.copy2(original, dest)
    return dest


def replace_original(original: Path, edited: Path, approved: bool) -> Path:
    if not approved:
        raise NeedsApproval(f"overwriting {original.name} needs your approval in the app")
    backup = original.with_name(f"{original.stem}.before-agent{original.suffix}")
    shutil.copy2(original, backup)
    shutil.copy2(edited, original)
    return backup
