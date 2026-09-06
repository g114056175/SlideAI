"""Upgrade existing projects in place to the shared large/thumbnail image pair."""
import argparse
import json
from pathlib import Path

from backend.app.services.artifact_store import VideoRunStore
from backend.app.services.page_images import ensure_run_images


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    store = VideoRunStore(args.root.resolve())
    failed = []
    for manifest_path in sorted(store.root.glob('*/manifest.json')):
        run_id = manifest_path.parent.name
        try:
            ensure_run_images(store, run_id)
            count = len(store.load_manifest(run_id).get('pages') or [])
            print(json.dumps({'run_id': run_id, 'pages': count, 'status': 'ready'}), flush=True)
        except Exception as exc:
            failed.append(run_id)
            print(json.dumps({'run_id': run_id, 'status': 'failed', 'error': str(exc)}), flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
