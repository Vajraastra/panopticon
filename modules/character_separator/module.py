from core.base_module import BaseModule


class CharacterSeparatorModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Character Separator"
        self._description = "Review character groups and copy them into named folders."
        self._icon = "👥"
        self.accent_color = "#3478b9"
        self.view = None

    def get_view(self):
        if self.view is None:
            from .ui.view import CharacterSeparatorView

            self.view = CharacterSeparatorView(self.tr, self.context)
        return self.view

    def run_headless(self, params, input_data):
        from pathlib import Path
        from .logic.recognition import scan

        return scan(Path(input_data), lambda: False, lambda *_: None)

    def on_unload(self):
        if self.view is not None:
            self.view.shutdown()

    def shutdown_ready(self):
        return self.view is None or self.view.shutdown_ready()
