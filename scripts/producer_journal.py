"""Read-only inspection of one explicit Metadata Hub/Optimizer/Cropper journal."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.producer_journal_review import review
from core.producer_reconciliation import reconcile
from core.library_service import LibraryService


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,help='Native database; opened read-only')
    parser.add_argument('--journal',required=True,help='One explicit operation folder/journal.json')
    parser.add_argument('--reconcile',action='store_true',help='Explicitly reconcile this reviewed operation; never restores/deletes files')
    parser.add_argument('--expected-review-fingerprint',help='Exact fingerprint from a fresh read-only review')
    args=parser.parse_args(argv)
    if args.reconcile!=bool(args.expected_review_fingerprint):
        parser.error('--reconcile requires --expected-review-fingerprint, and vice versa')
    try:
        result=review(args.db,args.journal)
        if args.reconcile:
            if not result['stable'] or result['review_fingerprint']!=args.expected_review_fingerprint:
                raise ValueError('producer_reconciliation_changed_since_review')
            service=LibraryService(args.db,require_ready=True)
            try:
                result=reconcile(service,args.journal,expected_review_fingerprint=args.expected_review_fingerprint)
            finally:
                service.close()
    except Exception as error:
        print(json.dumps(dict(status='reconciliation_failed' if args.reconcile else 'review_failed',error=str(error)[:1000]),ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if args.reconcile:
        return 0 if result['status']=='complete' else 1
    return 0 if result['stable'] else 1


if __name__=='__main__':
    raise SystemExit(main())
