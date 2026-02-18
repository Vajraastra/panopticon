from core.base_module import BaseModule
from core.components.standard_layout import StandardToolLayout
from .ui.composer_view import LayerComposerView

class LayerComposerModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "PSD Composer"
        self._description = "Extract, toggle, and export layers from PSD files."
        self._icon = "🎨"  # Art/Layers icon
        self.accent_color = "#bd93f9" # Purple (Cyberpunk Info/Manual)
        self.view = None

    def get_view(self):
        if self.view: return self.view
        
        # Lazy Loading
        self.content_view = LayerComposerView(self.context)
        self.view = self.content_view
        return self.view

    def load_image_set(self, paths: list):
        if not self.view: self.get_view()
        # Pass the first valid PSD to the view
        for p in paths:
            if p.lower().endswith(('.psd', '.psb')):
                self.content_view.load_psd(p)
                break
