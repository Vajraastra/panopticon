"""Shared input decoding for image recognition."""

from PIL import Image, ImageOps


MAX_PIXELS = 50_000_000


def read_rgb(source):
    """Decode the first frame as oriented RGB from a path or open binary stream."""
    with Image.open(source) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("image_size")
        return ImageOps.exif_transpose(image).convert("RGB")
