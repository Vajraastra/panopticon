"""Controles de fase pHash explícita; sin acceso a datos ni imágenes."""
from PySide6.QtCore import Qt,Signal
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,
    QPlainTextEdit,QPushButton,QSpinBox,QFileDialog)
from core.theme import Theme


class PerceptualPanel(QWidget):
    changed=Signal()

    def __init__(self,text,accent):
        super().__init__()
        self.text=text
        self.result=None
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        note=QLabel(text('phash_note','Explicit phase for 1–32 separate indexed folders, one per line. Checks current SHA and saves derived pHash in the native database; preserves curation and Cherry receipts. Similarity never authorizes retirement.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.folders=QPlainTextEdit()
        self.folders.setMaximumHeight(105)
        self.folders.setPlaceholderText(text('phash_folders_placeholder','Absolute indexed folder paths, one per line'))
        self.folders.setToolTip(text('phash_folders_tip','Choose only the indexed folders to process. Unknown SHA values require the separate SHA phase; mismatched bytes are reported without storing ambiguous pHash.'))
        self.folders.textChanged.connect(self.changed)
        layout.addWidget(self.folders)
        actions=QHBoxLayout()
        self.add=QPushButton(text('phash_add','Add folder'))
        self.add.setToolTip(text('phash_add_tip','Append a folder to the explicit scope. Calculation starts only when you press Calculate and save pHash.'))
        self.add.clicked.connect(self.choose_folder)
        self.clear=QPushButton(text('phash_clear','Clear folders'))
        self.clear.setToolTip(text('phash_clear_tip','Clear the selected scope and displayed result; previously committed derived hashes remain in the database.'))
        self.clear.clicked.connect(self.folders.clear)
        for button in (self.add,self.clear):
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(Theme.get_button_style(accent))
            actions.addWidget(button)
        layout.addLayout(actions)
        form=QFormLayout()
        self.pixels=QSpinBox()
        self.pixels.setRange(1,10_000_000)
        self.pixels.setValue(10_000_000)
        self.pixels.setToolTip(text('phash_pixels_tip','Maximum decoded pixels per image, up to 10 million. Larger images are preserved and reported. Verified cache reuse skips decoding but still reads current SHA.'))
        self.images=QSpinBox()
        self.images.setRange(1,100_000)
        self.images.setValue(100_000)
        self.images.setToolTip(text('phash_images_tip','Maximum images with known SHA in this run, up to 100,000. An initial excess rejects the whole scope before preparation; later failures retain only confirmed batches.'))
        for control in (self.pixels,self.images):
            control.setStyleSheet(Theme.get_input_style())
            control.lineEdit().setToolTip(control.toolTip())
            control.valueChanged.connect(self.changed)
        form.addRow(text('phash_pixels','Maximum decoded pixels:'),self.pixels)
        form.addRow(text('phash_images','Maximum images in this run:'),self.images)
        layout.addLayout(form)
        self.details=QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setToolTip(text('phash_details_tip','Confirmed stored and reused counts, unknown SHA values and at most 20 sampled issues. Cancelling keeps accepted commits; counts never claim unconfirmed writes.'))
        layout.addWidget(self.details,1)

    def paths(self):
        return [line.strip() for line in self.folders.toPlainText().splitlines() if line.strip()]

    def choose_folder(self):
        folder=QFileDialog.getExistingDirectory(self,self.text('phash_add','Add folder'))
        if folder:self.folders.appendPlainText(folder)

    def set_idle(self,idle):
        for control in (self.folders,self.add,self.clear,self.pixels,self.images):control.setEnabled(idle)

    def invalidate(self):
        self.result=None
        self.details.clear()

    def show_result(self,value,reason):
        self.result=value
        summary=self.text('phash_details_summary','Images: {total} · Known SHA: {known} · Unknown SHA: {unknown}\nProcessed: {processed} · Stored (confirmed): {stored} · Reused after SHA: {reused}\nIssues: {issues}\nAlgorithm: {algorithm}').format(
            total=value['total_images'],known=value['known_images'],unknown=value['unknown_images'],
            processed=value['processed'],stored=value['persisted'],reused=value['reused'],issues=value['issue_count'],algorithm=value['algorithm'] or '—')
        lines=[summary]
        if value['error']:lines.append(reason(value['error']))
        lines.extend(f"{item['path'] or '—'}: {reason(item['reason'])} ({item['count']})" for item in value['issues'])
        self.details.setPlainText('\n\n'.join(lines))
