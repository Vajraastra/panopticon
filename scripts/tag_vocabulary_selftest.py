"""Vocabulario acotado y Qt con fixtures privados; no modifica biblioteca ni Cherry."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QLineEdit
from core.components.tag_vocabulary import TagVocabulary, TagPageBar
from core.library_tags import page
from core.library_results import ReadResultError
from core.library_service import LibraryError, LibraryService
from core.library_connections import open_reader
from gallery_data_selftest import pump


class PoisonConnection:
    def execute(self, *args):
        raise AssertionError('invalid query reached SQL')
    create_function = execute


class TagVocabularySafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'private.db'
        self.service = LibraryService(self.path)
        self.addCleanup(self.service.close)
        self.controls = []
        self.addCleanup(self.close_controls)

    def sql(self, statement, args=()):
        with closing(sqlite3.connect(self.path)) as conn:
            value = conn.execute(statement, args).fetchall()
            conn.commit()
            return value

    def seed(self, names):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executemany('INSERT INTO tags(name) VALUES (?)', ((name,) for name in names))
            conn.commit()

    def read(self, **kwargs):
        return self.service.read('get_tags_page', **kwargs)

    def controller(self):
        field = QLineEdit()
        controller = TagVocabulary(self.service, field)
        bar = TagPageBar()
        bar.bind(controller)
        self.controls.append((controller, field, bar))
        return controller, field, bar

    def close_controls(self):
        for controller, field, bar in self.controls:
            controller.close()
            self.assertTrue(pump(controller.reader.shutdown_ready))
            field.close()
            bar.close()
            field.deleteLater()
            bar.deleteLater()
        self.app.processEvents()

    def test_invalid_page_query_and_cursor_reject_before_sql(self):
        for kwargs in [dict(limit=-1), dict(limit=201), dict(limit=True), dict(limit=1.0),
                       dict(query=None), dict(query='x'*257), dict(query='x\x00'),
                       dict(after_name=''), dict(after_name=True), dict(after_name='x\x00')]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ReadResultError):
                page(PoisonConnection(), **kwargs)

    def test_keyset_pages_cover_every_tag_in_binary_name_order(self):
        names = [f'tag-{n:04d}' for n in range(501)]
        self.seed(names)
        found, cursor, counts = [], None, []
        while True:
            value = self.read(after_name=cursor)
            counts.append(len(value['names']))
            found.extend(value['names'])
            if not value['more']:
                self.assertIsNone(value['next_name'])
                break
            cursor = value['next_name']
            self.assertEqual(cursor, value['names'][-1])
        self.assertEqual(counts, [200, 200, 101])
        self.assertEqual(found, names)

    def test_exact_page_boundary_and_empty_result_do_not_invent_more(self):
        self.seed([f'tag-{n:04d}' for n in range(200)])
        value = self.read()
        self.assertEqual((len(value['names']), value['more']), (200, False))
        self.assertEqual(self.read(query='absent'), dict(names=[], more=False, next_name=None))

    def test_literal_wildcards_and_unicode_casefold_query(self):
        self.seed(['100%_done', '100anythingdone', 'ÁRBOL', 'Straße', '雪の絵', 'name_1'])
        self.assertEqual(self.read(query='%_')['names'], ['100%_done'])
        self.assertEqual(self.read(query='árb')['names'], ['ÁRBOL'])
        self.assertEqual(self.read(query='STRASSE')['names'], ['Straße'])
        self.assertEqual(self.read(query='雪')['names'], ['雪の絵'])
        self.assertEqual(self.read(query='_')['names'], ['100%_done', 'name_1'])

    def test_query_after_cursor_uses_single_select_without_offset_or_ddl(self):
        self.seed(['alpha', 'beta', 'delta'])
        with closing(open_reader(self.path)) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            value = page(conn, query='a', after_name='alpha', limit=1)
        self.assertEqual(value, dict(names=['beta'], more=True, next_name='beta'))
        selects = [s for s in statements if s.startswith('SELECT')]
        self.assertEqual(len(selects), 1)
        self.assertNotIn('OFFSET', selects[0])
        self.assertFalse(any(s.startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE')) for s in statements))

    def test_legacy_long_tag_is_preserved_and_bounded_response_error_is_explicit(self):
        long_tag = 'legacy-' + 'x'*5000
        self.seed([long_tag, 'short'])
        self.assertEqual(self.read(query='legacy')['names'], [long_tag])
        with patch('core.library_results.MAX_RESULT_BYTES', 100):
            with self.assertRaises(LibraryError) as caught:
                self.read(query='legacy')
        self.assertEqual(caught.exception.reason, 'read_result_too_large')
        self.assertEqual(self.sql('SELECT name FROM tags ORDER BY name'), [(long_tag,), ('short',)])
        self.assertEqual(self.read(query='short')['names'], ['short'])

    def test_malformed_legacy_tag_is_error_not_an_incomplete_vocabulary(self):
        self.seed([sqlite3.Binary(b'invalid')])
        with self.assertRaises(LibraryError) as caught:
            self.read()
        self.assertEqual(caught.exception.reason, 'invalid_tag_vocabulary')

    def test_stats_can_omit_tags_and_do_not_call_unbounded_vocabulary(self):
        self.seed(['alpha'])
        from core.library_repository import LibraryRepository
        with patch.object(LibraryRepository, 'get_all_tags', side_effect=AssertionError('unbounded vocabulary')):
            self.assertEqual(self.service.read('get_library_overview', include_tags=False),
                             dict(folders=[], count=0, tags=[]))
        self.assertEqual(self.service.read('get_library_overview')['tags'], ['alpha'])

    def test_cancel_and_reader_close_have_no_registry_mutations(self):
        self.seed(['alpha'])
        schema = self.sql('SELECT name,sql FROM sqlite_master ORDER BY name')
        event = threading.Event()
        event.set()
        with patch('core.library_service.open_reader', side_effect=AssertionError('cancelled SQL opened')):
            with self.assertRaises(LibraryError) as caught:
                self.service.read('get_tags_page', cancel_event=event)
        self.assertEqual(caught.exception.reason, 'cancelled')
        self.assertEqual(self.sql('SELECT name,sql FROM sqlite_master ORDER BY name'), schema)
        self.assertEqual(self.service.metrics_snapshot()['reads_active'], 0)

    def test_next_previous_pages_are_bounded_and_labels_translated(self):
        self.seed([f'tag-{n:04d}' for n in range(501)])
        controller, field, bar = self.controller()
        controller.refresh()
        self.assertTrue(pump(lambda: not controller.busy))
        self.assertEqual(controller.model.rowCount(), 200)
        self.assertFalse(bar.previous.isEnabled())
        self.assertTrue(bar.next.isEnabled())
        controller.next()
        self.assertTrue(pump(lambda: not controller.busy))
        self.assertEqual(controller.model.stringList()[0], 'tag-0200')
        controller.next()
        self.assertTrue(pump(lambda: not controller.busy))
        self.assertEqual(controller.model.rowCount(), 101)
        self.assertFalse(bar.next.isEnabled())
        controller.previous()
        self.assertTrue(pump(lambda: not controller.busy))
        self.assertEqual(controller.model.stringList()[0], 'tag-0200')
        self.assertTrue(bar.previous.toolTip())
        self.assertTrue(bar.next.toolTip())

    def test_text_change_invalidates_suggestions_immediately_and_resets_cursor(self):
        self.seed([f'tag-{n:04d}' for n in range(301)]+['unique needle'])
        controller, field, bar = self.controller()
        controller.refresh()
        self.assertTrue(pump(lambda: not controller.busy))
        controller.next()
        self.assertTrue(pump(lambda: not controller.busy))
        field.setText('needle')
        self.assertEqual(controller.model.rowCount(), 0)
        self.assertEqual(controller.cursors, [None])
        self.assertTrue(pump(lambda: not controller.busy))
        self.assertEqual(controller.model.stringList(), ['unique needle'])
        self.assertFalse(bar.previous.isEnabled())

    def test_latest_query_wins_during_delayed_previous_result(self):
        self.seed(['alpha', 'beta'])
        controller, field, bar = self.controller()
        entered, release = threading.Event(), threading.Event()
        original = self.service.read
        values = []
        controller.changed.connect(lambda value: values.append(value['names']))
        def blocked(operation, *args, **kwargs):
            value = original(operation, *args, **kwargs)
            if kwargs.get('query') == 'alpha':
                entered.set()
                if not release.wait(3):
                    raise AssertionError('fixture timeout')
            return value
        try:
            with patch.object(self.service, 'read', blocked):
                field.setText('alpha')
                self.assertTrue(pump(entered.is_set))
                field.setText('beta')
                self.assertEqual(controller.model.rowCount(), 0)
                release.set()
                self.assertTrue(pump(lambda: not controller.busy))
        finally:
            release.set()
        self.assertEqual(controller.model.stringList(), ['beta'])
        self.assertEqual(values, [['beta']])

    def test_close_before_debounce_makes_no_query_and_is_idempotent(self):
        controller, field, bar = self.controller()
        with patch.object(self.service, 'read', side_effect=AssertionError('closed suggestions ran')):
            field.setText('pending')
            controller.close()
            controller.close()
            self.assertFalse(controller.timer.isActive())
            controller._request()
            self.app.processEvents()
        self.assertTrue(controller.reader.shutdown_ready())

    def test_error_clears_results_and_does_not_block_manual_tag_entry(self):
        self.seed(['alpha'])
        controller, field, bar = self.controller()
        errors = []
        controller.failed.connect(errors.append)
        with patch.object(self.service, 'read', side_effect=LibraryError('get_tags_page', 'fixture failure')):
            controller.refresh()
            self.assertTrue(pump(lambda: not controller.busy))
        self.assertEqual(len(errors), 1)
        self.assertEqual(controller.model.rowCount(), 0)
        self.assertFalse(bar.next.isEnabled())
        field.setText('any manual tag')
        self.assertEqual(field.text(), 'any manual tag')
        self.assertTrue(pump(lambda: not controller.busy))

    def test_multiple_controllers_keep_independent_filters_and_never_read_on_gui(self):
        self.seed(['alpha', 'beta'])
        first, one, _ = self.controller()
        second, two, _ = self.controller()
        main, calls = threading.get_ident(), []
        original = self.service.read
        def tracked(operation, *args, **kwargs):
            calls.append((operation, threading.get_ident()))
            return original(operation, *args, **kwargs)
        with patch.object(self.service, 'read', tracked):
            one.setText('alpha')
            two.setText('beta')
            self.assertTrue(pump(lambda: not first.busy and not second.busy))
        self.assertEqual(first.model.stringList(), ['alpha'])
        self.assertEqual(second.model.stringList(), ['beta'])
        self.assertTrue(all(operation == 'get_tags_page' and thread != main for operation, thread in calls))

    def test_query_refresh_retains_one_model_and_completer(self):
        self.seed(['alpha'])
        controller, field, bar = self.controller()
        model, completer = controller.model, controller.completer
        for _ in range(5):
            controller.refresh()
            self.assertTrue(pump(lambda: not controller.busy))
        self.assertIs(controller.model, model)
        self.assertIs(field.completer(), completer)
        self.assertEqual(controller.model.rowCount(), 1)


if __name__ == '__main__':
    unittest.main()
