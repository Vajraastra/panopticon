"""pHash vivo de juguete: límites, cancelación y revisión sin retiro/SQL."""
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import imagehash
import numpy as np
from modules.duplicate_finder.logic.visual_review import scan, _hash
from modules.duplicate_finder.logic.review_worker import VisualReviewWorker


class Visual(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.event = threading.Event()
        self.progress = []

    def file(self, name, color='red'):
        path = self.root/name
        Image.new('RGB', (16, 16), color).save(path)
        return path

    def scan(self):
        return scan(str(self.root), 5, lambda *value: self.progress.append(value), self.event)

    def test_live_identical_images_propose_manual_group_never_discards(self):
        a, b = self.file('a.png'), self.file('b.png')
        before = a.read_bytes(), b.read_bytes()
        result = self.scan()
        self.assertTrue(result['complete'])
        self.assertEqual(list(result['groups'].values()), [[a.as_posix(), b.as_posix()]])
        report = next(iter(result['reports'].values()))
        self.assertEqual((report['status'], report['remove'], report['keep']), ('review', (), (a.as_posix(), b.as_posix())))
        self.assertEqual((a.read_bytes(), b.read_bytes()), before)
        self.assertEqual(self.progress[-1][:2], (2, 2))

    def test_no_transitive_union_of_distant_images(self):
        paths = [self.file(f'{index}.png') for index in range(3)]
        def values(path, event):
            count = int(Path(path).stem)*4
            bits = np.zeros(64, dtype=bool)
            bits[:count] = True
            return imagehash.ImageHash(bits.reshape(8, 8)), None
        with patch('modules.duplicate_finder.logic.visual_review._hash', values):
            result = self.scan()
        self.assertEqual(list(result['groups'].values()), [[paths[0].as_posix(), paths[1].as_posix()]])

    def test_invalid_image_reports_incomplete_without_silent_success(self):
        (self.root/'broken.png').write_bytes(b'not image')
        result = self.scan()
        self.assertFalse(result['complete'])
        self.assertEqual((result['issue_count'], result['groups']), (1, {}))

    def test_pixel_budget_checked_before_decode(self):
        path = self.file('a.png')
        class TooLarge:
            width, height = 10001, 1000
            def __enter__(self): return self
            def __exit__(self, *unused): pass
        with (patch('PIL.Image.open', return_value=TooLarge()),
              patch('imagehash.phash', side_effect=AssertionError('decode'))):
            value, error = _hash(path, self.event)
        self.assertIsNone(value)
        self.assertEqual(error, 'duplicate_visual_pixel_limit')

    def test_changed_file_during_phash_rejected(self):
        path = self.file('a.png')
        original = imagehash.phash
        def changed(image):
            value = original(image)
            path.write_bytes(b'changed')
            return value
        with patch('imagehash.phash', changed):
            value, error = _hash(path, self.event)
        self.assertIsNone(value)
        self.assertEqual(error, 'copy_version_file_changed')

    def test_inventory_budget_rejects_before_hash_admission(self):
        for index in range(2001):
            (self.root/f'{index}.png').write_bytes(b'fixture')
        with patch('modules.duplicate_finder.logic.visual_review._hash', side_effect=AssertionError('hash')):
            with self.assertRaisesRegex(ValueError, 'duplicate_visual_inventory_limit'):
                self.scan()

    def test_hash_admission_batches_no_more_than_256(self):
        for index in range(257):
            (self.root/f'{index:03}.png').write_bytes(b'fixture')
        admitted = []
        value = imagehash.ImageHash(np.zeros((8, 8), dtype=bool))
        class Pool:
            def __enter__(self): return self
            def __exit__(self, *unused): pass
            def map(self, function, values):
                values = list(values)
                admitted.append(len(values))
                return [(value, None) for _ in values]
        with patch('modules.duplicate_finder.logic.visual_review.ThreadPoolExecutor', return_value=Pool()):
            result = self.scan()
        self.assertEqual(admitted, [256, 1])
        self.assertEqual(len(next(iter(result['groups'].values()))), 257)

    def test_cancellation_before_inventory_never_hashes(self):
        self.event.set()
        with patch('os.walk', return_value=[(str(self.root), [], [])]):
            with self.assertRaisesRegex(RuntimeError, 'curation_cancelled'):
                self.scan()

    def test_cancel_during_hash_drops_all_partial_groups(self):
        self.file('a.png')
        self.file('b.png')
        def cancelled(path, event):
            event.set()
            return None, None
        with patch('modules.duplicate_finder.logic.visual_review._hash', cancelled):
            with self.assertRaisesRegex(RuntimeError, 'curation_cancelled'):
                self.scan()

    def test_invalid_threshold_rejected_before_qthread_start(self):
        for value in (0, 21, True, 3.2):
            with self.assertRaisesRegex(ValueError, 'invalid_duplicate_visual_threshold'):
                VisualReviewWorker(str(self.root), value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
