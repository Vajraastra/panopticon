"""Presentación de alcances/pares; sin SQL, observaciones físicas ni fusiones."""
from pathlib import PureWindowsPath

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPlainTextEdit,
    QPushButton,QSpinBox,QFileDialog,QTreeWidget,QTreeWidgetItem,QHeaderView)
from core.theme import Theme


class OverlapPanel(QWidget):
    changed = Signal()

    def __init__(self,text,accent,color):
        super().__init__()
        self.text,self.result=text,None
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.note=QLabel(text('overlap_note','Compare 2–32 separate indexed folders, one per line. Stored hashes classify pairs for review only; they do not authorize merges or retirement.'))
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.folders=QPlainTextEdit()
        self.folders.setMaximumHeight(105)
        self.folders.setPlaceholderText(text('overlap_folders_placeholder','Absolute folder paths, one per line'))
        self.folders.setToolTip(text('overlap_folders_tip','Paste absolute indexed folder paths or add them with the picker. Nested or repeated scopes are rejected. Counts exclude offline copies and show unknown hashes separately.'))
        self.folders.textChanged.connect(self.changed)
        layout.addWidget(self.folders)
        actions=QHBoxLayout()
        self.add=QPushButton(text('overlap_add','Add folder'))
        self.add.setToolTip(text('overlap_add_tip','Append an existing folder to the comparison scope. Only native indexed records are counted; no indexing or hashing starts.'))
        self.add.clicked.connect(self.choose_folder)
        self.clear=QPushButton(text('overlap_clear','Clear folders'))
        self.clear.setToolTip(text('overlap_clear_tip','Clear the comparison scope and its report without changing the library or files.'))
        self.clear.clicked.connect(self.folders.clear)
        for button in (self.add,self.clear):
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(Theme.get_button_style(accent))
            actions.addWidget(button)
        actions.addWidget(QLabel(text('overlap_threshold','High overlap ≥ Jaccard (%):')))
        self.threshold=QSpinBox()
        self.threshold.setRange(0,100)
        self.threshold.setValue(51)
        self.threshold.setSuffix('%')
        self.threshold.setStyleSheet(Theme.get_input_style())
        self.threshold.setToolTip(text('overlap_threshold_tip','Classify a nonempty shared pair as high overlap when shared unique hashes / union reaches this percentage. Directional coverage is shown separately; no automatic merge follows.'))
        self.threshold.lineEdit().setToolTip(self.threshold.toolTip())
        self.threshold.valueChanged.connect(self.changed)
        actions.addWidget(self.threshold)
        layout.addLayout(actions)
        self.tree=QTreeWidget()
        self.tree.setStyleSheet(f'QTreeView::item:selected {{ background-color:{color("accent_main")}; color:{color("bg_main")}; }}')
        self.tree.setHeaderLabels([text(key,label) for key,label in (
            ('overlap_first','First folder'),('overlap_second','Second folder'),('overlap_shared','Shared hashes'),
            ('overlap_jaccard','Jaccard'),('overlap_classification','Classification'))])
        for index in (0,1):self.tree.header().setSectionResizeMode(index,QHeaderView.Stretch)
        for index in (2,3,4):self.tree.header().setSectionResizeMode(index,QHeaderView.ResizeToContents)
        self.tree.setToolTip(text('overlap_tree_tip','Select a pair to inspect both folders’ hash coverage and directional containment. The report uses stored identities, not current physical bytes or curation.'))
        self.tree.currentItemChanged.connect(self.details_for_pair)
        layout.addWidget(self.tree,2)
        self.details=QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setToolTip(text('overlap_details_tip','Complete captured coverage: active copies, known and unknown hashes, unique hashes, shared/union counts and directional ratios. It is a read-only report.'))
        layout.addWidget(self.details,1)

    def paths(self):
        return [line.strip() for line in self.folders.toPlainText().splitlines() if line.strip()]

    def choose_folder(self):
        folder=QFileDialog.getExistingDirectory(self,self.text('overlap_add','Add folder'))
        if folder:
            self.folders.appendPlainText(folder)

    def set_idle(self,idle):
        for control in (self.folders,self.add,self.clear,self.threshold):control.setEnabled(idle)

    def invalidate(self):
        self.result=None
        self.tree.clear()
        self.details.clear()

    def show_result(self,value):
        self.invalidate()
        self.result=value
        for pair in value['pairs']:
            first,second=value['folders'][pair['first']],value['folders'][pair['second']]
            ratio=self.ratio(pair['jaccard'])
            label=self.text('overlap_class_'+pair['classification'],pair['classification'])
            labels=[f"{index+1} · {PureWindowsPath(folder['folder']).name or folder['folder']}"
                for index,folder in ((pair['first'],first),(pair['second'],second))]
            item=QTreeWidgetItem([*labels,str(pair['shared_hashes']),ratio,label])
            item.setData(0,Qt.UserRole,pair)
            for column,folder in enumerate((first,second)):item.setToolTip(column,folder['folder'])
            self.tree.addTopLevelItem(item)
        self.details.setPlainText('\n\n'.join(self.folder_details(folder,f'{index+1}')
            for index,folder in enumerate(value['folders'])))

    @staticmethod
    def ratio(value):
        return '—' if value is None else f'{value*100:.2f}%'

    def folder_details(self,folder,label):
        return self.text('overlap_folder_details','Folder {label}: {folder}\nActive copies: {active} · Hashed: {known} · Unknown: {unknown} · Unique hashes: {unique}').format(
            label=label,folder=folder['folder'],active=folder['active_copies'],known=folder['hashed_copies'],
            unknown=folder['unknown_copies'],unique=folder['unique_hashes'])

    def details_for_pair(self,current,previous=None):
        if not current or not self.result:return
        pair=current.data(0,Qt.UserRole)
        folders=[self.folder_details(self.result['folders'][index],str(index+1))
            for index in (pair['first'],pair['second'])]
        summary=self.text('overlap_pair_details','Shared hashes: {shared} · Union: {union} · Jaccard: {jaccard}\nCoverage of first folder: {first} · Coverage of second folder: {second}\nClassification: {classification} · Captured threshold: {threshold}%').format(
            shared=pair['shared_hashes'],union=pair['union_hashes'],jaccard=self.ratio(pair['jaccard']),
            first=self.ratio(pair['first_coverage']),second=self.ratio(pair['second_coverage']),
            classification=self.text('overlap_class_'+pair['classification'],pair['classification']),threshold=self.result['threshold'])
        self.details.setPlainText('\n\n'.join([*folders,summary]))
