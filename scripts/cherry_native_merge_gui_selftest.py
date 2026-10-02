"""Offscreen checks for merge confirmation and partial-effect notifications."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from modules.cherry.logic import merge
from modules.cherry.ui.duplicates_dialog import DuplicatesDialog, MergeConfirmDialog
from modules.cherry.ui.view import CherryView

APP=QApplication.instance() or QApplication([])


class MergeGUI(unittest.TestCase):
    def plan(self):return merge.MergePlan(1,2,'older','newer',Path('C:/private/older'),Path('C:/private/newer'),duplicated=1)
    def dialog(self,service=None):
        runtime=SimpleNamespace(is_running=False)
        with patch.object(DuplicatesDialog,'_scan'):
            dialog=DuplicatesDialog(runtime,data_service=service)
        self.addCleanup(dialog.close)
        return dialog

    def test_confirmation_preserves_orphans_and_disables_unsafe_options(self):
        dialog=MergeConfirmDialog(self.plan()); self.addCleanup(dialog.close)
        self.assertEqual(dialog.orphan_action(),merge.ORPHAN_IGNORE)
        buttons={button.property('orphan_action'):button for button in dialog._orphans.buttons()}
        self.assertTrue(buttons[merge.ORPHAN_IGNORE].isEnabled())
        for action in (merge.ORPHAN_DELETE,merge.ORPHAN_RENAME):
            self.assertFalse(buttons[action].isEnabled()); self.assertIn('Duplicate Finder',buttons[action].toolTip())

    def test_failed_ack_notifies_both_scopes_and_does_not_claim_intact_folders(self):
        dialog=self.dialog(); notifications=[]; dialog.folders_changed.connect(notifications.append)
        result=merge.MergeResult(self.plan(),changed_paths=['C:/private/older/output.png'],errors=['ack failed'])
        dialog._on_merged(result,{})
        self.assertEqual(notifications,[['C:\\private\\older','C:\\private\\newer']])
        self.assertNotIn('intact',dialog._status.text()); self.assertIn('ack failed',dialog._status.text())

    def test_runtime_operation_receives_shared_native_service(self):
        service=object(); dialog=self.dialog(service)
        result=merge.MergeResult(self.plan(),errors=['private'])
        with patch('modules.cherry.logic.merge.merge_pair',new=AsyncMock(return_value=result)) as call:
            asyncio.run(dialog._merge_async({},merge.ORPHAN_IGNORE))
        self.assertIs(call.call_args.kwargs['data_service'],service)

    def test_cannot_open_merge_during_download_or_without_native_service(self):
        for queue,bridge,service in [(True,False,object()),(False,True,object()),(False,False,None)]:
            runtime=SimpleNamespace(start=lambda:self.fail('runtime must not start'))
            view=SimpleNamespace(_maintenance_busy=False,_preparing_download=False,
                _queue=SimpleNamespace(is_running=queue),_bridge=SimpleNamespace(is_busy=bridge),
                context={'data_service':service},_runtime=runtime)
            CherryView._open_duplicates(view)


if __name__=='__main__':unittest.main()
