"""Explicit Native path-evidence preparation; no file moves, merges or deletions."""
import argparse
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='Existing ready Native Panopticon DB')
    parser.add_argument('--apply', action='store_true', help='Back up and prepare additive path evidence explicitly')
    parser.add_argument('--resolve', help='Inspect one existing local image after preparation')
    args = parser.parse_args()
    event = threading.Event()
    service = LibraryService(args.db, require_ready=True)
    try:
        print(json.dumps(service.read('get_path_identity_status'), ensure_ascii=False))
        if args.apply:
            try:
                result = service.backfill_path_identity(cancel_event=event,
                    progress=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True))
            except KeyboardInterrupt:
                event.set()
                raise
            print(json.dumps(result, ensure_ascii=False))
        if args.resolve:
            print(json.dumps(service.resolve_path_identity(args.resolve), ensure_ascii=False))
    finally:
        event.set()
        service.close()


if __name__ == '__main__':
    main()
