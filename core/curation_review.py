"""Páginas de comparación y vistas de texto acotadas para la revisión Qt."""
import json
from core.curation_sidecar import compare_curation_page
from core.curation_restore import fingerprint

TEXT_LIMIT = 16384


def _preview(state):
    if state is None:
        return dict(text='', truncated=False, tags=0)
    header = {key: state[key] for key in ('hash_original', 'hash_mod', 'rating')}
    lines = [json.dumps(header, ensure_ascii=False, indent=2)[:-2] + ',\n  "tags": [']
    size = sum(map(len, lines))
    truncated = False
    for tag in state['tags']:
        # Un solo tag ya tiene un límite de campo en el lector del sidecar.
        line = ('    ' if len(lines) == 1 else '    ,') + json.dumps(tag, ensure_ascii=False)
        if size + len(line) + 8 > TEXT_LIMIT:
            truncated = True
            break
        lines.append(line)
        size += len(line) + 1
    lines.append('  ]\n}')
    return dict(text='\n'.join(lines), truncated=truncated, tags=len(state['tags']))


class CurationReviewTasks:
    def __init__(self, db_path):
        self.db_path = str(db_path)

    def read(self, operation, *, folder, after=0, export_id=None, cancel_event=None):
        if operation != 'compare_curation':
            raise ValueError('unknown_curation_task')
        limit = 100
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError('curation_cancelled')
            try:
                value = compare_curation_page(self.db_path, folder, after=after,
                                             limit=limit, export_id=export_id)
                break
            except ValueError as error:
                if str(error) != 'sidecar_payload_too_large' or limit == 1:
                    raise
                limit = max(1, limit // 2)
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError('curation_cancelled')
        reports = []
        for report in value['reports']:
            reports.append(dict(relative_path=report['relative_path'], status=report['status'],
                copy_id=report['sidecar']['id'],
                fingerprint=fingerprint(report['central']) if report['central'] is not None else None,
                sidecar=_preview(report['sidecar']), central=_preview(report['central'])))
        return dict(export_id=value['export_id'], total=value['total'], after=value['after'], reports=reports)
