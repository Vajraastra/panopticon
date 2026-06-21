"""
Sugerencia de prosa para el editor de captions (Fase 2 del QoL de edición).

A diferencia del autocompletado de tags (sin LLM, `tag_engine`), los datasets de
PROSA (Flux / Z-Image / Ideogram) se editan en lenguaje natural. Aquí el LLM sí
ayuda, pero SIEMPRE a demanda y como propuesta aceptar/rechazar (nunca automático):
un solo botón "Sugerir" que en una pasada **corrige gramática + reestructura para
el modelo destino + traduce al inglés**.

Por defecto trabaja SOLO con el texto (barato, arregla forma e idioma sin re-mirar
la imagen). Un checkbox opcional "usar imagen" deja que el VLM corrija también los
hechos en capturas imperfectas.

El trabajo va en `RefineWorker(QThread)` (Regla de Oro #4): la llamada al LLM no
debe congelar la GUI.
"""
import logging
from PySide6.QtCore import QThread, Signal

from .providers.base_provider import ProviderError

log = logging.getLogger(__name__)


def build_prompt(text, structure="", label=""):
    """Arma el prompt de refinamiento para un caption de prosa.

    `structure` (del preset del modelo destino) describe la forma deseada;
    `label` es el nombre del modelo, solo para contexto. El LLM debe devolver
    ÚNICAMENTE el caption mejorado, sin comentarios ni comillas.
    """
    target = f" for the «{label}» text-to-image model" if label else ""
    rules = [
        "You are an expert prompt editor for text-to-image datasets.",
        f"Rewrite the following image caption{target}.",
        "Tasks, in one pass:",
        "1. Fix grammar, spelling and awkward phrasing.",
        "2. Translate it to natural English if it is in another language.",
    ]
    if structure:
        rules.append(f"3. Restructure it to follow this target form: {structure}")
    rules += [
        "Keep every concrete fact already present; do NOT invent details or add "
        "anything not implied by the original text.",
        "Return ONLY the improved caption as plain text — no preamble, no quotes, "
        "no markdown, no explanation.",
        "",
        "Caption to improve:",
        text.strip(),
    ]
    return "\n".join(rules)


# Instrucción para el modo "usar imagen": el VLM mira la foto y corrige hechos
# además de forma/idioma. Se manda como prompt junto a la imagen (provider.caption).
def build_image_prompt(text, structure="", label=""):
    base = build_prompt(text, structure, label)
    return (base + "\n\nUse the attached image to correct any factual error "
            "(wrong colors, miscounts, missing or hallucinated elements).")


class RefineWorker(QThread):
    """Pide la sugerencia al LLM fuera del hilo GUI.

    Si `image_path` está dado usa el VLM (corrige hechos mirando la imagen);
    si no, una completion solo-texto (más barata, arregla forma e idioma).
    """
    ready = Signal(str)        # texto sugerido
    failed = Signal(str)       # mensaje de error

    def __init__(self, provider, model, text, structure="", label="",
                 image_path=None, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.model = model
        self.text = text
        self.structure = structure
        self.label = label
        self.image_path = image_path

    def run(self):
        try:
            if self.image_path:
                prompt = build_image_prompt(self.text, self.structure, self.label)
                out = self.provider.caption(self.image_path, prompt, model=self.model)
            else:
                prompt = build_prompt(self.text, self.structure, self.label)
                out = self.provider.complete(prompt, model=self.model)
            out = (out or "").strip().strip('"').strip()
            if not out:
                self.failed.emit("El modelo devolvió una sugerencia vacía.")
                return
            self.ready.emit(out)
        except ProviderError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 — superficie de error para la UI
            log.error("Fallo en la sugerencia de prosa: %s", e)
            self.failed.emit(str(e))
