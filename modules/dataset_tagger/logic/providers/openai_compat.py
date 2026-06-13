"""
Provider para cualquier endpoint compatible con la API de OpenAI:
LM Studio, Ollama (modo OpenAI), vLLM, llama.cpp server, etc.

`base_url` debe terminar en '/v1' (p. ej. http://localhost:1234/v1). Cubre el
caso local de la v1 del Dataset Tagger sin añadir dependencias (solo requests).
"""
import logging
import requests
from .base_provider import BaseProvider, ProviderError, encode_image

log = logging.getLogger(__name__)


class OpenAICompatProvider(BaseProvider):
    name = "openai_compat"

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def list_models(self):
        try:
            r = requests.get(f"{self.base_url}/models", headers=self._headers(),
                             timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise ProviderError(f"No se pudo listar modelos en {self.base_url}: {e}") from e
        try:
            data = r.json().get("data", [])
        except ValueError as e:
            raise ProviderError(f"Respuesta no-JSON de {self.base_url}/models: {e}") from e
        return [m.get("id") for m in data if m.get("id")]

    def caption(self, image_path, prompt, model=None):
        model = model or self.model
        if not model:
            raise ProviderError("No se especificó un modelo para el captioning.")
        data_uri, _ = encode_image(image_path)
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }],
            "temperature": 0.2,
            "max_tokens": 512,
        }
        try:
            r = requests.post(f"{self.base_url}/chat/completions", headers=self._headers(),
                              json=payload, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise ProviderError(f"Fallo en el captioning: {e}") from e
        try:
            return r.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError(f"Respuesta inesperada del servidor: {e}") from e
