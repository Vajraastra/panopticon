"""Nombres en disco: la extensión que manda el sitio no puede romper ni esconder la descarga."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from third_party.cherry_dl.downloads import build_filename, safe_extension
from third_party.cherry_dl.file_routing import route, routed_name

HOSTILE = [
    "sketch v1.5: final",          # ':' tras el último punto → flujo alternativo en NTFS
    "pack.p:ng",
    "notes 1.2*draft",             # caracteres ilegales en Windows
    "what?.v2?",
    'comilla.a"b',
    "flecha.a<b>c",
    "tuberia.a|b",
    "title. " + "palabra " * 40,   # «extensión» larguísima con espacios
]


class SafeExtension(unittest.TestCase):
    def test_normal_names_are_unchanged(self):
        for original, expected in [("archivo.JPG", ".jpg"), ("a.b.PSD", ".psd"), ("x.procreate", ".procreate"),
                                   ("v1.2.zip", ".zip"), ("sin_extension", ""), ("foto.jpg ", ".jpg")]:
            self.assertEqual(safe_extension(original), expected, original)
        self.assertEqual(build_filename("ViciNeko", 1, "x.JPG"), "ViciNeko_00001.jpg")
        self.assertEqual(build_filename("Some Artist", 42, "dibujo.psd"), "Some_Artist_00042.psd")

    def test_hostile_extensions_become_bin(self):
        for original in HOSTILE:
            self.assertEqual(safe_extension(original), ".bin", original)
        self.assertEqual(safe_extension("ñandú.jpgé"), ".bin")      # no ASCII
        self.assertEqual(safe_extension("x." + "a" * 11), ".bin")   # demasiado larga
        self.assertEqual(safe_extension("x." + "a" * 10), "." + "a" * 10)

    def test_every_hostile_name_can_be_written_as_a_plain_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for number, original in enumerate(HOSTILE, 1):
                name = routed_name("Artista", number, original)
                (folder / name).write_bytes(b"contenido")
                self.assertEqual((folder / name).stat().st_size, 9, name)
            # Un archivo por nombre, ninguno de 0 bytes (flujo alternativo) ni con ':' en el nombre.
            files = list(folder.iterdir())
            self.assertEqual(len(files), len(HOSTILE))
            self.assertTrue(all(f.stat().st_size == 9 and ":" not in f.name for f in files))

    def test_folder_and_name_agree_for_odd_extensions(self):
        # El nombre construido y la carpeta deben salir de la misma extensión saneada.
        self.assertEqual(route("foto.jpg ").folder, route("foto.jpg").folder)
        self.assertEqual(route("x.p:ng"), route(routed_name("A", 1, "x.p:ng")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
