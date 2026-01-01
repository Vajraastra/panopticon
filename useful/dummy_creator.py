import os
import shutil
import argparse
from PIL import Image

# =========================
# CONFIGURACIÓN
# =========================

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
DUMMY_SIZE = (32, 32)
DUMMY_COLOR = (128, 128, 128)
ORIGINALS_DIR = "originals"

# =========================
# UTILIDADES
# =========================

def is_image_file(filename):
    return os.path.splitext(filename.lower())[1] in IMAGE_EXTENSIONS

def create_dummy_image(path, extension):
    img = Image.new("RGB", DUMMY_SIZE, DUMMY_COLOR)

    if extension in {".jpg", ".jpeg"}:
        img.save(path, "JPEG", quality=20, optimize=True)
    elif extension == ".png":
        img.save(path, "PNG", optimize=True)
    elif extension == ".webp":
        img.save(path, "WEBP", quality=20)
    elif extension == ".bmp":
        img.save(path, "BMP")
    else:
        raise ValueError(f"Formato no soportado: {extension}")

# =========================
# PROCESAMIENTO
# =========================

def process_folder(base_path, dry_run=False):
    if not os.path.isdir(base_path):
        raise ValueError(f"Path inválido: {base_path}")

    originals_path = os.path.join(base_path, ORIGINALS_DIR)

    if dry_run:
        print(f"[DRY-RUN] Se crearía carpeta: {originals_path}")
    else:
        os.makedirs(originals_path, exist_ok=True)

    for filename in os.listdir(base_path):
        full_path = os.path.join(base_path, filename)

        if not os.path.isfile(full_path):
            continue

        if not is_image_file(filename):
            continue

        original_target = os.path.join(originals_path, filename)

        if os.path.exists(original_target):
            continue

        name, ext = os.path.splitext(filename)

        if dry_run:
            print(f"[DRY-RUN] Movería: {filename} → originals/")
            print(f"[DRY-RUN] Crearía dummy: {filename}")
        else:
            shutil.move(full_path, original_target)
            create_dummy_image(full_path, ext.lower())
            print(f"[OK] Dummy creado: {filename}")

    print("\nProceso finalizado.")

# =========================
# ENTRY POINT
# =========================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dummy Creator para scrapers")
    parser.add_argument("path", help="Carpeta a procesar")
    parser.add_argument("--dry-run", action="store_true", help="Simula el proceso sin modificar archivos")

    args = parser.parse_args()
    process_folder(args.path, args.dry_run)
