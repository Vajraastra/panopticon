"""
Worker de captioning en QThread.

Itera las imágenes, llama al provider (VLM), ensambla el caption con la
plantilla del modelo y escribe los sidecars en la(s) carpeta(s) de salida.
Soporta modo dual (varias plantillas → varias carpetas), política de
existentes, reintentos básicos, progreso y cancelación.

Nota: el worker no construye strings de UI; emite datos crudos y la vista
traduce. Las plantillas/meta-prompts ya vienen resueltas desde los presets.
"""
import logging
from PySide6.QtCore import QThread, Signal

from .templates import CaptionTemplate, with_content_mode
from . import sidecar
from .providers.base_provider import ProviderError

log = logging.getLogger(__name__)


class CaptionWorker(QThread):
    progress = Signal(int, int, str)   # done, total, filename actual
    image_done = Signal(str, str)      # image_path, caption final (último formato escrito)
    error = Signal(str, str)           # image_path, mensaje
    finished_all = Signal(int, int)    # procesadas, saltadas

    def __init__(self, provider, model, source_folder, template_keys,
                 policy=sidecar.SKIP, trigger="", prefix="", suffix="",
                 extra_prefix_tags=None, recursive=False, retries=2,
                 images=None, prompt_overrides=None, providers=None,
                 model_names=None, nsfw=False, parent=None):
        super().__init__(parent)
        self.provider = provider               # provider por defecto (VLM)
        # Provider específico por plantilla {key: provider}. Permite que un
        # dataset de tags use el WD tagger (ONNX local) y el natural el VLM en
        # la misma corrida (modo "Both"). Si una key no está, usa el default.
        self.providers = providers or {}
        self.model = model
        # Nombre de modelo por plantilla {key: name}. Sirve tanto para nombrar la
        # carpeta de salida (<base>_<name>_<fmt>) como para el parámetro `model`
        # del provider. Así cada dataset refleja su motor (p.ej. el tagger WD vs
        # el VLM del endpoint). Si una key no está, usa `model`.
        self.model_names = model_names or {}
        self.source_folder = source_folder
        self.template_keys = list(template_keys)   # ["pony"] o dual ["pony", "flux"]
        self.policy = policy
        self.trigger = trigger
        self.prefix = prefix
        self.suffix = suffix
        self.extra_prefix_tags = extra_prefix_tags or []
        self.recursive = recursive
        self.retries = retries
        self._images = [str(p) for p in images] if images else None
        # Prompts custom por plantilla {key: meta_prompt}. Vacío/ausente => el
        # default del preset. La UI puede editar el meta-prompt antes de iniciar.
        self.prompt_overrides = prompt_overrides or {}
        self.nsfw = nsfw                        # sube el tono del meta-prompt VLM
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        # 1. lista de imágenes
        try:
            if self._images is not None:
                images = self._images
            else:
                images = [str(p) for p in sidecar.list_images(self.source_folder, self.recursive)]
        except Exception as e:  # noqa: BLE001
            self.error.emit("", f"No se pudieron listar imágenes: {e}")
            self.finished_all.emit(0, 0)
            return

        # 2. preparar plantilla + carpeta de salida por cada formato objetivo
        prepared = []
        try:
            for key in self.template_keys:
                tmpl = CaptionTemplate(key)
                mname = self.model_names.get(key, self.model)   # nombre por motor
                out = sidecar.prepare_output(self.source_folder, mname, tmpl.format)
                # Prompt efectivo: override del usuario si lo hay (no vacío),
                # si no el meta-prompt default del preset.
                ov = (self.prompt_overrides.get(key) or "").strip()
                prompt = with_content_mode(ov or tmpl.meta_prompt(), self.nsfw)
                prov = self.providers.get(key, self.provider)  # motor por plantilla
                prepared.append((tmpl, out, prompt, prov, mname))
        except Exception as e:  # noqa: BLE001
            self.error.emit("", f"No se pudo preparar la salida: {e}")
            self.finished_all.emit(0, 0)
            return

        total = len(images)
        processed = skipped = 0

        for i, img in enumerate(images, 1):
            if self._cancel:
                break
            self.progress.emit(i, total, img.replace("\\", "/").rsplit("/", 1)[-1])

            wrote_any = False
            for tmpl, out, prompt, prov, mname in prepared:
                cap_path = sidecar.caption_path(out, img)
                do, mode = sidecar.should_process(cap_path, self.policy)
                if not do:
                    continue
                caption = self._caption_with_retry(img, prompt, prov, mname)
                if caption is None:
                    continue  # error ya emitido
                final = tmpl.assemble(
                    caption, trigger=self.trigger, prefix=self.prefix,
                    suffix=self.suffix, extra_prefix_tags=self.extra_prefix_tags,
                )
                sidecar.copy_image(img, out)
                sidecar.write_caption(cap_path, final, mode, sep=tmpl.separator)
                self.image_done.emit(img, final)
                wrote_any = True

            if wrote_any:
                processed += 1
            else:
                skipped += 1

        self.finished_all.emit(processed, skipped)

    def _caption_with_retry(self, img, prompt, provider=None, model=None):
        provider = provider or self.provider
        model = model or self.model
        last = None
        for _ in range(self.retries + 1):
            if self._cancel:
                return None
            try:
                return provider.caption(img, prompt, model=model)
            except ProviderError as e:
                last = str(e)
            except Exception as e:  # noqa: BLE001 — robustez razonable
                last = str(e)
        self.error.emit(img, last or "fallo desconocido")
        return None
