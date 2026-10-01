"""Read-only inspection of one explicit Metadata Hub/Optimizer/Cropper journal."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.producer_journal_review import review


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,help='Native database; opened read-only')
    parser.add_argument('--journal',required=True,help='One explicit operation folder/journal.json')
    args=parser.parse_args(argv)
    try:
        result=review(args.db,args.journal)
    except Exception as error:
        print(json.dumps(dict(status='review_failed',error=str(error)[:1000]),ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['stable'] else 1


if __name__=='__main__':
    raise SystemExit(main())
