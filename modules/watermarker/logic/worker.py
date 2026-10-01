"""One bounded image job; only detached QImage/results cross to the GUI."""
import threading
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage
from .watermarker import render_preview, process_image, WatermarkCancelled


class WatermarkWorker(QThread):
    finished_signal = Signal(dict)

    def __init__(self, operation, options, *, generation=0, output=None, preview_size=(1600,1200), service=None):
        super().__init__()
        self.operation = operation
        self.options = dict(options)
        self.generation = generation
        self.output = output
        self.preview_size = preview_size
        self.service = service
        self.cancel_event = threading.Event()

    def stop(self):
        self.cancel_event.set()

    def run(self):
        result = dict(operation=self.operation, generation=self.generation, output=self.output,
                      success=False, cancelled=False, image=None, error=None)
        try:
            if self.operation == 'preview':
                with render_preview(**self.options, preview_size=self.preview_size,
                                    cancel_event=self.cancel_event) as image:
                    data = image.tobytes('raw','RGBA')
                    result['image'] = QImage(data,image.width,image.height,image.width*4,
                                             QImage.Format_RGBA8888).copy()
                    if result['image'].isNull():
                        raise ValueError('watermarker_preview_allocation_failed')
                if self.cancel_event.is_set():
                    raise WatermarkCancelled('watermarker_cancelled')
                result['success'] = True
            elif self.operation == 'save':
                success, message = process_image(**self.options, output_path=self.output,
                    cancel_event=self.cancel_event, native_service=self.service)
                result.update(success=success, cancelled=not success and message=='watermarker_cancelled',
                              error=None if success else message)
            else:
                raise ValueError('watermarker_invalid_operation')
        except WatermarkCancelled:
            result.update(cancelled=True, image=None)
        except Exception as error:
            result.update(error=str(error), image=None)
        finally:
            self.finished_signal.emit(result)
