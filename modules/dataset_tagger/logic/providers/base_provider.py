"""
Interfaz común de un backend de captioning (provider) y utilidades compartidas.

Un provider sabe: listar modelos, probar la conexión y generar un caption para
una imagen dada un prompt. La capa de UI/worker no debe conocer detalles del
backend concreto — solo esta interfaz.
"""
import base64
import mimetypes
from pathlib import Path

# Heurística best-effort de modelos de visión conocidos (familias VLM comunes
# servidas vía API OpenAI-compatible). NO es exhaustiva ni autoritativa: sirve
# para sugerir/filtrar en la UI, nunca para bloquear al usuario.
VISION_HINTS = (
    "llava", "-vl", "vl-", "vl:", "vision", "moondream", "minicpm-v", "minicpm-o",
    "pixtral", "internvl", "gemma3", "gemma-3", "gemma4", "gemma-4",
    "qwen2-vl", "qwen2.5-vl", "qwen3-vl",
    "llama-3.2-vision", "llama3.2-vision", "cogvlm", "glm-4v", "phi-3-vision",
    "phi-3.5-vision", "smolvlm", "florence", "joycaption", "molmo",
)


class ProviderError(RuntimeError):
    """Error de comunicación o respuesta inesperada de un provider."""


def encode_image(path):
    """
    Lee una imagen y la devuelve como data URI base64 (sin redimensionar —
    Panopticon ya hizo el preprocesamiento aguas arriba). Retorna (data_uri, mime).
    """
    p = Path(path)
    mime, _ = mimetypes.guess_type(p.name)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    data = base64.standard_b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}", mime


def looks_like_vision(model_id):
    """¿El nombre del modelo sugiere capacidad de visión? True/False/None."""
    if not model_id:
        return None
    m = model_id.lower()
    return any(h in m for h in VISION_HINTS)


class BaseProvider:
    """Backend abstracto de captioning."""
    name = "base"

    def __init__(self, base_url, api_key=None, model=None, timeout=120):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def list_models(self):
        """Lista de IDs de modelo disponibles en el backend."""
        raise NotImplementedError

    def caption(self, image_path, prompt, model=None):
        """Genera y retorna el caption (str) para una imagen."""
        raise NotImplementedError

    def complete(self, prompt, model=None):
        """Completa un prompt SOLO de texto (sin imagen). Retorna el texto.

        Lo usa la sugerencia de prosa del editor (corrige/estructura/traduce un
        caption ya escrito sin re-mirar la imagen). Un provider sin LLM de texto
        (p. ej. el WD tagger) puede dejarlo sin implementar.
        """
        raise NotImplementedError

    def test_connection(self):
        """Prueba la conexión. Retorna (ok: bool, mensaje: str)."""
        try:
            models = self.list_models()
            return True, f"OK — {len(models)} modelo(s) disponible(s)"
        except Exception as e:  # noqa: BLE001 — superficie de error para la UI
            return False, str(e)

    def is_vision(self, model=None):
        """Heurística de visión para el modelo dado (o el configurado)."""
        return looks_like_vision(model or self.model)
