"""Validate the authenticated website response before confirming scan coverage."""
from urllib.parse import parse_qs, urljoin, urlparse


def validate_page(body, campaign_id, seen_cursors):
    if (not isinstance(body, dict) or body.get('errors')
            or not isinstance(body.get('data'), list)):
        raise RuntimeError('Patreon devolvió una página incompleta; no se confirma el recorrido.')
    posts = body['data']
    included = body.get('included', [])
    links = body.get('links') or {}
    if not isinstance(included, list) or not isinstance(links, dict):
        raise RuntimeError('Patreon devolvió relaciones o enlaces inválidos.')
    for post in posts:
        if (not isinstance(post, dict) or post.get('type') != 'post'
                or not post.get('id') or not isinstance(post.get('attributes'), dict)):
            raise RuntimeError('Patreon devolvió una publicación inválida.')
        relationships = post.get('relationships') or {}
        if not isinstance(relationships, dict):
            raise RuntimeError('Patreon devolvió relaciones inválidas.')
        campaign = relationships.get('campaign')
        if campaign is not None:
            if (not isinstance(campaign, dict) or not isinstance(campaign.get('data'), dict)
                    or str(campaign['data'].get('id')) != str(campaign_id)):
                raise RuntimeError('Patreon devolvió una publicación de otra campaña.')
    for item in included:
        if (not isinstance(item, dict) or not item.get('type') or not item.get('id')
                or not isinstance(item.get('attributes', {}), dict)):
            raise RuntimeError('Patreon devolvió un recurso incluido inválido.')
    next_url = links.get('next')
    if next_url is None:
        return posts, included, None
    if isinstance(next_url, dict):
        next_url = next_url.get('href')
    if not isinstance(next_url, str) or not next_url:
        raise RuntimeError('Patreon devolvió un enlace de continuación inválido.')
    parsed = urlparse(urljoin('https://www.patreon.com/api/posts', next_url))
    if (parsed.scheme != 'https' or parsed.netloc not in {'www.patreon.com', 'patreon.com'}
            or parsed.path != '/api/posts'):
        raise RuntimeError('Patreon devolvió una continuación fuera de su API de publicaciones.')
    cursors = parse_qs(parsed.query).get('page[cursor]', [])
    if len(cursors) != 1 or not cursors[0] or cursors[0] in seen_cursors or not posts:
        raise RuntimeError('La paginación de Patreon no avanzó; se conserva la frontera anterior.')
    seen_cursors.add(cursors[0])
    return posts, included, cursors[0]
