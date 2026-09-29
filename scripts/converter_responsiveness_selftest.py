"""Qt heartbeat during real metadata verification; disposable, warm-cache files."""
import os
import sys
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from modules.format_converter import module as m
from modules.format_converter.logic.converter import ConversionResult, convert_single


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    module = m.FormatConverterModule()
    module.on_load({})
    view = module.get_view()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / 'seed.png'
        info = PngInfo()
        info.add_text('parameters', 'test prompt\nSteps: 20, Sampler: Euler, CFG scale: 7, Seed: 42')
        Image.new('RGB', (32, 32), 'red').save(src, pnginfo=info)
        seed = convert_single(src, root / 'seed.webp')
        assert seed.success, seed.error
        original, converted = src.read_bytes(), Path(seed.output_path).read_bytes()
        report = m.BatchConversionReport(output_dir=tmp, total_files=2000, converted_count=2000)
        for i in range(2000):
            a, b = root / f'{i}.png', root / f'{i}.webp'
            a.write_bytes(original)
            b.write_bytes(converted)
            report.results.append(ConversionResult(str(a), str(b), True))
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
        timer.start()
        duration = []
        def finish(*args):
            duration.append(time.perf_counter())
            QTimer.singleShot(50, app.quit)
        baseline = '--baseline' in sys.argv
        module.files_to_convert = [src]
        def begin():
            duration.append(time.perf_counter())
            if baseline:
                module.worker_thread = QThread()
                module.on_conversion_finished(report)
            else:
                module.start_conversion()
        with patch.object(QMessageBox, 'information', side_effect=finish), \
             patch.object(QFileDialog, 'getExistingDirectory', return_value=tmp), \
             patch.object(m, 'convert_batch', return_value=report):
            QTimer.singleShot(50, begin)
            app.exec()
        timer.stop()
        elapsed = duration[1] - duration[0]
        gaps = [b-a for a, b in zip(ticks, ticks[1:])]
        active_ticks = sum(duration[0] < t < duration[1] for t in ticks)
        metrics = dict(baseline=baseline, pairs=2000, seconds=round(elapsed, 3),
                       max_heartbeat_gap_ms=round(max(gaps)*1000, 1), active_ticks=active_ticks)
        print(json.dumps(metrics))
        assert '2000/2000' in module.txt_log.toPlainText()
        if baseline:
            assert active_ticks == 0
        else:
            assert active_ticks >= 3
            assert max(gaps) < 0.5, metrics
            assert module.worker_thread.wait(1000)
        view.close()


if __name__ == '__main__':
    main()
