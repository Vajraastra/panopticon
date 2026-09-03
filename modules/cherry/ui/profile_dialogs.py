"""
Diálogos de alta: proyecto nuevo y fuente nueva.

Solo recogen datos y validan; quien escribe en el índice es la vista, para que
el error salga por su barra de estado y no por un modal más. La única parte que
toca la red es "obtener el nombre", y es opcional a propósito: si el sitio pide
sesión o no responde, el nombre se escribe a mano y el perfil se crea igual.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from ..logic import profile_writes as writes


class _BaseDialog(QDialog):
    def __init__(self, theme=None, locale=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._lm = locale

    def tr(self, key: str, default: str) -> str:
        return self._lm.tr(key, default) if self._lm else default

    def _color(self, key: str, fallback: str) -> str:
        return self._theme.get_color(key) if self._theme else fallback

    def _hint(self, text: str = "") -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        return lbl


class NewProfileDialog(_BaseDialog):
    """Alta de un proyecto: URL principal + nombre + carpeta destino."""

    _name_resolved = Signal(str)
    _resolve_failed = Signal(str)

    def __init__(self, runtime, theme=None, locale=None, parent: QWidget | None = None) -> None:
        super().__init__(theme, locale, parent)
        self._runtime = runtime
        self.setWindowTitle(self.tr("cherry.new_title", "New project"))
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._url = QLineEdit()
        self._url.setPlaceholderText("https://www.patreon.com/c/…")
        self._url.textChanged.connect(self._on_url_changed)
        form.addRow(self.tr("cherry.new_url", "URL"), self._url)

        self._site = self._hint("—")
        form.addRow("", self._site)

        self._name = QLineEdit()
        self._name.setPlaceholderText(self.tr("cherry.new_name_ph", "Artist name"))
        self._name.textChanged.connect(self._update_state)
        form.addRow(self.tr("cherry.new_name", "Name"), self._name)

        self._btn_resolve = QPushButton(self.tr("cherry.new_resolve", "Fetch name from the site"))
        self._btn_resolve.clicked.connect(self._resolve)
        form.addRow("", self._btn_resolve)

        self._folder = self._hint("—")
        form.addRow(self.tr("cherry.new_folder", "Folder"), self._folder)

        lay.addLayout(form)

        self._status = self._hint("")
        lay.addWidget(self._status)

        self._download_now = QCheckBox(self.tr("cherry.new_download_now", "Download right away"))
        self._download_now.setChecked(True)
        lay.addWidget(self._download_now)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText(self.tr("cherry.new_create", "Create"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._name_resolved.connect(self._on_name_resolved)
        self._resolve_failed.connect(self._on_resolve_failed)
        self._update_state()

    # ── datos ─────────────────────────────────────────────────────────────
    def url(self) -> str:
        return self._url.text().strip()

    def display_name(self) -> str:
        return self._name.text().strip()

    def download_now(self) -> bool:
        return self._download_now.isChecked()

    # ── estado ────────────────────────────────────────────────────────────
    def _on_url_changed(self) -> None:
        url = self.url()
        site = writes.site_of(url) if url else ""
        if not url:
            self._site.setText("—")
        elif site == "unknown":
            self._site.setText(self.tr(
                "cherry.new_site_unknown", "⚠ No template recognises this URL"))
        else:
            self._site.setText(self.tr("cherry.new_site", "Site: {s}").format(s=site))
        self._update_state()

    def _update_state(self) -> None:
        name = self.display_name()
        self._folder.setText(str(writes.folder_preview(name)) if name else "—")
        self._btn_resolve.setEnabled(bool(self.url()))
        self._ok.setEnabled(bool(self.url()) and bool(name))

    # ── resolución del nombre (red) ───────────────────────────────────────
    def _resolve(self) -> None:
        if not self._runtime.start():
            self._status.setText(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return
        self._btn_resolve.setEnabled(False)
        self._status.setText(self.tr("cherry.new_resolving", "Asking the site…"))
        try:
            self._runtime.submit(self._resolve_async(self.url()))
        except RuntimeError as exc:
            self._resolve_failed.emit(str(exc))

    async def _resolve_async(self, url: str) -> None:
        try:
            name = await writes.resolve_name(url)
        except Exception as exc:
            self._resolve_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._name_resolved.emit(name)

    def _on_name_resolved(self, name: str) -> None:
        self._name.setText(name)
        self._btn_resolve.setEnabled(True)
        self._status.setText(self.tr("cherry.new_resolved", "Name: {n}").format(n=name))
        self._update_state()

    def _on_resolve_failed(self, message: str) -> None:
        self._btn_resolve.setEnabled(True)
        self._status.setText(self.tr(
            "cherry.new_resolve_failed",
            "Could not fetch the name ({msg}) — type it by hand and create anyway.",
        ).format(msg=message))


class AddUrlDialog(_BaseDialog):
    """Fuente nueva para un perfil existente. No toca la red."""

    def __init__(self, profile_name: str, theme=None, locale=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(theme, locale, parent)
        self.setWindowTitle(self.tr("cherry.add_url_title", "Add source"))
        self.setMinimumWidth(480)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(self.tr(
            "cherry.add_url_body", "New source for “{name}”:").format(name=profile_name)))

        self._url = QLineEdit()
        self._url.setPlaceholderText("https://kemono.cr/…")
        self._url.textChanged.connect(self._on_changed)
        lay.addWidget(self._url)

        self._site = self._hint("—")
        lay.addWidget(self._site)

        lay.addWidget(self._hint(self.tr(
            "cherry.add_url_hint",
            "Everything lands in the same folder as the profile. Its first sync "
            "scans the whole history of the new source.",
        )))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def url(self) -> str:
        return self._url.text().strip()

    def _on_changed(self) -> None:
        url = self.url()
        site = writes.site_of(url) if url else ""
        if not url:
            self._site.setText("—")
        elif site == "unknown":
            self._site.setText(self.tr(
                "cherry.new_site_unknown", "⚠ No template recognises this URL"))
        else:
            self._site.setText(self.tr("cherry.new_site", "Site: {s}").format(s=site))
        # Una URL sin template se puede guardar, pero no la descargaría nadie.
        self._ok.setEnabled(bool(url) and site != "unknown")
