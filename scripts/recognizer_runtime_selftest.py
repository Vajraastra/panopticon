"""Carga e inferencia reales con modelos locales, sin red ni biblioteca del usuario."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from modules.character_recognizer.logic.recognition_engine import RecognitionEngine


class RuntimeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = RecognitionEngine()
        with patch.object(cls.engine, "_download_file", side_effect=AssertionError("No downloads")), \
             patch.object(cls.engine, "_download_buffalo_l", side_effect=AssertionError("No downloads")):
            cls.engine.initialize()

    def test_local_models_load(self):
        self.assertTrue(self.engine.initialized)
        self.assertFalse(self.engine.anime_cascade.empty())

    def test_both_modes_run_detection_and_embedding(self):
        # Una imagen vacía recorre YuNet, el fallback AnimeFace y ArcFace.
        # Comprueba compatibilidad de ejecución; no mide precisión de identidad.
        image = np.zeros((320, 320, 3), dtype=np.uint8)
        for mode in ("real", "illustration"):
            with self.subTest(mode=mode):
                embedding, box, score = self.engine.analyze_image(image, mode=mode)
                self.assertEqual(embedding.shape, (512,))
                self.assertTrue(np.isfinite(embedding).all())
                self.assertAlmostEqual(float(np.linalg.norm(embedding)), 1.0, places=5)
                self.assertEqual(box, (0, 0, 320, 320))
                self.assertEqual(score, 0.1)


if __name__ == "__main__":
    unittest.main()
