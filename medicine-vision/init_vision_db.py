"""Initialize the local integration SQLite schema without deleting records."""
import argparse
import json
from pathlib import Path
from vision_service.config import Settings
from vision_service.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    settings = Settings(data_dir=args.data_dir.resolve()) if args.data_dir else Settings.from_env()
    store = Store(settings.database)
    store.initialize()
    print(json.dumps(dict(database=str(settings.database), tasks=store.ping(), purpose='local integration/testing'), indent=2))


if __name__ == '__main__':
    main()
