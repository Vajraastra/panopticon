"""Diario y acciones explícitas; sólo consume evidencia, sin SQL/stat de la GUI."""
from pathlib import Path
from PySide6.QtCore import Qt,Signal
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,
    QPushButton,QFileDialog,QTreeWidget,QTreeWidgetItem,QPlainTextEdit,QHeaderView)
from core.theme import Theme


class RecoveryPanel(QWidget):
    changed=Signal()
    selection_changed=Signal()
    restore_requested=Signal()
    purge_preview_requested=Signal()
    purge_requested=Signal()

    def __init__(self,text,accent,color=None):
        super().__init__()
        self.text=text
        self.result=None
        self.purge_preview=None
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        note=QLabel(text('recovery_review_note','Review one retirement journal explicitly. Restoration and permanent deletion are separate manual actions. Deletion requires a fresh preview and confirmation of the selected copy; the recorded phase alone proves nothing.'))
        note.setWordWrap(True);layout.addWidget(note)
        row=QHBoxLayout()
        self.journal=QLineEdit()
        self.journal.setPlaceholderText(text('recovery_review_path','Absolute path to recovery/job/journal.json'))
        self.journal.setToolTip(text('recovery_review_path_tip','Paste the journal path shown after retirement. One complete group is reviewed; changing the path discards the displayed evidence and never starts work automatically.'))
        self.journal.setStyleSheet(Theme.get_input_style());self.journal.textChanged.connect(self.changed)
        self.choose=QPushButton(text('recovery_review_choose','Choose journal'))
        self.choose.setToolTip(text('recovery_review_choose_tip','Select an existing journal.json. Verification starts only when you press Review recovery journal. Files and portable Cherry databases remain untouched.'))
        self.choose.setCursor(Qt.PointingHandCursor);self.choose.setStyleSheet(Theme.get_button_style(accent))
        self.choose.clicked.connect(self.select_journal)
        row.addWidget(self.journal,1);row.addWidget(self.choose);layout.addLayout(row)
        self.tree=QTreeWidget()
        color=Theme._c if color is None else color
        self.tree.setStyleSheet(f'QTreeView::item:selected {{ background-color:{color("accent_main")}; color:{color("bg_main")}; }}')
        self.tree.setHeaderLabels([text('recovery_review_copy','Retired copy'),text('recovery_review_observed','Observed state'),
            text('recovery_review_native','Native identity'),text('recovery_review_bytes','Recovery bytes')])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0,QHeaderView.Stretch)
        for index in range(1,4):self.tree.header().setSectionResizeMode(index,QHeaderView.ResizeToContents)
        self.tree.setToolTip(text('recovery_review_tree_tip','Select a copy to inspect its original and recovery paths, native ID, current occupant and documentation. Equal SHA with different file identity remains pending review.'))
        self.tree.currentItemChanged.connect(self._selected)
        layout.addWidget(self.tree,2)
        self.restore=QPushButton(text('restore_selected','Restore selected copy'))
        self.restore.setToolTip(text('restore_selected_tip','Return only the reviewed copy to its original empty path, or reactivate its intact source. Preserves the same ID, curation and Cherry receipts. Requires confirmation and a fresh worker check; never overwrites.'))
        self.restore.setCursor(Qt.PointingHandCursor)
        self.restore.setStyleSheet(Theme.get_button_style(color('accent_success')))
        self.restore.setEnabled(False)
        self.restore.clicked.connect(self.restore_requested)
        layout.addWidget(self.restore)
        actions=QHBoxLayout()
        self.prepare_purge=QPushButton(text('purge_preview','Prepare deletion preview'))
        self.prepare_purge.setToolTip(text('purge_preview_tip','Recheck the selected recovery copy and the retained copy, including current bytes, curation and scope. Displays the exact file before confirmation; deletes nothing.'))
        self.prepare_purge.setStyleSheet(Theme.get_button_style(color('accent_warning')))
        self.prepare_purge.setCursor(Qt.PointingHandCursor);self.prepare_purge.setEnabled(False)
        self.prepare_purge.clicked.connect(self.purge_preview_requested)
        self.purge=QPushButton(text('purge_selected','Permanently delete verified copy'))
        self.purge.setToolTip(text('purge_selected_tip','Delete only the file identified by a current preview, after confirmation. Bypasses the Recycle Bin; keeps offline IDs, curation, journals and Cherry download receipts.'))
        self.purge.setStyleSheet(Theme.get_button_style(color('accent_danger')))
        self.purge.setCursor(Qt.PointingHandCursor);self.purge.setEnabled(False)
        self.purge.clicked.connect(self.purge_requested)
        actions.addWidget(self.prepare_purge);actions.addWidget(self.purge);layout.addLayout(actions)
        self.details=QPlainTextEdit();self.details.setReadOnly(True)
        self.details.setToolTip(text('recovery_review_details_tip','Evidence reflects this inspection only. Missing paths are distinguished from unavailable parents; any future restore or purge requires a fresh check and an explicit action.'))
        layout.addWidget(self.details,1)

    def path(self):return self.journal.text().strip()

    def select_journal(self):
        name,_=QFileDialog.getOpenFileName(self,self.text('recovery_review_choose','Choose journal'),'',
            self.text('recovery_review_filter','Recovery journal (journal.json)'))
        if name:self.journal.setText(name)

    def set_idle(self,idle):
        self.journal.setEnabled(idle);self.choose.setEnabled(idle)
        self.tree.setEnabled(idle)

    def invalidate_purge(self):
        self.purge_preview=None;self.purge.setEnabled(False)

    def invalidate(self):
        self.result=None;self.tree.clear();self.details.clear()
        self.restore.setEnabled(False)
        self.invalidate_purge();self.prepare_purge.setEnabled(False)

    def selected_restore(self):
        item=self.tree.currentItem()
        if self.result is None or not self.result['complete'] or item is None:return None
        move=item.data(0,Qt.UserRole)
        if move['observed'] not in ('retired_verified','archived_source_present'):return None
        copy=next(copy for copy in self.result['copies'] if copy['copy_id']==move['copy_id'])
        return dict(journal=Path(self.path()).as_posix(),copy_id=copy['copy_id'],journal_fingerprint=self.result['journal_fingerprint'],
            expected_fingerprint=copy['fingerprint'],source=move['source'],destination=move['destination'],observed=move['observed'])

    def selected_purge_request(self):
        request=self.selected_restore()
        return request if request is not None and request['observed']=='retired_verified' else None

    def selected_purge(self):
        request=self.selected_purge_request()
        value=self.purge_preview
        if request is None or value is None or not value['complete']:return None
        evidence=value['evidence']
        if any(request[key]!=evidence[key] for key in ('journal','copy_id','journal_fingerprint','source','destination')):return None
        if request['expected_fingerprint']!=evidence['approved'][0]['fingerprint']:return None
        return dict(request,preview_fingerprint=value['preview_fingerprint'])

    def show_purge_preview(self,value,reason):
        self.invalidate_purge()
        if not value['complete']:
            self.details.setPlainText(reason(value['error']) if value['error'] else self.text('purge_preview_cancelled','Deletion preview cancelled. No file was deleted.'))
            return
        self.purge_preview=value
        evidence=value['evidence']
        self.details.setPlainText(self.text('purge_preview_details','Permanent deletion preview\nCopy ID: {id}\nFile to delete: {target}\nOriginal path (absent): {source}\nVerified retained copy ID: {keeper_id}\nRetained file: {keeper}\nFile size: {size} bytes\n\nOffline IDs, curation, journals and Cherry receipts remain. This bypasses the Recycle Bin. No file has been deleted; confirmation is still required.').format(
            id=evidence['copy_id'],target=evidence['destination'],source=evidence['source'],
            keeper_id=evidence['keeper_id'],keeper=evidence['keeper'],size=evidence['size']))

    def show_purge(self,value,reason):
        self.invalidate()
        self.details.setPlainText(self.text('purge_result_details','Permanent deletion confirmed: {deleted}\nDeletion marked: {marked}\nNative approval confirmed: {approved}\nFile: {target}\nRetained file: {keeper}\nOperation journal: {journal}\n\n{reason}\nOffline identities, curation and Cherry receipts are retained. Request a fresh journal review before another action.').format(
            deleted=self.text('recovery_yes','Yes') if value['deleted'] else self.text('recovery_no','No'),
            marked=self.text('recovery_yes','Yes') if value['delete_marked'] else self.text('recovery_no','No'),
            approved=self.text('recovery_yes','Yes') if value['approved'] else self.text('recovery_no','No'),
            target=value['destination'] or '—',keeper=value['keeper'] or '—',journal=value['operation_journal'] or '—',
            reason=reason(value['error']) if value['error'] else ''))

    def show_restore(self,value,reason):
        self.invalidate()
        self.details.setPlainText(self.text('restore_details','Native restoration confirmed: {confirmed}\nFile returned: {moved}\nOriginal: {source}\nRecovery: {destination}\nOperation journal: {journal}\n\n{reason}\nRequest a fresh journal review before another action.').format(
            confirmed=self.text('recovery_yes','Yes') if value['committed'] else self.text('recovery_no','No'),
            moved=self.text('recovery_yes','Yes') if value['moved'] else self.text('recovery_no','No'),
            source=value['source'] or '—',destination=value['destination'] or '—',journal=value['operation_journal'] or '—',
            reason=reason(value['error']) if value['error'] else ''))

    def label(self,kind,value):return self.text('recovery_'+kind+'_'+value,value)

    def physical(self,value):
        result=self.label('physical',value['status'])
        if value.get('same_identity') is False:result+=' · '+self.text('recovery_identity_changed','Different file identity')
        return result

    def show_result(self,value,reason):
        self.invalidate();self.result=value
        if not value['complete']:
            self.details.setPlainText(reason(value['error']) if value['error'] else self.text('recovery_review_cancelled','Journal review cancelled. No changes were made.'))
            return
        self.details.setPlainText(self.text('recovery_review_select','Select a copy to inspect the observed evidence.'))
        copies={copy['source']:copy for copy in value['copies']}
        for move in value['moves']:
            copy=copies[move['source']]
            item=QTreeWidgetItem([Path(move['source']).name,self.label('observed',move['observed']),
                self.label('native',copy['native']),self.physical(move['destination_state'])])
            item.setData(0,Qt.UserRole,move)
            item.setToolTip(0,move['source']);item.setToolTip(3,move['destination'])
            self.tree.addTopLevelItem(item)
        if self.tree.topLevelItemCount():self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _selected(self,item,previous):
        self.invalidate_purge()
        self.selection_changed.emit()
        if item is None or self.result is None:return
        move=item.data(0,Qt.UserRole)
        copy=next(copy for copy in self.result['copies'] if copy['source']==move['source'])
        lines=[self.text('recovery_review_evidence','ID: {id}\nOriginal: {source}\nRecovery: {destination}\nObserved: {observed}\nNative: {native}\nOriginal bytes: {source_bytes}\nRecovery bytes: {destination_bytes}\nActive occupant ID: {occupant}\nExternal documentation: {documented}\nRecorded phase / move: {phase} / {recorded}').format(
            id=copy['copy_id'],source=move['source'],destination=move['destination'],
            observed=self.label('observed',move['observed']),native=self.label('native',copy['native']),
            source_bytes=self.physical(copy['physical']),destination_bytes=self.physical(move['destination_state']),
            occupant=copy['active_copy_id'] if copy['active_copy_id'] is not None else '—',
            documented=self.text('recovery_yes','Yes') if copy['documented'] else self.text('recovery_no','No'),
            phase=self.label('phase',self.result['phase']),recorded=self.label('recorded',move['recorded']))]
        sources={move['source'] for move in self.result['moves']}
        lines.append(self.text('recovery_review_keepers','Copies retained in the collection:'))
        for keeper in self.result['copies']:
            if keeper['source'] not in sources:
                lines.append(f"{keeper['source']}\n{self.label('native',keeper['native'])} · {self.physical(keeper['physical'])}"+
                    (' · '+self.text('recovery_documented','External documentation present') if keeper['documented'] else ''))
        self.details.setPlainText('\n\n'.join(lines))
