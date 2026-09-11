"""Audita o aplica organización sin extracción. Ejecutar con Panopticon cerrado."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from third_party.cherry_dl import archives


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    with readonly(Path.home() / '.cherry-dl/index.db') as db:
        projects = db.execute('SELECT display_name, folder_path FROM profiles').fetchall()
    report = []
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    for name, location in projects:
        folder = Path(location).resolve()
        item = dict(project=name, folder=str(folder), moves=[], errors=[])
        report.append(item)
        try:
            with readonly(folder / 'catalog.db') as db:
                rows = db.execute('SELECT hash, filename FROM files').fetchall()
            for digest, filename in rows:
                group = archives.category(filename)
                if not group:
                    continue
                source = archives._inside(folder, filename)
                if source.parent != folder or not source.is_file():
                    continue
                group = archives.file_category(source)
                if not group:
                    continue
                relative = f'{group}/{source.name}'
                target = archives._inside(folder, relative)
                if target.exists():
                    raise FileExistsError(f'Destino ocupado: {target}')
                item['moves'].append(dict(hash=digest, old=filename, new=relative,
                                          size=source.stat().st_size))
            item['categories'] = dict(Counter(m['new'].split('/')[0] for m in item['moves']))
            if args.apply and item['moves']:
                backup = folder / '.cherry-archives' / f'backup-organize-{stamp}.db'
                archives._inside(folder, backup.relative_to(folder).as_posix())
                backup.parent.mkdir(exist_ok=True)
                with readonly(folder / 'catalog.db') as src, sqlite3.connect(backup) as dst:
                    src.backup(dst)
                item['backup'] = str(backup)
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
                result = await archives.organize_project(folder)
                item['organized'] = result.organized
                with readonly(folder / 'catalog.db') as db:
                    after = dict(db.execute('SELECT hash, filename FROM files'))
                planned = {m['hash']: m['new'] for m in item['moves']}
                for digest, filename in rows:
                    assert after[digest] == planned.get(digest, filename), filename
                for move in item['moves']:
                    assert not (folder / move['old']).exists(), move
                    assert (folder / move['new']).stat().st_size == move['size'], move
                item['verified'] = True
        except Exception as exc:
            item['errors'].append(str(exc))
        print(json.dumps({k: v for k, v in item.items() if k != 'moves'}), flush=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
    print(json.dumps(dict(projects=len(report), planned=sum(len(i['moves']) for i in report),
                         organized=sum(i.get('organized', 0) for i in report),
                         errors=sum(len(i['errors']) for i in report))))
    return 1 if any(i['errors'] for i in report) else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
