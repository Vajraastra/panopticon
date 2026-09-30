"""Exportación/reporte y cuarentena explícita de recursos propios; central sin cambios."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.curation_sidecar import export_curation, compare_curation_page
from core.curation_recovery import inspect_recovery, quarantine_recovery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('export', 'compare', 'inspect-recovery', 'quarantine-recovery'))
    parser.add_argument('--db', type=Path)
    parser.add_argument('--folder', required=True, type=Path)
    parser.add_argument('--expected-fingerprint')
    args = parser.parse_args()
    if args.operation in ('export', 'compare') and args.db is None:
        parser.error('--db is required for export/compare')
    if args.operation == 'quarantine-recovery' and args.expected_fingerprint is None:
        parser.error('--expected-fingerprint is required after inspect-recovery')
    if args.operation == 'inspect-recovery':
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
                print(json.dumps(report))
                count += 1
        if count != expected:
            raise ValueError('sidecar_count_mismatch')
        print(json.dumps(dict(report_complete=True, export_id=token, copies=count)))


if __name__ == '__main__':
    main()
