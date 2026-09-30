"""Exportación manual y reporte de conflictos. No aplica cambios a la central."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.curation_sidecar import export_curation, compare_curation_page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('export', 'compare'))
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--folder', required=True, type=Path)
    args = parser.parse_args()
    if args.operation == 'export':
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
