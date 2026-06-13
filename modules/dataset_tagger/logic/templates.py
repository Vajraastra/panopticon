"""
Plantillas de caption por modelo de generación.

Carga `presets/model_templates.json` (Fase 1) y, para un modelo dado:
- expone el `meta_prompt` que recibe el VLM,
- ensambla el caption final a partir de la salida cruda del VLM, aplicando
  quality_prefix + (tags extra: rating/source) + trigger + prefix/suffix del
  usuario, con deduplicación cuando el formato es de tags.
"""
import json
from pathlib import Path

PRESETS_PATH = Path(__file__).resolve().parent.parent / "presets" / "model_templates.json"

# Modificador de "tono" reutilizable que se concatena al meta-prompt cuando el
# usuario marca el dataset como NSFW. Único para todos los modelos (DRY): solo
# sube el tono e indica explícitamente qué se espera de la descripción. Aplica
# al VLM (un clasificador WD ignora el prompt y ya da NSFW canónico de fábrica).
NSFW_SUFFIX = (
    "This dataset is adult (NSFW) content. Describe any sexual or explicit "
    "elements directly and accurately with precise, uncensored terminology; "
    "do not omit, soften, censor or refuse. Include anatomy, acts and state of "
    "dress exactly as visible."
)


def with_content_mode(prompt, nsfw=False):
    """Devuelve el meta-prompt con el modificador NSFW añadido si procede.

    Idempotente: si el sufijo ya viene en el prompt (la UI lo muestra en vivo en
    el editor y lo manda como override) no se duplica.
    """
    if nsfw and prompt and NSFW_SUFFIX not in prompt:
        return f"{prompt.rstrip()} {NSFW_SUFFIX}"
    return prompt


def load_presets(path=None):
    with open(path or PRESETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def available_models(presets=None, include_deferred=False):
    """Lista de (key, label) de modelos seleccionables."""
    presets = presets or load_presets()
    out = []
    for key, cfg in presets["models"].items():
        if cfg.get("_deferred") and not include_deferred:
            continue
        out.append((key, cfg.get("label", key)))
    return out


class CaptionTemplate:
    def __init__(self, model_key, presets=None):
        presets = presets or load_presets()
        if model_key not in presets["models"]:
            raise KeyError(f"Modelo desconocido en presets: {model_key}")
        self.key = model_key
        self.cfg = presets["models"][model_key]
        self.format = self.cfg.get("format", "tags")          # "tags" | "natural"
        self.separator = self.cfg.get("separator", ", ")
        self.quality_prefix = list(self.cfg.get("quality_prefix", []))
        self.trigger_position = self.cfg.get("trigger_position", "start")

    def meta_prompt(self):
        return self.cfg["meta_prompt"]

    # -- ensamblado -------------------------------------------------------
    def assemble(self, vlm_output, trigger="", prefix="", suffix="", extra_prefix_tags=None):
        if self.format == "tags":
            return self._assemble_tags(vlm_output, trigger, prefix, suffix, extra_prefix_tags)
        return self._assemble_natural(vlm_output, trigger, prefix, suffix)

    @staticmethod
    def _split_tags(text):
        return [t.strip() for t in (text or "").split(",") if t.strip()]

    def _assemble_tags(self, vlm_output, trigger, prefix, suffix, extra_prefix_tags):
        parts = list(self.quality_prefix)
        if extra_prefix_tags:
            parts += [t.strip() for t in extra_prefix_tags if t and t.strip()]
        if trigger and self.trigger_position in ("start", "after_quality"):
            parts.append(trigger.strip())
        parts += self._split_tags(prefix)
        parts += self._split_tags(vlm_output)
        parts += self._split_tags(suffix)
        if trigger and self.trigger_position == "end":
            parts.append(trigger.strip())
        # dedup preservando orden, case-insensitive
        seen, out = set(), []
        for t in parts:
            k = t.lower()
            if k and k not in seen:
                seen.add(k)
                out.append(t)
        return self.separator.join(out)

    def _assemble_natural(self, vlm_output, trigger, prefix, suffix):
        segs = []
        if trigger and self.trigger_position in ("start", "after_quality"):
            segs.append(trigger.strip())
        if prefix:
            segs.append(prefix.strip())
        if vlm_output:
            segs.append(vlm_output.strip())
        if suffix:
            segs.append(suffix.strip())
        if trigger and self.trigger_position == "end":
            segs.append(trigger.strip())
        return " ".join(s for s in segs if s)
