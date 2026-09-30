"""Preferencia configurable por evidencia. Sólo recomendaciones; nunca borra."""
from dataclasses import dataclass
import hashlib
import re
from core.duplicate_snapshot import encode

GENERATION = ('meta_tool', 'meta_model', 'meta_positive', 'meta_negative', 'meta_seed')
PROVENANCE = ('origin_url', 'origin_site', 'artist')
SHA = re.compile('[0-9a-f]{64}')


@dataclass(frozen=True)
class DuplicatePolicy:
    manual_tag_weight: int = 40
    other_tag_weight: int = 10
    rating_weight: int = 20
    generation_weight: int = 10
    provenance_weight: int = 1

    def __post_init__(self):
        if any(type(value) is not int or not 0 <= value <= 1000 for value in vars(self).values()):
            raise ValueError('invalid_duplicate_policy_weight')


def _meaningful(value):
    return value not in (None, '', 'Unknown')


def _data(record):
    state = record['state']
    file = state['file']
    tags = frozenset((tag['name'], tag['source'], tag['category']) for tag in state['tags'])
    rating = file['rating'] or 0
    if type(rating) is not int or not 0 <= rating <= 5:
        raise ValueError('invalid_duplicate_rating')
    generation = {name: file.get(name) for name in GENERATION if _meaningful(file.get(name))}
    provenance = {name: file.get(name) for name in PROVENANCE if _meaningful(file.get(name))}
    curated = bool(tags or rating or generation)
    return dict(tags=tags, rating=rating, generation=generation, provenance=provenance, curated=curated)


def _contains(winner, other):
    if not other['tags'] <= winner['tags']:
        return False
    # Un cero de una copia curada también puede ser deliberado; no tratarlo como hueco.
    if (other['curated'] or other['rating']) and winner['rating'] != other['rating']:
        return False
    for kind in ('generation', 'provenance'):
        if any(winner[kind].get(name) != value for name, value in other[kind].items()):
            return False
    return True


def recommend(copies, *, verified_hashes, policy=None):
    """Grupo de hasta256 estados leídos; SHA actuales los aporta el worker verificador.

La huella del estado se conserva para la aprobación posterior; no prueba los bytes.
Companions/sidecars externos o estados ausentes requieren revisión. Ponderaciones
jamás autorizan perder información ni permiten que una cruda sustituya a una curada.
"""
    policy = DuplicatePolicy() if policy is None else policy
    if (not isinstance(policy, DuplicatePolicy) or not isinstance(copies, (list, tuple)) or
            not 2 <= len(copies) <= 256 or not isinstance(verified_hashes, dict)):
        raise ValueError('invalid_duplicate_group')
    paths = [record['path'] for record in copies]
    if len(set(paths)) != len(paths):
        raise ValueError('duplicate_group_path_repeated')
    def review(reason):
        return dict(status='review', reason=reason, preferred=None, keep=tuple(paths), remove=())
    if len({path.casefold() for path in paths}) != len(paths):
        return review('ambiguous_path_identity')
    values = []
    digests = []
    for record in copies:
        path = record['path']
        digest = verified_hashes.get(path)
        if not isinstance(digest, str) or not SHA.fullmatch(digest):
            return review('current_bytes_unverified')
        digests.append(digest)
        state = record.get('state')
        if state is None:
            return review('copy_not_registered')
        file = state['file']
        if file['path'] != path or file.get('is_offline') != 0:
            return review('copy_not_active')
        if record.get('external_documentation'):
            return review('external_documentation_requires_review')
        if not isinstance(record.get('fingerprint'), str) or not SHA.fullmatch(record['fingerprint']):
            return review('state_unreviewable')
        known = file.get('hash_mod') or file.get('hash_original')
        if not isinstance(known, str) or known.lower() != digest:
            return review('catalog_identity_mismatch')
        if hashlib.sha256(encode(state)).hexdigest() != record['fingerprint']:
            return review('state_unreviewable')
        values.append(_data(record))
    if len(set(digests)) != 1:
        return review('different_current_bytes')
    def priority(index):
        value = values[index]
        manual = sum(source == 'manual' for _, source, _ in value['tags'])
        score = (manual * policy.manual_tag_weight + (len(value['tags']) - manual) * policy.other_tag_weight +
                 value['rating'] * policy.rating_weight + len(value['generation']) * policy.generation_weight +
                 len(value['provenance']) * policy.provenance_weight)
        # Curación precede a ponderaciones; empate estable, nunca orden de inventario.
        return -int(value['curated']), -score, paths[index].casefold(), paths[index]
    preferred = paths[min(range(len(values)), key=priority)]
    eligible = [i for i, candidate in enumerate(values) if all(_contains(candidate, other) for other in values)]
    if not eligible:
        result = review('complementary_or_conflicting_data')
        result['preferred'] = preferred  # Ayuda a revisión, sin marcar descartes.
        return result
    winner = min(eligible, key=priority)
    return dict(status='recommended', reason='complete_information_preserved', preferred=paths[winner], keep=(paths[winner],),
                remove=tuple(path for index, path in enumerate(paths) if index != winner))
