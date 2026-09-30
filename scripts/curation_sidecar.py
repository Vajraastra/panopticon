"""Curación portable: exportar, revisar, elegir una copia y recuperar recursos propios."""
import argparse
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.curation_sidecar import export_curation, compare_curation_page
from core.curation_recovery import inspect_recovery, quarantine_recovery
from core.curation_restore import fingerprint
from core.curation_apply import CurationApplyTasks
from core.library_connections import open_reader
from core.library_service import LibraryService, LibraryError, require_ready_schema
from contextlib import closing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('export', 'compare', 'apply', 'inspect-recovery', 'quarantine-recovery'))
    parser.add_argument('--db', type=Path)
    parser.add_argument('--folder', required=True, type=Path)
    parser.add_argument('--expected-fingerprint')
    parser.add_argument('--mode', choices=('restore', 'register', 'adopt'))
    parser.add_argument('--copy-id', type=int)
    parser.add_argument('--relative-path')
    parser.add_argument('--export-id')
    args = parser.parse_args()
    if args.operation in ('export', 'compare', 'apply') and args.db is None:
        parser.error('--db is required for export/compare/apply')
    if args.operation == 'quarantine-recovery' and args.expected_fingerprint is None:
        parser.error('--expected-fingerprint is required after inspect-recovery')
    if args.operation == 'apply':
        if args.mode is None or args.copy_id is None or args.relative_path is None or args.export_id is None:
            parser.error('apply requires --mode, --copy-id, --relative-path and --export-id from compare')
        if args.mode == 'register' and args.expected_fingerprint is not None:
            parser.error('register requires a reviewed absent path, without --expected-fingerprint')
        if args.mode != 'register' and args.expected_fingerprint is None:
            parser.error('restore/adopt require --expected-fingerprint from compare')
        try:
            path = args.db.resolve(strict=True)
            # Preflight RO; incluso una DB desconocida no llega al dueño RW.
            with closing(open_reader(path)) as reader:
                require_ready_schema(reader)
                if reader.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                    raise ValueError('library_not_ready')
            service = LibraryService(path, require_ready=True)
            try:
                result = CurationApplyTasks(service).read('apply_curation', folder=args.folder,
                    copy_id=args.copy_id, relative_path=args.relative_path, export_id=args.export_id,
                    expected_fingerprint=args.expected_fingerprint, mode=args.mode)
                if result is not True:
                    raise ValueError('restore_not_confirmed')
            finally:
                service.close()
        except (LibraryError, ValueError, OSError, sqlite3.Error) as error:
            parser.exit(1, json.dumps(dict(error=getattr(error, 'reason', str(error)))) + '\n')
        print(json.dumps(dict(applied=True, mode=args.mode, relative_path=args.relative_path)))
    elif args.operation == 'inspect-recovery':
        print(json.dumps(inspect_recovery(args.folder)))
    elif args.operation == 'quarantine-recovery':
        print(json.dumps(quarantine_recovery(args.folder, expected_fingerprint=args.expected_fingerprint)))
    elif args.operation == 'export':
        print(json.dumps(export_curation(args.db, args.folder)))
    else:
        after = 0
        token = None
        limit = 256
        count = 0
        expected = None
        while True:
            try:
                page = compare_curation_page(args.db, args.folder, after=after, limit=limit, export_id=token)
            except ValueError as error:
                if str(error) != 'sidecar_payload_too_large' or limit == 1:
                    raise
                limit //= 2
                continue
            token, after = page['export_id'], page['after']
            if expected is None:
                expected = page['total']
            elif expected != page['total']:
                raise ValueError('sidecar_changed')
            if not page['reports']:
                break
            for report in page['reports']:
                report.update(export_id=token, copy_id=report['sidecar']['id'],
                    fingerprint=fingerprint(report['central']) if report['central'] is not None else None)
                print(json.dumps(report))
                count += 1
        if count != expected:
            raise ValueError('sidecar_count_mismatch')
        print(json.dumps(dict(report_complete=True, export_id=token, copies=count)))


if __name__ == '__main__':
    main()
