"""Non-modal explicit Native preparation/review; reader shutdown is deferred."""
import logging
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,
    QTableWidget,QTableWidgetItem,QAbstractItemView,QHeaderView,QComboBox)
from core.library_async import LibraryReader
from core.components.reader_dialog import ReaderDialog
from core.path_identity_tasks import PathIdentityTasks

log=logging.getLogger(__name__)


class PathIdentityReviewDialog(ReaderDialog):
    busy_changed=Signal(bool)
    review_copies_requested=Signal()
    _progress=Signal(int,object)

    def __init__(self,service,translate,theme=None,parent=None,can_prepare=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.tr_text=translate
        self.can_prepare=can_prepare or (lambda:True)
        self._closed=self._busy=self._preparing=False
        self._token=0
        self._after=self._upper=None
        self._more=False
        self._followup=0
        self._control_poll=False
        self.tasks=PathIdentityTasks(service,self._progress.emit)
        self.reader=LibraryReader(self.tasks,self)
        self.reader.result.connect(self._received)
        self._progress.connect(self._progressed,Qt.QueuedConnection)
        self.setWindowTitle(self.text('title','Review path identities'))
        self.resize(1000,620)
        layout=QVBoxLayout(self)
        note=QLabel(self.text('note','Prepare path identities according to Windows naming rules. '
            'Keeps names, files, copy IDs, curation and offline history. '
            'Conflicting copies remain separate for review. No files are merged or deleted.'))
        note.setWordWrap(True)
        note.setTextFormat(Qt.PlainText)
        layout.addWidget(note)
        self.summary=QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        layout.addWidget(self.summary)
        self.scope=QComboBox()
        self.scope.addItem(self.text('issues','Pending review'),'issues')
        self.scope.addItem(self.text('all','All retained copies'),'all')
        self.scope.setToolTip(self.text('scope_tip','Review unresolved paths and conflicts; all copies also includes offline history.'))
        self.scope.currentIndexChanged.connect(self.restart)
        layout.addWidget(self.scope)
        self.table=QTableWidget(0,5)
        self.table.setHorizontalHeaderLabels([self.text(k,d) for k,d in
            (('id','Copy ID'),('path','Stored path'),('state','State'),('rating','Rating'),('canonical','Last confirmed name'))])
        self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4,QHeaderView.Stretch)
        for column in (0,2,3):
            self.table.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setToolTip(self.text('table_tip','Up to100 copies per page. IDs and ratings stay independent even when Windows confirms the same name. Evidence comes from the last preparation; prepare again after changing files or disks.'))
        layout.addWidget(self.table,1)
        lookup=QHBoxLayout()
        self.path=QLineEdit()
        self.path.setMaxLength(4096)
        self.path.setPlaceholderText(self.text('path_placeholder','Existing local image path'))
        self.path.setToolTip(self.text('path_tip','Check a spelling variant in a worker; no file is registered or changed.'))
        self.path.textChanged.connect(lambda:self._controls())
        self.resolve_button=QPushButton(self.text('resolve','Check path'))
        self.resolve_button.setToolTip(self.text('resolve_tip','Ask Windows to resolve this name, then review matching Native IDs. Incomplete evidence never selects a winner.'))
        self.resolve_button.clicked.connect(self.resolve)
        lookup.addWidget(self.path,1)
        lookup.addWidget(self.resolve_button)
        layout.addLayout(lookup)
        self.status=QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        controls=QHBoxLayout()
        specs=(('refresh_button','refresh','Refresh','Read current coverage and start the first review page.',self.refresh),
            ('next_button','next','Next page','Read the next complete page without keeping old pages in memory.',self.next_page),
            ('prepare_button','prepare','Prepare identities','Back up the Native database, then verify names in bounded batches. Preserves files and every copy history.',self.prepare),
            ('stop_button','stop','Stop preparation','Cancel before the next admission. A running batch settles; confirmed records remain.',self.stop),
            ('copies_button','copies','Review copy histories','Open the retained-copy catalog for explicit review by ID. Conflicts are not merged here.',lambda:self.review_copies_requested.emit()),
            ('close_button','close','Close','Cancel outstanding preparation and wait for readers to finish before closing.',self.close))
        for attr,key,default,tip,callback in specs:
            button=QPushButton(self.text(key,default))
            button.setToolTip(self.text(key+'_tip',tip))
            button.clicked.connect(callback)
            setattr(self,attr,button)
            controls.addWidget(button)
        layout.addLayout(controls)
        if theme is not None:
            self.setStyleSheet(theme.get_stylesheet())
        self.refresh()

    def text(self,key,default):
        return self.tr_text('lib.identity.'+key,default)

    def _controls(self):
        if self._closed:
            return
        ready=self.reader.shutdown_ready()
        idle=not self._busy and ready
        for widget in (self.scope,self.path,self.refresh_button,self.prepare_button,self.copies_button):
            widget.setEnabled(idle)
        self.resolve_button.setEnabled(idle and bool(self.path.text().strip()))
        self.next_button.setEnabled(idle and self._more)
        self.stop_button.setEnabled(self._preparing and not self.tasks.cancel.is_set())
        if not self._busy and not ready and not self._control_poll:
            self._control_poll=True
            QTimer.singleShot(25,self,self._retry_controls)

    def _retry_controls(self):
        self._control_poll=False
        self._controls()

    def _later(self,callback):
        self._followup+=1
        token=self._followup
        def run():
            if self._closed or token!=self._followup:
                return
            if self._busy or not self.reader.shutdown_ready():
                QTimer.singleShot(25,self,run)
                return
            callback()
        QTimer.singleShot(25,self,run)

    def _request(self,operation,*args,**kwargs):
        if self._closed or self._busy or not self.reader.shutdown_ready():
            return False
        self._busy=True
        self._followup+=1
        self._operation=operation
        self.reader.request(operation,*args,**kwargs)
        self._controls()
        return True

    def refresh(self):
        self._request('get_path_identity_status')

    def restart(self):
        if self._closed or self._busy:
            return
        self._after=self._upper=None
        self.next_page()

    def next_page(self):
        self._request('get_path_identity_review_page',after_id=self._after,upper_id=self._upper,
                      scope=self.scope.currentData(),limit=100)

    def prepare(self):
        if self._closed or self._busy or not self.reader.shutdown_ready():
            return
        if not self.can_prepare():
            self.status.setText(self.text('wait','Wait for indexing, hashing or folder changes to finish.'))
            return
        self._token+=1
        self.tasks.begin(self._token)
        self._preparing=True
        self.busy_changed.emit(True)
        self.status.setText(self.text('preparing','Preparing identities…'))
        if not self._request('prepare',token=self._token):
            self._preparing=False
            self.busy_changed.emit(False)

    def stop(self):
        if self._preparing:
            self.tasks.stop()
            self.status.setText(self.text('stopping','Stopping after the current work settles…'))
            self._controls()

    def resolve(self):
        path=self.path.text()
        if path.strip():
            self._request('resolve',path)

    @Slot(int,object)
    def _progressed(self,token,row):
        if self._closed or not self._preparing or token!=self._token or self.tasks.cancel.is_set():
            return
        self.status.setText(self.text('progress','{count} records confirmed.').format(count=row['confirmed']))

    def _summary(self,value):
        self.summary.setText(self.text('summary','{total} retained copies · {pending} not prepared · '
            '{unresolved} unavailable or unresolved · {conflict_groups} conflicting names').format(**value))

    def _rows(self,rows):
        self.table.setRowCount(len(rows))
        for index,row in enumerate(rows):
            file_id,path,rating,offline,status,canonical=row
            values=(str(file_id),path,self.text('state.'+status,status),str(rating if rating is not None else 0),canonical or '')
            for column,value in enumerate(values):
                item=QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(index,column,item)

    @Slot(int,object,object)
    def _received(self,ticket,value,error):
        if self._closed:
            return
        operation=self._operation
        self._busy=False
        if operation=='prepare':
            self._preparing=False
        if error is not None:
            log.warning('Path identity %s failed: %s',operation,error)
            self.status.setText(self.text('failed','Could not complete the review. '
                'Confirmed records remain preserved. Refresh and review changed or unavailable paths.'))
        elif operation=='get_path_identity_status':
            self._summary(value)
            self._after=self._upper=None
            # Native thread may still be finishing signal delivery.
            self._later(self.next_page)
        elif operation=='get_path_identity_review_page':
            self._rows(value['records'])
            self._after,self._upper,self._more=value['after_id'],value['upper_id'],value['more']
        elif operation=='prepare':
            if value['coverage'] is not None:
                self._summary(value['coverage'])
            key='cancelled' if value['cancelled'] else 'prepared'
            default='Stopped; {count} records confirmed.' if value['cancelled'] else 'Prepared {count} records. Conflicts remain for review.'
            self.status.setText(self.text(key,default).format(count=value['confirmed']))
            self._after=self._upper=None
        elif operation=='resolve':
            self.status.setText(self.text('lookup.'+value['status'],'This path needs review.'))
            self._rows([(r[0],r[1],r[2],0,'conflict' if len(value['records'])>1 else 'resolved',value['canonical']) for r in value['records']])
            self._more=False
        if operation=='prepare':
            def settled():
                self.busy_changed.emit(False)
                if error is None:
                    self.next_page()
            self._later(settled)
        self._controls()

    def _shutdown(self,*_):
        self.tasks.stop()
        super()._shutdown()
