"""Keep an independent pre-upgrade copy before extending the legacy database."""

import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def backup_legacy(data: Path, config):
    target = data / "upgrade_v0_4_backup"
    database = data / "auto_memory.sqlite3"
    if target.exists() or not database.exists():
        return
    with closing(
        sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    ) as source:
        if source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_records'"
        ).fetchone():
            return
        staging = Path(tempfile.mkdtemp(prefix=".upgrade-backup-", dir=data))
        try:
            with closing(sqlite3.connect(staging / database.name)) as destination:
                source.backup(destination)
            (staging / "config.json").write_text(
                json.dumps(dict(config), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if (data / "backups").is_dir():
                shutil.copytree(data / "backups", staging / "backups")
            for path in staging.rglob("*"):
                path.chmod(0o700 if path.is_dir() else 0o600)
            staging.rename(target)
        except BaseException:
            shutil.rmtree(staging)
            raise
