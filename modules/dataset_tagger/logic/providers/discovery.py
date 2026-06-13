"""
Descubrimiento de APIs locales de inferencia.

El usuario pulsa "Descubrir" y esto escanea los endpoints locales conocidos
(todos exponen API compatible con OpenAI en /v1) y devuelve los que respondan,
con sus modelos. Pensado para ejecutarse desde un hilo (timeouts cortos), nunca
en el arranque ni en el hilo GUI.
"""
import logging
import requests
from .base_provider import looks_like_vision

log = logging.getLogger(__name__)

# (etiqueta, base_url). Puertos por defecto de los runtimes locales más comunes.
KNOWN_ENDPOINTS = [
    ("LM Studio", "http://localhost:1234/v1"),
    ("Ollama", "http://localhost:11434/v1"),
    ("vLLM", "http://localhost:8000/v1"),
    ("llama.cpp", "http://localhost:8080/v1"),
]


def probe_endpoint(base_url, timeout=1.5):
    """
    Sondea un endpoint OpenAI-compatible. Retorna la lista de model IDs si
    responde 200 en /models, o None si no contesta / no es compatible.
    """
    try:
        r = requests.get(f"{base_url.rstrip('/')}/models", timeout=timeout)
        if r.status_code != 200:
            return None
        return [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except (requests.RequestException, ValueError):
        return None


def discover_local(timeout=1.5):
    """
    Escanea KNOWN_ENDPOINTS. Para cada uno que responda devuelve
    {label, base_url, models, vision_models}. Lista vacía si no hay nada.
    """
    found = []
    for label, base in KNOWN_ENDPOINTS:
        models = probe_endpoint(base, timeout=timeout)
        if models is None:
            continue
        vision = [m for m in models if looks_like_vision(m)]
        found.append({
            "label": label,
            "base_url": base,
            "models": models,
            "vision_models": vision,
        })
        log.info("Descubierto %s en %s (%d modelos, %d de visión)",
                 label, base, len(models), len(vision))
    return found
