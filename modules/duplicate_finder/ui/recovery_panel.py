"""Diario seleccionado y evidencia observada; sin SQL, stat ni acciones destructivas."""
from pathlib import Path
from PySide6.QtCore import Qt,Signal
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,
    QPushButton,QFileDialog,QTreeWidget,QTreeWidgetItem,QPlainTextEdit,QHeaderView)
from core.theme import Theme


class RecoveryPanel(QWidget):
    changed=Signal()

    def __init__(self,text,accent,color=None):
        super().__init__()
        self.text=text
        self.result=None
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0)
        note=QLabel(text('recovery_review_note','Select one retirement journal explicitly. Compares native IDs, curation and current bytes at both locations. Recorded phase is not proof of completion. This review changes nothing and does not restore or purge.'))
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

    def invalidate(self):
        self.result=None;self.tree.clear();self.details.clear()

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
