"""Render the production association dialogs using isolated synthetic records only."""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ.update(QT_QPA_PLATFORM='offscreen', REPLENISHMENT_DATA_DIR=str(output/'settings'),
                      LOCALAPPDATA=str(output/'component-settings'))
    sys.path[:0] = [str(ROOT/'tests'), str(ROOT/'src')]
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont, QFontDatabase
    from ui_controls import STYLE
    from test_aoxiang_binding import BindingUiTests
    from aoxiang_directory import atomic_json, file_hash

    BindingUiTests.setUpClass()
    app = QApplication.instance()
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    font = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts/msyh.ttc'
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont('Microsoft YaHei', 10))
    fixture = BindingUiTests()
    fixture.setUp()
    captured = []
    try:
        dialog = fixture.d
        dialog.host.closing = False
        proof = {'evidence': {'rows': [{'sku':'0001', 'quantity':'7', 'amount':'0.99', 'status':'待提交'}],
                              'observedAt':'2026-11-02T04:00:00Z'}}
        file = Path(fixture.f.temp.name)/'synthetic-binding.json'
        atomic_json(file, proof)
        dialog.client.binding['proposal']['manifest'] = {'file':str(file), 'sha256':file_hash(file)}
        dialog.binding_status(dialog.client.binding)
        dialog.resize(1180, 900)
        app.processEvents()
        if not dialog.grab().save(str(output/'binding-source-synthetic.png')):
            raise RuntimeError('Preview screenshot failed')
        timer = QTimer()
        timer.setInterval(10)
        began = time.monotonic()

        def capture():
            modal = app.activeModalWidget()
            if modal and modal.windowTitle().startswith('逐商品关联核对'):
                app.processEvents()
                captured.append(modal.grab().save(str(output/'binding-details-source-synthetic.png')))
                timer.stop()
                modal.accept()
            elif time.monotonic()-began > 5:
                timer.stop()
                if modal:
                    modal.close()

        timer.timeout.connect(capture)
        timer.start()
        dialog.binding_details()
        timer.stop()
        if captured != [True]:
            raise RuntimeError(('Detail screenshot failed', fixture.f.errors))
    finally:
        fixture.tearDown()
    report = {'synthetic':True, 'source_qt':True, 'exe':False, 'live_platform':False,
              'production_stylesheet':True,
              'screenshots_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob('*.png')}}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
