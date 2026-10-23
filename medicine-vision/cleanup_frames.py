"""Opt-in cleanup of old terminal-task JPEGs. No inference/runtime imports."""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import re
import sqlite3
import time


def cleanup(data_dir, older_than_hours=24, apply=False, now=None):
    if not math.isfinite(older_than_hours) or older_than_hours <= 0:
        raise ValueError('older_than_hours must be finite and positive')
    root = Path(data_dir).resolve()
    database = root / 'vision_test.sqlite3'
    if not database.is_file():
        raise ValueError('Existing OCR database not found; refusing to create an empty one')
    frames = root / 'frames'
    if frames.is_symlink() or frames.resolve().parent != root:
        raise ValueError('Frames directory must be inside the configured data directory')
    report = dict(dry_run=not apply, eligible=0, deleted=0, missing=0, skipped=0)
    cutoff = (time.time() if now is None else now) - older_than_hours * 3600
    with closing(sqlite3.connect(database, timeout=5)) as db, db:
        # Serializes against state changes/uploads. Terminal rows cannot be reactivated.
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("""SELECT f.path FROM vision_frames f JOIN vision_tasks t
            ON f.task_id=t.task_id WHERE t.status IN ('succeeded','failed','timed_out')
            AND t.updated_at < ?""", (cutoff,)).fetchall()
        for (name,) in rows:
            path = Path(name)
            if (not path.is_absolute() or path.is_symlink()
                    or path.parent.resolve() != frames.resolve()
                    or not re.fullmatch(r'[a-f0-9]{32}\.jpg', path.name)):
                report['skipped'] += 1
                continue
            if not path.is_file():
                report['missing'] += 1
                continue
            report['eligible'] += 1
            if apply:
                path.unlink()
                report['deleted'] += 1
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--older-than-hours', type=float, default=24)
    parser.add_argument('--apply', action='store_true', help='Permanently delete eligible JPEGs')
    args = parser.parse_args()
    print(json.dumps(cleanup(args.data_dir, args.older_than_hours, args.apply)))


if __name__ == '__main__':
    main()
