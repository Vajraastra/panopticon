"""
Provider WD tagger: clasificador ONNX local (SmilingWolf) en vez de un VLM.

A diferencia del provider OpenAI-compatible, NO llama a ningún endpoint ni
inventa tags: corre un modelo ONNX en local y emite SOLO tags reales del
vocabulario de Danbooru con que fue entrenado, por encima de un umbral de
confianza. Resuelve la alucinación de tags del LLM e incluye el NSFW canónico
de fábrica (sin censura ni rechazos).

Preprocesado fiel a la referencia oficial del Space de SmilingWolf:
- pad a cuadrado con fondo BLANCO (255), centrado,
- resize a target_size (lo da el input shape del modelo, 448 en eva02) BICUBIC,
- float32 en rango 0-255 (SIN normalizar), RGB -> BGR, layout NHWC [1,H,W,3].

Reglas del framework respetadas:
- La carga del ONNX (pesada) es LAZY y ocurre dentro de caption(), que corre en
  el QThread del worker — nunca en el hilo GUI (Regla de Oro #4).
- Sin dependencias nuevas: onnxruntime, numpy y Pillow ya están en el stack; el
  CSV se lee con el módulo `csv` de la stdlib (no se usa pandas).
"""
import csv
import logging

import numpy as np
from PIL import Image

from .base_provider import BaseProvider, ProviderError

log = logging.getLogger(__name__)

# Categorías del selected_tags.csv (convención SmilingWolf).
CAT_GENERAL = 0
CAT_CHARACTER = 4
CAT_RATING = 9

DEFAULT_SIZE = 448  # fallback si el modelo declara dimensión dinámica


class WDTaggerProvider(BaseProvider):
    """Backend de captioning por clasificación ONNX (tags booru reales)."""
    name = "wd_tagger"

    def __init__(self, model_path, csv_path, general_threshold=0.35,
                 character_threshold=0.85, replace_underscore=True,
                 include_rating=False):
        super().__init__(base_url="", model=str(model_path))
        self.model_path = str(model_path)
        self.csv_path = str(csv_path)
        self.general_threshold = float(general_threshold)
        # floor de seguridad como en la referencia oficial
        self.character_threshold = max(0.15, float(character_threshold))
        self.replace_underscore = replace_underscore
        self.include_rating = include_rating

        self._session = None
        self._input_name = None
        self._label_name = None
        self._target_size = DEFAULT_SIZE
        self._names = []        # nombre de tag por índice de salida
        self._cats = []         # categoría por índice de salida

    # -- carga perezosa -------------------------------------------------------
    def _ensure_loaded(self):
        if self._session is not None:
            return
        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(self.model_path)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"No se pudo cargar el modelo ONNX: {e}") from e

        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        self._label_name = self._session.get_outputs()[0].name
        # input shape NHWC: [batch, H, W, C]; H/W pueden venir dinámicas
        try:
            h = inp.shape[1]
            if isinstance(h, int) and h > 0:
                self._target_size = h
        except (IndexError, TypeError):
            pass

        self._load_tags()

    def _load_tags(self):
        names, cats = [], []
        try:
            with open(self.csv_path, encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    names.append(row["name"])
                    cats.append(int(row.get("category", CAT_GENERAL)))
        except (OSError, KeyError, ValueError) as e:
            raise ProviderError(f"No se pudo leer el CSV de tags: {e}") from e
        self._names = names
        self._cats = cats

    # -- preprocesado ---------------------------------------------------------
    def _preprocess(self, image_path):
        img = Image.open(image_path)
        if img.mode != "RGB":
            # aplanar transparencia sobre blanco para no meter halos negros
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                img = Image.alpha_composite(bg, img).convert("RGB")
            else:
                img = img.convert("RGB")

        w, h = img.size
        m = max(w, h)
        square = Image.new("RGB", (m, m), (255, 255, 255))
        square.paste(img, ((m - w) // 2, (m - h) // 2))
        if m != self._target_size:
            square = square.resize((self._target_size, self._target_size), Image.BICUBIC)

        arr = np.asarray(square, dtype=np.float32)
        arr = arr[:, :, ::-1]                 # RGB -> BGR
        return np.expand_dims(arr, axis=0)    # NHWC [1,H,W,3]

    # -- API de provider ------------------------------------------------------
    def list_models(self):
        return [self.model_path]

    def test_connection(self):
        try:
            self._ensure_loaded()
            return True, f"OK — {len(self._names)} tags, entrada {self._target_size}px"
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def is_vision(self, model=None):
        return True

    def caption(self, image_path, prompt=None, model=None):
        """Devuelve 'tag1, tag2, ...' (character + general sobre umbral).

        `prompt` y `model` se ignoran: un clasificador no usa meta-prompt.
        """
        self._ensure_loaded()
        try:
            batch = self._preprocess(image_path)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"No se pudo preparar la imagen: {e}") from e

        try:
            preds = self._session.run([self._label_name], {self._input_name: batch})[0][0]
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"Fallo en la inferencia ONNX: {e}") from e

        general, character = [], []
        for i, prob in enumerate(preds):
            cat = self._cats[i]
            if cat == CAT_GENERAL and prob > self.general_threshold:
                general.append((self._names[i], float(prob)))
            elif cat == CAT_CHARACTER and prob > self.character_threshold:
                character.append((self._names[i], float(prob)))

        # más confiable primero; personaje/copyright antes que generales
        character.sort(key=lambda x: x[1], reverse=True)
        general.sort(key=lambda x: x[1], reverse=True)
        tags = [name for name, _ in character] + [name for name, _ in general]

        if self.replace_underscore:
            tags = [t.replace("_", " ") if len(t) > 3 else t for t in tags]

        return ", ".join(tags)
