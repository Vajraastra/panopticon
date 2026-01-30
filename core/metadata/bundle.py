"""
MetadataBundle: Estructura de datos unificada para metadata de imágenes.
Centraliza prompts de IA, tags de organización y datos de calidad.
"""
from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass
class MetadataBundle:
    """
    Contenedor unificado para todos los tipos de metadata de imagen.
    
    Agrupa:
    - Datos de generación de IA (prompts, modelo, seed, etc.)
    - Datos de organización de Panopticon (tags, rating, quality)
    - Metadata raw para preservación
    
    Ejemplo:
        bundle = MetadataBundle(
            positive_prompt="1girl, beautiful, detailed",
            negative_prompt="bad quality, blurry",
            tool="A1111",
            tags=["portrait", "anime"],
            rating=4
        )
    """
    
    # ===== DATOS DE GENERACIÓN DE IA =====
    positive_prompt: str = ""
    negative_prompt: str = ""
    
    # Parámetros técnicos de generación
    model: str = ""
    seed: str = ""
    steps: str = ""
    cfg: str = ""
    sampler: str = ""
    vae: str = ""
    loras: list = field(default_factory=list)
    
    # Herramienta de origen
    tool: str = "Unknown"  # A1111, ComfyUI, NAI, Forge, etc.
    
    # ===== DATOS DE PANOPTICON =====
    tags: list = field(default_factory=list)
    rating: int = 0  # 0-5 stars
    quality_score: int = 0  # 0-100 from Quality Scorer
    
    # ===== METADATA RAW =====
    raw: dict = field(default_factory=dict)
    source_format: str = ""  # PNG, JPEG, WEBP
    
    # ===== MÉTODOS DE VALIDACIÓN =====
    
    def is_valid(self) -> bool:
        """
        Verifica si el bundle tiene datos esenciales.
        Un bundle es válido si tiene al menos prompts O tags.
        """
        return bool(self.positive_prompt or self.tags)
    
    def has_prompts(self) -> bool:
        """Verifica si tiene datos de generación de IA."""
        return bool(self.positive_prompt or self.negative_prompt)
    
    def has_panopticon_data(self) -> bool:
        """Verifica si tiene datos de organización de Panopticon."""
        return bool(self.tags or self.rating > 0 or self.quality_score > 0)
    
    def has_generation_params(self) -> bool:
        """Verifica si tiene parámetros técnicos de generación."""
        return bool(self.model or self.seed or self.steps)
    
    # ===== MÉTODOS DE COMPARACIÓN =====
    
    def compare(self, other: 'MetadataBundle') -> dict:
        """
        Compara este bundle con otro y retorna diferencias.
        
        Args:
            other: Otro MetadataBundle para comparar
        
        Returns:
            dict con campos que difieren: {campo: (self_value, other_value)}
        """
        differences = {}
        
        # Campos a comparar
        fields_to_compare = [
            'positive_prompt', 'negative_prompt', 'model', 'seed',
            'steps', 'cfg', 'sampler', 'tool', 'rating', 'quality_score'
        ]
        
        for field_name in fields_to_compare:
            self_val = getattr(self, field_name)
            other_val = getattr(other, field_name)
            if self_val != other_val:
                differences[field_name] = (self_val, other_val)
        
        # Comparar tags (como set para ignorar orden)
        if set(self.tags) != set(other.tags):
            differences['tags'] = (self.tags, other.tags)
        
        # Comparar loras
        if set(self.loras) != set(other.loras):
            differences['loras'] = (self.loras, other.loras)
        
        return differences
    
    def integrity_score(self, other: 'MetadataBundle') -> float:
        """
        Calcula un score de integridad (0-100) comparando con otro bundle.
        100 = idénticos, 0 = completamente diferentes.
        """
        differences = self.compare(other)
        
        # Pesos por campo (algunos son más críticos)
        weights = {
            'positive_prompt': 30,
            'negative_prompt': 15,
            'tags': 25,
            'rating': 10,
            'quality_score': 5,
            'model': 5,
            'seed': 5,
            'steps': 2,
            'cfg': 2,
            'sampler': 1,
        }
        
        total_weight = sum(weights.values())
        lost_weight = sum(weights.get(field, 1) for field in differences.keys())
        
        return round((1 - lost_weight / total_weight) * 100, 1)
    
    # ===== MÉTODOS DE SERIALIZACIÓN =====
    
    def to_dict(self) -> dict:
        """Convierte el bundle a diccionario para serialización."""
        return {
            'positive_prompt': self.positive_prompt,
            'negative_prompt': self.negative_prompt,
            'model': self.model,
            'seed': self.seed,
            'steps': self.steps,
            'cfg': self.cfg,
            'sampler': self.sampler,
            'vae': self.vae,
            'loras': self.loras.copy(),
            'tool': self.tool,
            'tags': self.tags.copy(),
            'rating': self.rating,
            'quality_score': self.quality_score,
            'source_format': self.source_format,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'MetadataBundle':
        """Crea un bundle desde un diccionario."""
        return cls(
            positive_prompt=data.get('positive_prompt', ''),
            negative_prompt=data.get('negative_prompt', ''),
            model=data.get('model', ''),
            seed=str(data.get('seed', '')),
            steps=str(data.get('steps', '')),
            cfg=str(data.get('cfg', '')),
            sampler=data.get('sampler', ''),
            vae=data.get('vae', ''),
            loras=data.get('loras', []).copy(),
            tool=data.get('tool', 'Unknown'),
            tags=data.get('tags', []).copy(),
            rating=int(data.get('rating', 0)),
            quality_score=int(data.get('quality_score', 0)),
            source_format=data.get('source_format', ''),
            raw=data.get('raw', {}),
        )
    
    def __str__(self) -> str:
        """Representación legible del bundle."""
        parts = []
        if self.tool != "Unknown":
            parts.append(f"Tool: {self.tool}")
        if self.positive_prompt:
            prompt_preview = self.positive_prompt[:50] + "..." if len(self.positive_prompt) > 50 else self.positive_prompt
            parts.append(f"Prompt: {prompt_preview}")
        if self.tags:
            parts.append(f"Tags: {', '.join(self.tags[:5])}")
        if self.rating:
            parts.append(f"Rating: {'★' * self.rating}")
        if self.quality_score:
            parts.append(f"Quality: {self.quality_score}%")
        
        return f"MetadataBundle({'; '.join(parts)})" if parts else "MetadataBundle(empty)"
