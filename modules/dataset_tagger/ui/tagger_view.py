import logging
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt
from core.components.standard_layout import StandardToolLayout

log = logging.getLogger(__name__)


class DatasetTaggerView(QWidget):
    """
    Vista principal del Dataset Tagger.

    Fase 0: esqueleto registrable con placeholder. Las fases siguientes añaden
    selección de modelo/formato, providers, worker y la UI de revisión.
    El acento se toma del theme_manager para sincronizar con la paleta global.
    """

    def __init__(self, context):
        super().__init__()
        self.context = context
        self._pending_paths = []

        content = self._create_content()
        sidebar = self._create_sidebar()

        self.layout_manager = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager') if self.context else None,
            event_bus=self.context.get('event_bus') if self.context else None,
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.layout_manager)

    def _tr(self, key, default=None):
        """Traduce usando el LocaleManager del contexto."""
        lm = self.context.get('locale_manager') if self.context else None
        if lm:
            return lm.tr(key, default)
        return default if default is not None else key

    def _accent(self):
        """Acento sincronizado con la paleta global de Panopticon."""
        tm = self.context.get('theme_manager') if self.context else None
        return tm.get_color('accent_main') if tm else "#bd93f9"

    def _create_content(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 24, 24, 24)

        title = QLabel(self._tr("tagger.title", "Dataset Tagger"))
        title.setStyleSheet(f"color: {self._accent()}; font-size: 22px; font-weight: bold;")

        subtitle = QLabel(self._tr(
            "tagger.subtitle",
            "Caption image folders with a vision LLM (tags + natural language)."
        ))
        subtitle.setWordWrap(True)

        lay.addWidget(title)
        lay.addWidget(subtitle)
        lay.addStretch()
        return w

    def _create_sidebar(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lbl = QLabel(self._tr("tagger.sidebar", "Options"))
        lbl.setStyleSheet("font-weight: bold;")
        lay.addWidget(lbl)
        lay.addStretch()
        return w

    def load_images(self, paths):
        """Recibe un set de imágenes desde otros módulos (se conecta en Fase 6)."""
        self._pending_paths = list(paths)
        log.info("Dataset Tagger recibió %d imágenes (pendiente: Fase 6)", len(self._pending_paths))
