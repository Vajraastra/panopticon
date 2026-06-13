"""
Presets de WD taggers (clasificadores ONNX de SmilingWolf).

Carga `presets/tagger_models.json` y expone, para un tagger dado, los datos que
necesitan la descarga (Pieza 2) y el provider ONNX (Pieza 3): repo de HuggingFace,
nombre del .onnx, nombre del .csv casado y los thresholds por categoría.

Reglas:
- El CSV está CASADO al modelo (traduce índice de salida -> nombre de tag). Este
  módulo nunca los separa: viajan siempre juntos en el mismo preset.
- No importa nada de otros módulos (solo stdlib); coherente con templates.py.
"""
import json
from pathlib import Path

PRESETS_PATH = Path(__file__).resolve().parent.parent / "presets" / "tagger_models.json"


def load_tagger_presets(path=None):
    with open(path or PRESETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def available_taggers(presets=None):
    """Lista de (key, label) de taggers seleccionables en la UI."""
    presets = presets or load_tagger_presets()
    return [(key, cfg.get("label", key)) for key, cfg in presets["taggers"].items()]


class TaggerPreset:
    """Configuración de un WD tagger (modelo + csv casado + thresholds)."""

    def __init__(self, tagger_key, presets=None):
        presets = presets or load_tagger_presets()
        if tagger_key not in presets["taggers"]:
            raise KeyError(f"Tagger desconocido en presets: {tagger_key}")
        self.key = tagger_key
        self.cfg = presets["taggers"][tagger_key]
        self.label = self.cfg.get("label", tagger_key)
        self.hf_repo = self.cfg["hf_repo"]
        self.model_file = self.cfg.get("model_file", "model.onnx")
        self.csv_file = self.cfg.get("csv_file", "selected_tags.csv")
        self.general_threshold = float(self.cfg.get("general_threshold", 0.35))
        self.character_threshold = float(self.cfg.get("character_threshold", 0.85))
        self.vocab = self.cfg.get("vocab", "danbooru")
