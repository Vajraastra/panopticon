"""
Diálogos de alta: proyecto nuevo y fuente nueva.

Solo recogen datos y validan; quien escribe en el índice es la vista, para que
el error salga por su barra de estado y no por un modal más. La única parte que
toca la red es "obtener el nombre", y es opcional a propósito: si el sitio pide
sesión o no responde, el nombre se escribe a mano y el perfil se crea igual.

Ambos avisan de lo que ya se tiene (A4): si la URL ya está en un proyecto y si
el artista ya tiene archivos en la biblioteca. Son avisos, no vetos — un mismo
artista en dos proyectos puede ser justo lo que se quiere. Las dos consultas
son de solo lectura y viven en `logic/library_check.py`; aquí solo se pintan.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from ..logic import library_check
from ..logic import profile_writes as writes


class _BaseDialog(QDialog):
    # Puente loop → GUI de la comprobación de URL. Se declara aquí porque las
    # dos subclases la usan y una Signal tiene que ser atributo de clase.
    _url_hits = Signal(str, object)   # url comprobada, list[dict]

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

    def _warn(self, text: str = "") -> QLabel:
        """Como `_hint` pero en color de aviso: es información, no un error."""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {self._color('accent_info', '#5599ff')}; font-size: 11px;")
        return lbl

    # ── "esto ya lo tienes" (A4) ──────────────────────────────────────────
    def _init_url_check(self, runtime, label: QLabel) -> None:
        """Arma la comprobación de URL duplicada con debounce.

        Va con retardo porque se dispara al teclear y cada pasada recorre los
        46 perfiles del índice (~56 ms). Pegar una URL es un solo `textChanged`,
        pero escribirla a mano son cuarenta.
        """
        self._chk_runtime = runtime
        self._chk_label = label
        self._chk_timer = QTimer(self)
        self._chk_timer.setSingleShot(True)
        self._chk_timer.setInterval(400)
        self._chk_timer.timeout.connect(self._run_url_check)
        self._url_hits.connect(self._on_url_hits)

    def _queue_url_check(self) -> None:
        self._chk_label.setText("")
        self._chk_timer.start()

    def _run_url_check(self) -> None:
        url = self.url()
        if not url or self._chk_runtime is None or not self._chk_runtime.is_running:
            return
        try:
            self._chk_runtime.submit(self._url_check_async(url))
        except RuntimeError:
            pass  # el aviso es opcional: sin runtime, simplemente no aparece

    async def _url_check_async(self, url: str) -> None:
        hits = await library_check.find_url(url)
        self._url_hits.emit(url, hits)

    def _on_url_hits(self, url: str, hits: list) -> None:
        if url != self.url():
            return  # llegó tarde: el campo ya cambió
        if not hits:
            self._chk_label.setText("")
            return
        names = ", ".join(h["display_name"] for h in hits[:3])
        self._chk_label.setText(self.tr(
            "cherry.dup_url",
            "⚠ This URL is already a source of: {names}",
        ).format(names=names))


class NewProfileDialog(_BaseDialog):
    """Alta de un proyecto: URL principal + nombre + carpeta destino."""

    _name_resolved = Signal(str)
    _resolve_failed = Signal(str)
    _artist_known  = Signal(str, object)   # nombre comprobado, resumen

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

        self._dup = self._warn("")
        form.addRow("", self._dup)

        self._name = QLineEdit()
        self._name.setPlaceholderText(self.tr("cherry.new_name_ph", "Artist name"))
        self._name.textChanged.connect(self._update_state)
        form.addRow(self.tr("cherry.new_name", "Name"), self._name)

        self._btn_resolve = QPushButton(self.tr("cherry.new_resolve", "Fetch name from the site"))
        self._btn_resolve.clicked.connect(self._resolve)
        form.addRow("", self._btn_resolve)

        self._folder = self._hint("—")
        form.addRow(self.tr("cherry.new_folder", "Folder"), self._folder)

        self._known = self._warn("")
        form.addRow("", self._known)

        lay.addLayout(form)

        self._status = self._hint("")
        lay.addWidget(self._status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText(self.tr("cherry.new_create", "Create"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._name_resolved.connect(self._on_name_resolved)
        self._resolve_failed.connect(self._on_resolve_failed)
        self._artist_known.connect(self._on_artist_known)

        self._init_url_check(runtime, self._dup)
        # El aviso del artista tiene su propio debounce: recorre las 294K filas
        # de `panopticon.db` (~180 ms en un hilo) y el nombre se teclea letra
        # a letra cuando el sitio no lo resuelve.
        self._name_timer = QTimer(self)
        self._name_timer.setSingleShot(True)
        self._name_timer.setInterval(500)
        self._name_timer.timeout.connect(self._run_artist_check)

        self._update_state()

    # ── datos ─────────────────────────────────────────────────────────────
    def url(self) -> str:
        return self._url.text().strip()

    def display_name(self) -> str:
        return self._name.text().strip()

    # ── estado ────────────────────────────────────────────────────────────
    def _on_url_changed(self) -> None:
        url = self.url()
        self._queue_url_check()
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
        self._known.setText("")
        self._name_timer.start()

    # ── "ya tienes archivos de este artista" (A4) ─────────────────────────
    def _run_artist_check(self) -> None:
        name = self.display_name()
        if not name or not self._runtime.is_running:
            return
        try:
            self._runtime.submit(self._artist_check_async(name))
        except RuntimeError:
            pass  # el aviso es opcional

    async def _artist_check_async(self, name: str) -> None:
        summary = await library_check.find_artist(name)
        self._artist_known.emit(name, summary)

    def _on_artist_known(self, name: str, summary: dict) -> None:
        if name != self.display_name():
            return  # llegó tarde
        n = summary.get("files", 0)
        if not n:
            self._known.setText("")
            return
        folders = summary.get("folders") or []
        where = folders[0] if folders else "?"
        if len(folders) > 1:
            where = self.tr("cherry.known_where_many", "{f} and {n} more folders").format(
                f=where, n=len(folders) - 1)
        self._known.setText(self.tr(
            "cherry.known_artist",
            "ℹ The library already has {n} files by “{a}” in {w}.",
        ).format(n=n, a=name, w=where))

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
    """Fuente nueva para un perfil existente. No toca la red.

    Sí lee el índice, para avisar de que la URL ya es fuente de algún proyecto:
    `add_profile_url` inserta sin comprobar nada, así que pegarla dos veces
    duplica la entrada en silencio.
    """

    def __init__(self, profile_name: str, runtime=None, theme=None, locale=None,
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

        self._dup = self._warn("")
        lay.addWidget(self._dup)

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

        self._init_url_check(runtime, self._dup)

    def url(self) -> str:
        return self._url.text().strip()

    def _on_changed(self) -> None:
        url = self.url()
        self._queue_url_check()
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
