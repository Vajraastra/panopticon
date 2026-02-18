from psd_tools import PSDImage
import numpy as np
import cv2

class PSDEngine:
    def __init__(self):
        self.psd = None
        self.visible_layers = set() # Set of layer IDs (or names if unique)
        self._flat_cache = None

    def load_psd(self, path):
        """Loads a PSD file."""
        try:
            self.psd = PSDImage.open(path)
            # Initialize visibility based on PSD settings
            self.visible_layers.clear()
            self._recurse_init_visibility(self.psd)
            return True, f"Loaded {len(self.visible_layers)} visible layers."
        except Exception as e:
            return False, str(e)

    def _recurse_init_visibility(self, layer):
        """Recursively sets initial visibility."""
        if layer.is_visible():
            # ID generation: We need a reliable ID. Using name for now, but index path describes structure better.
            # For simplicity in V1, let's use the object hash or a custom ID map.
            # Using python's id() is risky across reloads, but okay for a session. 
            # Better: Store reference to layer object in UI Item.
            self.visible_layers.add(id(layer))
            
            if hasattr(layer, 'is_group') and layer.is_group():
                for child in layer:
                    self._recurse_init_visibility(child)

    def set_layer_visibility(self, layer, visible):
        """Toggles visibility for a specific layer object."""
        if visible:
            self.visible_layers.add(id(layer))
        else:
            self.visible_layers.discard(id(layer))
            
        # Optimization: If we just toggle one layer, we might not need to re-render everything 
        # if we had a smart cache, but psd-tools composite is heavy.
        # For V1, we will re-compose.

    def render_composite(self):
        """
        Renders the composite image based on current visibility.
        Returns: PIL Image
        """
        if not self.psd: return None
        
        # We need to construct a robust visibility function for the composite method
        # psd-tools composite() allows passing a 'layer_filter' callback.
        
        def visible_filter(layer):
            # Always render root
            if layer == self.psd: return True
            # Check if this layer is set to visible in our tracking set
            is_vis = id(layer) in self.visible_layers
            
            # Logic: If a Group is NOT visible, its children should effectively be hidden 
            # by the renderer, but the callback usually visits everyone.
            # psd-tools might handle group visibility logic, but let's be strict.
            return is_vis

        try:
            # force=True might be needed if cached composite is used
            # as_PIL returns a PIL image
            return self.psd.composite(layer_filter=visible_filter)
        except Exception as e:
            print(f"Render Error: {e}")
            return None

    def get_preview_image(self):
        """Returns a fast preview (OpenCV format) for UI."""
        pil_img = self.render_composite()
        if not pil_img: return None
        
        # Convert PIL (RGB) to OpenCV (BGR)
        # Handle different modes (RGBA, CMYK, etc)
        if pil_img.mode == 'CMYK':
            pil_img = pil_img.convert('RGB')
        
        # Ensure RGBA for transparency
        pil_img = pil_img.convert('RGBA')
        
        arr = np.array(pil_img)
        # Convert RGB to BGR (keeping Alpha)
        # PIL is RGBA -> OpenCV BGRA
        img_bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA)
        return img_bgr

    def get_structure(self):
        """Returns the PSD root for traversal."""
        return self.psd
