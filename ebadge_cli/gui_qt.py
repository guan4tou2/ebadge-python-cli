"""E87 Badge GUI — PySide6 版本 (Stage 1-2: 裝置連線 + 單圖/多圖/QR 預覽)。

啟動方式:
    pip install PySide6
    python3 -m ebadge_cli.gui_qt
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider, QSpinBox,
    QStackedWidget, QStatusBar, QTextEdit, QVBoxLayout, QWidget,
)


# ── 常駐 asyncio loop：所有 BLE 任務共用同一個 loop，避免跨 loop Future 錯誤 ──

class _BleLoop:
    """單例常駐 asyncio loop（背景執行緒）。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        """把 coroutine 送進常駐 loop，回傳 concurrent.futures.Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


BLE_LOOP: Optional[_BleLoop] = None


def _ble_loop() -> _BleLoop:
    global BLE_LOOP
    if BLE_LOOP is None:
        BLE_LOOP = _BleLoop()
    return BLE_LOOP


class AsyncRunner(QObject):
    """包裝 coroutine_threadsafe，結果/錯誤由 Qt signal 回主執行緒。"""

    finished = Signal(object)
    failed = Signal(object)
    progress = Signal(str)

    def run(self, coro_factory: Callable) -> None:
        fut = _ble_loop().submit(coro_factory())

        def done_cb(f):
            try:
                result = f.result()
                self.finished.emit(result)
            except Exception as exc:
                self.failed.emit(exc)

        fut.add_done_callback(done_cb)


# ── 左側導航按鈕（icon + 文字，垂直排列）──

NAV_ITEMS = [
    ("image",    "單圖",      "🖼"),
    ("multi",    "多圖",      "🎞"),
    ("video",    "影片/GIF",  "🎬"),
    ("danmaku",  "文字",      "💬"),
    ("qr",       "QR Code",   "▦"),
    ("pattern",  "圖案",      "✨"),
    # OTA / 設定 暫隱藏 — 此韌體不回 bind response，無法取得 serial/fw version
    # ("ota",      "OTA 更新",  "🔄"),
    # ("settings", "設定",      "⚙"),
]


def _make_nav_icon(emoji: str) -> QIcon:
    """用 emoji 畫一個 48×48 icon (無第三方 icon 資源時的替代)."""
    pix = QPixmap(48, 48)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont()
    f.setPointSize(22)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignCenter, emoji)
    p.end()
    return QIcon(pix)


# ── WYSIWYG 368×368 圓形預覽 widget ──

BADGE_SIZE = 368
PREVIEW_DISPLAY = 320  # 顯示尺寸 (縮小讓 UI 不佔滿)


def _pil_to_qpixmap(pil_img) -> QPixmap:
    """PIL.Image(RGB) → QPixmap."""
    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")
    data = pil_img.tobytes("raw", "RGB")
    qimg = QImage(data, pil_img.width, pil_img.height, pil_img.width * 3, QImage.Format_RGB888)
    return QPixmap.fromImage(qimg.copy())


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


class BadgePreview(QWidget):
    """固定尺寸圓形預覽，可顯示單張或播放幀序列 (QTimer)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(PREVIEW_DISPLAY + 16, PREVIEW_DISPLAY + 16)
        self._frames: list[QPixmap] = []
        self._idx = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next_frame)
        self._placeholder = True

    def clear(self) -> None:
        self._timer.stop()
        self._frames = []
        self._placeholder = True
        self.update()

    def set_image(self, pil_img) -> None:
        self._timer.stop()
        self._frames = [_pil_to_qpixmap(pil_img)] if pil_img is not None else []
        self._placeholder = not self._frames
        self._idx = 0
        self.update()

    def set_frames(self, pil_frames: list, fps: int = 12) -> None:
        self._timer.stop()
        self._frames = [_pil_to_qpixmap(f) for f in pil_frames] if pil_frames else []
        self._placeholder = not self._frames
        self._idx = 0
        if len(self._frames) > 1:
            self._timer.start(max(1, int(1000 / max(1, fps))))
        self.update()

    def _next_frame(self) -> None:
        if not self._frames:
            return
        self._idx = (self._idx + 1) % len(self._frames)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 外框 (徽章外觀)
        cx, cy = self.width() / 2, self.height() / 2
        r = PREVIEW_DISPLAY / 2
        p.setBrush(QColor("#111"))
        p.setPen(QColor("#333"))
        p.drawEllipse(int(cx - r - 4), int(cy - r - 4),
                      int((r + 4) * 2), int((r + 4) * 2))
        # 裁剪成圓形
        path = QPainterPath()
        path.addEllipse(cx - r, cy - r, r * 2, r * 2)
        p.setClipPath(path)
        if self._placeholder or not self._frames:
            p.fillRect(self.rect(), QColor("#222"))
            p.setPen(QColor("#666"))
            f = QFont(); f.setPointSize(14); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "預覽\n368×368")
        else:
            pix = self._frames[self._idx]
            p.drawPixmap(int(cx - r), int(cy - r),
                         int(r * 2), int(r * 2), pix)
        p.end()


# ── 色彩選擇按鈕 ──

class ColorButton(QPushButton):
    colorChanged = Signal(str)

    def __init__(self, initial: str = "#ffffff") -> None:
        super().__init__()
        self.setFixedSize(36, 24)
        self._color = initial
        self._apply()
        self.clicked.connect(self._pick)

    def color(self) -> str:
        return self._color

    def setColor(self, c: str) -> None:
        self._color = c
        self._apply()

    def _apply(self) -> None:
        self.setStyleSheet(
            f"QPushButton{{background:{self._color};border:1px solid #555;border-radius:3px;}}"
        )

    def _pick(self) -> None:
        dlg = QColorDialog(QColor(self._color), self)
        if dlg.exec():
            c = dlg.selectedColor().name()
            self._color = c
            self._apply()
            self.colorChanged.emit(c)


# ── 共用 page 基底：左邊參數、右邊預覽 ──

class BasePage(QWidget):
    """提供左參數 + 右預覽的標準版面。"""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_ref = window
        self.setStyleSheet(
            "QWidget{color:#e8e8e8;}"
            "QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox{background:#3a3a3a;color:#e8e8e8;border:1px solid #555;padding:3px 6px;border-radius:3px;}"
            "QPushButton{background:#3a3a3a;color:#e8e8e8;border:1px solid #555;padding:6px 14px;border-radius:4px;}"
            "QPushButton:hover{background:#4a4a4a;}"
            "QPushButton:pressed{background:#0a84ff;border-color:#0a84ff;}"
            "QPushButton#primary{background:#0a84ff;border-color:#0a84ff;color:white;font-weight:500;}"
            "QPushButton#primary:hover{background:#3a9bff;}"
            "QPushButton:disabled{color:#666;background:#2f2f2f;border-color:#3a3a3a;}"
            "QListWidget{background:#2b2b2b;border:1px solid #444;}"
            "QSlider::groove:horizontal{background:#3a3a3a;height:4px;border-radius:2px;}"
            "QSlider::handle:horizontal{background:#0a84ff;width:14px;margin:-5px 0;border-radius:7px;}"
        )
        root = QHBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(20)
        self.form = QVBoxLayout()
        self.form.setSpacing(10)
        left = QWidget()
        left.setLayout(self.form)
        root.addWidget(left, 1)
        self.preview = BadgePreview()
        right = QVBoxLayout()
        right.setAlignment(Qt.AlignTop)
        right.addWidget(self.preview, alignment=Qt.AlignCenter)
        lbl = QLabel("WYSIWYG 預覽")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet("color:#888;font-size:11px;")
        right.addWidget(lbl)
        root.addLayout(right)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(180)  # debounce
        self._preview_timer.timeout.connect(self.refresh_preview)

    def debounced_refresh(self) -> None:
        self._preview_timer.start()

    def refresh_preview(self) -> None:
        """子類實作."""
        pass

    def _row(self, *widgets, label: str | None = None) -> QHBoxLayout:
        row = QHBoxLayout()
        if label is not None:
            lbl = QLabel(label)
            lbl.setMinimumWidth(90)
            row.addWidget(lbl)
        for w in widgets:
            row.addWidget(w)
        row.addStretch(1)
        self.form.addLayout(row)
        return row


# ── 單圖 ──

FIT_MODES = [("填滿 (裁切)", "cover"), ("完整 (留邊)", "contain"), ("拉伸", "stretch")]


def _fit_zoom_controls(page: "BasePage") -> tuple[QComboBox, QSlider, QLabel]:
    """建立 fit + zoom 共用控制列，附到 page.form。回傳 (fit_combo, zoom_slider, zoom_label)."""
    fit_combo = QComboBox()
    for label, _ in FIT_MODES:
        fit_combo.addItem(label)
    fit_combo.currentIndexChanged.connect(lambda _: page.debounced_refresh())
    page._row(fit_combo, label="填滿模式:")

    zoom_slider = QSlider(Qt.Horizontal)
    zoom_slider.setRange(50, 200); zoom_slider.setValue(100)
    zoom_label = QLabel("1.00×")
    zoom_slider.valueChanged.connect(
        lambda v: (zoom_label.setText(f"{v/100:.2f}×"), page.debounced_refresh())
    )
    page._row(zoom_slider, zoom_label, label="縮放:")
    return fit_combo, zoom_slider, zoom_label


class ImagePage(BasePage):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("選擇 PNG/JPG/BMP/WEBP...")
        btn_browse = QPushButton("瀏覽…")
        btn_browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(QLabel("圖片檔案:"))
        row.addWidget(self.path_edit, 1)
        row.addWidget(btn_browse)
        self.form.addLayout(row)

        self.fit_combo, self.zoom_slider, self.zoom_label = _fit_zoom_controls(self)

        self.btn_upload = QPushButton("上傳到徽章")
        self.btn_upload.setObjectName("primary")
        self.btn_upload.clicked.connect(self._upload)
        self.form.addStretch(1)
        self.form.addWidget(self.btn_upload)

    def _browse(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "選擇圖片", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All (*.*)")
        if p:
            self.path_edit.setText(p)
            self.refresh_preview()

    def _transform(self):
        from PIL import Image
        from ebadge_cli.image_converter import transform_to_badge
        path = self.path_edit.text()
        if not path or not os.path.isfile(path):
            return None
        img = Image.open(path)
        fit = FIT_MODES[self.fit_combo.currentIndex()][1]
        zoom = self.zoom_slider.value() / 100.0
        return transform_to_badge(img, fit=fit, zoom=zoom)

    def refresh_preview(self) -> None:
        try:
            img = self._transform()
            if img is None:
                self.preview.clear(); return
            # encode & decode to match on-badge JPEG quality
            from ebadge_cli.image_converter import encode_badge_jpeg
            from PIL import Image
            jpeg = encode_badge_jpeg(img)
            self.preview.set_image(Image.open(io.BytesIO(jpeg)).convert("RGB"))
        except Exception as exc:
            self.window_ref.log_msg(f"預覽失敗: {exc}")
            self.preview.clear()

    def _upload(self) -> None:
        if not self.window_ref.require_connection():
            return
        if not self.path_edit.text():
            QMessageBox.warning(self, "提示", "請選擇圖片"); return
        from ebadge_cli.image_converter import encode_badge_jpeg
        from ebadge_cli.rcsp_transfer import run_e87_upload
        try:
            img = self._transform()
            data = encode_badge_jpeg(img)
        except Exception as exc:
            QMessageBox.critical(self, "錯誤", str(exc)); return
        self.window_ref.log_msg(f"圖片: {len(data)} bytes")
        addr = self.window_ref._connected_device.address
        ble_device = self.window_ref.take_ble_device()
        cb = self.window_ref.make_progress_cb()

        async def task():
            return await run_e87_upload(
                name=None, address=addr, file_bytes=data, upload_mode="image",
                timeout=60.0, verbose=True, on_progress=cb, device=ble_device,
            )

        def done(r):
            self.window_ref.log_msg("上傳成功!" if r.success else f"失敗: {r.error}")

        self.window_ref.run_async(task, done)


# ── 多圖 ──

class MultiPage(BasePage):
    """多圖：每張可獨立設定 fit / zoom / duration."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._items: list[dict] = []   # [{path, fit, zoom, duration}]
        self._loading = False          # 防止載入時觸發 change→save

        self.list_widget = QListWidget()
        self.list_widget.setFixedHeight(170)
        self.list_widget.currentRowChanged.connect(self._on_row_changed)

        btns = QHBoxLayout()
        b_add = QPushButton("加入"); b_add.clicked.connect(self._add)
        b_remove = QPushButton("移除"); b_remove.clicked.connect(self._remove)
        b_clear = QPushButton("清空"); b_clear.clicked.connect(self._clear)
        b_up = QPushButton("↑"); b_up.clicked.connect(lambda: self._move(-1))
        b_dn = QPushButton("↓"); b_dn.clicked.connect(lambda: self._move(1))
        b_apply_all = QPushButton("套用到全部"); b_apply_all.clicked.connect(self._apply_all)
        self.b_play_all = QPushButton("▶ 預覽全部")
        self.b_play_all.setCheckable(True)
        self.b_play_all.toggled.connect(lambda _: self.refresh_preview())
        for b in (b_add, b_remove, b_clear, b_up, b_dn, b_apply_all, self.b_play_all):
            btns.addWidget(b)
        btns.addStretch(1)

        self.form.addWidget(QLabel("圖片列表 (選取單張後下方設定僅套用到該張):"))
        self.form.addWidget(self.list_widget)
        self.form.addLayout(btns)

        # Per-item controls
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.1, 30.0); self.duration_spin.setValue(2.0); self.duration_spin.setSuffix(" s")
        self.duration_spin.valueChanged.connect(lambda _: self._save_current())
        self._row(self.duration_spin, label="此張時長:")

        self.fit_combo, self.zoom_slider, self.zoom_label = _fit_zoom_controls(self)
        # 覆寫原本只 debounce 預覽的 handler，改成「存 + 預覽」
        self.fit_combo.currentIndexChanged.disconnect()
        self.fit_combo.currentIndexChanged.connect(lambda _: self._save_current())
        self.zoom_slider.valueChanged.disconnect()
        self.zoom_slider.valueChanged.connect(
            lambda v: (self.zoom_label.setText(f"{v/100:.2f}×"), self._save_current())
        )

        self.btn_upload = QPushButton("上傳 Slideshow")
        self.btn_upload.setObjectName("primary")
        self.btn_upload.clicked.connect(self._upload)
        self.form.addStretch(1)
        self.form.addWidget(self.btn_upload)

        self._set_controls_enabled(False)

    # ── item helpers ──

    def _set_controls_enabled(self, on: bool) -> None:
        for w in (self.duration_spin, self.fit_combo, self.zoom_slider):
            w.setEnabled(on)

    def _label_for(self, item: dict) -> str:
        return (
            f"{os.path.basename(item['path'])}   "
            f"[{item['duration']:.1f}s · {item['fit']} · {item['zoom']:.2f}×]"
        )

    def _refresh_list_labels(self) -> None:
        for i, it in enumerate(self._items):
            self.list_widget.item(i).setText(self._label_for(it))

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "選擇圖片", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All (*.*)")
        if not paths:
            return
        for p in paths:
            it = {"path": p, "fit": "cover", "zoom": 1.0, "duration": 2.0}
            self._items.append(it)
            self.list_widget.addItem(self._label_for(it))
        # 選中第一個新增的
        self.list_widget.setCurrentRow(len(self._items) - len(paths))
        self.refresh_preview()

    def _remove(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        self._items.pop(row)
        self.list_widget.takeItem(row)
        if not self._items:
            self._set_controls_enabled(False)
        self.refresh_preview()

    def _clear(self) -> None:
        self._items.clear()
        self.list_widget.clear()
        self._set_controls_enabled(False)
        self.preview.clear()

    def _move(self, delta: int) -> None:
        row = self.list_widget.currentRow()
        new_row = row + delta
        if row < 0 or not (0 <= new_row < len(self._items)):
            return
        self._items[row], self._items[new_row] = self._items[new_row], self._items[row]
        self._refresh_list_labels()
        self.list_widget.setCurrentRow(new_row)
        self.refresh_preview()

    def _apply_all(self) -> None:
        """把目前選取的 fit/zoom/duration 套用到所有圖片."""
        if not self._items:
            return
        fit = FIT_MODES[self.fit_combo.currentIndex()][1]
        zoom = self.zoom_slider.value() / 100.0
        dur = self.duration_spin.value()
        for it in self._items:
            it["fit"] = fit; it["zoom"] = zoom; it["duration"] = dur
        self._refresh_list_labels()
        self.refresh_preview()

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._items):
            self._set_controls_enabled(False)
            return
        # 選到某張時，自動退出「預覽全部」模式，讓使用者看得到該張靜態畫面
        if self.b_play_all.isChecked():
            self.b_play_all.blockSignals(True)
            self.b_play_all.setChecked(False)
            self.b_play_all.blockSignals(False)
        self._set_controls_enabled(True)
        it = self._items[row]
        self._loading = True
        try:
            idx = next(i for i, (_, k) in enumerate(FIT_MODES) if k == it["fit"])
        except StopIteration:
            idx = 0
        self.fit_combo.setCurrentIndex(idx)
        self.zoom_slider.setValue(int(it["zoom"] * 100))
        self.zoom_label.setText(f"{it['zoom']:.2f}×")
        self.duration_spin.setValue(it["duration"])
        self._loading = False
        self.refresh_preview()

    def _save_current(self) -> None:
        if self._loading:
            return
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._items):
            return
        it = self._items[row]
        it["fit"] = FIT_MODES[self.fit_combo.currentIndex()][1]
        it["zoom"] = self.zoom_slider.value() / 100.0
        it["duration"] = self.duration_spin.value()
        self.list_widget.item(row).setText(self._label_for(it))
        self.debounced_refresh()

    # ── frames / preview ──

    def _frames_and_repeats(self, preview_fps: int = 12) -> tuple[list, list[int]]:
        """每張轉換後的 PIL 幀 + 每張重複次數（整張片段）."""
        from PIL import Image
        from ebadge_cli.image_converter import transform_to_badge
        frames, repeats = [], []
        for it in self._items:
            if not os.path.isfile(it["path"]):
                continue
            img = Image.open(it["path"])
            frames.append(transform_to_badge(img, fit=it["fit"], zoom=it["zoom"]))
            repeats.append(max(1, int(round(it["duration"] * preview_fps))))
        return frames, repeats

    def refresh_preview(self) -> None:
        if not self._items:
            self.preview.clear(); return
        try:
            # 選取單張且未啟用「預覽全部」: 只顯示該張 (方便調整)
            row = self.list_widget.currentRow()
            if not self.b_play_all.isChecked() and 0 <= row < len(self._items):
                from PIL import Image
                from ebadge_cli.image_converter import transform_to_badge
                it = self._items[row]
                if not os.path.isfile(it["path"]):
                    self.preview.clear(); return
                img = transform_to_badge(
                    Image.open(it["path"]), fit=it["fit"], zoom=it["zoom"]
                )
                self.preview.set_image(img)
                return
            # 否則: 播整個 slideshow
            fps = 12
            frames, repeats = self._frames_and_repeats(fps)
            if not frames:
                self.preview.clear(); return
            seq = []
            for f, r in zip(frames, repeats):
                seq.extend([f] * r)
            self.preview.set_frames(seq, fps=fps)
        except Exception as exc:
            self.window_ref.log_msg(f"多圖預覽失敗: {exc}")

    def _upload(self) -> None:
        if not self.window_ref.require_connection():
            return
        if not self._items:
            QMessageBox.warning(self, "提示", "請加入圖片"); return
        from ebadge_cli.avi_builder import build_mjpg_avi
        from ebadge_cli.image_converter import encode_video_jpeg
        from ebadge_cli.rcsp_transfer import run_e87_upload
        try:
            fps = 12
            frames, repeats = self._frames_and_repeats(fps)
            jpegs = [encode_video_jpeg(f) for f in frames]
            seq: list[bytes] = []
            for j, r in zip(jpegs, repeats):
                seq.extend([j] * r)
            data = build_mjpg_avi(seq, width=BADGE_SIZE, height=BADGE_SIZE, fps=fps)
        except Exception as exc:
            QMessageBox.critical(self, "錯誤", str(exc)); return
        if not self.window_ref.check_upload_size(len(data), self):
            return
        self.window_ref.log_msg(
            f"多圖: {len(frames)} 張, 總 {sum(repeats)} 幀, AVI {len(data)} bytes"
        )
        addr = self.window_ref._connected_device.address
        ble_device = self.window_ref.take_ble_device()
        cb = self.window_ref.make_progress_cb()

        async def task():
            return await run_e87_upload(
                name=None, address=addr, file_bytes=data, upload_mode="video",
                timeout=120.0, verbose=True, on_progress=cb, device=ble_device,
            )

        def done(r):
            self.window_ref.log_msg("上傳成功!" if r.success else f"失敗: {r.error}")

        self.window_ref.run_async(task, done)


# ── QR Code ──

class QrPage(BasePage):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)

        self.text_edit = QLineEdit("https://")
        self.text_edit.textChanged.connect(self.debounced_refresh)
        self._row(self.text_edit, label="內容:")

        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(85, 145); self.zoom_slider.setValue(100)
        self.zoom_label = QLabel("1.00")
        self.zoom_slider.valueChanged.connect(
            lambda v: (self.zoom_label.setText(f"{v/100:.2f}"), self.debounced_refresh())
        )
        self._row(self.zoom_slider, self.zoom_label, label="縮放:")

        self.fg_btn = ColorButton("#000000")
        self.fg_btn.colorChanged.connect(lambda _: self.debounced_refresh())
        self.bg_btn = ColorButton("#ffffff")
        self.bg_btn.colorChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.fg_btn, QLabel("   背景:"), self.bg_btn, label="前景:")

        self.btn_upload = QPushButton("上傳 QR Code")
        self.btn_upload.setObjectName("primary")
        self.btn_upload.clicked.connect(self._upload)
        self.form.addStretch(1)
        self.form.addWidget(self.btn_upload)

        QTimer.singleShot(0, self.refresh_preview)

    def refresh_preview(self) -> None:
        text = self.text_edit.text().strip() or "https://"
        try:
            from ebadge_cli.image_converter import prepare_qr
            from PIL import Image
            data = prepare_qr(
                data=text,
                fg_color=_hex_to_rgb(self.fg_btn.color()),
                bg_color=_hex_to_rgb(self.bg_btn.color()),
                zoom=self.zoom_slider.value() / 100.0,
            )
            img = Image.open(io.BytesIO(data)).convert("RGB")
            self.preview.set_image(img)
        except Exception as exc:
            self.window_ref.log_msg(f"QR 預覽失敗: {exc}")

    def _upload(self) -> None:
        if not self.window_ref.require_connection():
            return
        text = self.text_edit.text().strip()
        if not text:
            QMessageBox.warning(self, "提示", "請輸入內容"); return
        from ebadge_cli.image_converter import prepare_qr
        from ebadge_cli.rcsp_transfer import run_e87_upload
        try:
            data = prepare_qr(
                data=text,
                fg_color=_hex_to_rgb(self.fg_btn.color()),
                bg_color=_hex_to_rgb(self.bg_btn.color()),
                zoom=self.zoom_slider.value() / 100.0,
            )
        except Exception as exc:
            QMessageBox.critical(self, "錯誤", str(exc)); return
        self.window_ref.log_msg(f"QR: {len(data)} bytes")
        addr = self.window_ref._connected_device.address
        ble_device = self.window_ref.take_ble_device()
        cb = self.window_ref.make_progress_cb()

        async def task():
            return await run_e87_upload(
                name=None, address=addr, file_bytes=data, upload_mode="qr",
                timeout=60.0, verbose=True, on_progress=cb, device=ble_device,
            )

        def done(r):
            self.window_ref.log_msg("上傳成功!" if r.success else f"失敗: {r.error}")

        self.window_ref.run_async(task, done)


# ── 彈幕 ──

def _list_system_fonts() -> list[tuple[str, Optional[str]]]:
    """回傳 [(顯示名, 路徑或 None)]，第一項為系統預設."""
    out: list[tuple[str, Optional[str]]] = [("(系統預設)", None)]
    for d in ("/System/Library/Fonts", "/Library/Fonts",
              os.path.expanduser("~/Library/Fonts")):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith((".ttf", ".ttc", ".otf")):
                out.append((f, os.path.join(d, f)))
    return out


TEXT_MODES = [
    ("滾動 (跑馬燈)", "scroll"),
    ("靜置 (置中)", "static"),
    ("圓形旋轉", "circle"),
    ("脈衝縮放", "pulse"),
    ("波浪", "wave"),
]


def _build_searchable_font_combo(fonts: list[tuple[str, Optional[str]]]) -> QComboBox:
    """字體 combo：可編輯、自動補全、case-insensitive 子字串搜尋。"""
    from PySide6.QtWidgets import QCompleter

    combo = QComboBox()
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    combo.setMinimumWidth(240)
    combo.setMaxVisibleItems(20)  # 選單最多顯示 20 項
    names = [f[0] for f in fonts]
    combo.addItems(names)
    completer = QCompleter(names, combo)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setFilterMode(Qt.MatchContains)
    completer.setCompletionMode(QCompleter.PopupCompletion)
    combo.setCompleter(completer)
    return combo


class DanmakuPage(BasePage):
    """文字頁：支援 scroll / static / circle / pulse / wave 五種模式."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.text_edit = QLineEdit("Hello E87!")
        self.text_edit.textChanged.connect(self.debounced_refresh)
        self._row(self.text_edit, label="文字:")

        self.mode_combo = QComboBox()
        for label, _ in TEXT_MODES:
            self.mode_combo.addItem(label)
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.currentIndexChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.mode_combo, label="動畫模式:")

        self._fonts = _list_system_fonts()
        self.font_combo = _build_searchable_font_combo(self._fonts)
        self.font_combo.currentIndexChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.font_combo, label="字體:")

        # 字型粗細（多數字體檔本身已內含，這裡用 PIL font_variant 的輔助）
        # 簡化：給一個「加粗」開關（用 PIL stroke 實現）
        self.bold_check = QComboBox()
        self.bold_check.addItems(["一般", "粗體 (描邊加粗)"])
        self.bold_check.currentIndexChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.bold_check, label="字型樣式:")

        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(16, 200); self.size_slider.setValue(64)
        self.size_label = QLabel("64")
        self.size_slider.valueChanged.connect(
            lambda v: (self.size_label.setText(str(v)), self.debounced_refresh()))
        self._row(self.size_slider, self.size_label, label="字體大小:")

        self.dur_spin = QDoubleSpinBox()
        self.dur_spin.setRange(0.5, 60.0); self.dur_spin.setValue(5.0); self.dur_spin.setSuffix(" s")
        self.dur_spin.valueChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.dur_spin, label="時長:")

        self.fg_btn = ColorButton("#ffffff")
        self.fg_btn.colorChanged.connect(lambda _: self.debounced_refresh())
        self.bg_btn = ColorButton("#000000")
        self.bg_btn.colorChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.fg_btn, QLabel("   背景:"), self.bg_btn, label="文字顏色:")

        self.btn_upload = QPushButton("上傳文字")
        self.btn_upload.setObjectName("primary")
        self.btn_upload.clicked.connect(self._upload)
        self.form.addStretch(1)
        self.form.addWidget(self.btn_upload)

        QTimer.singleShot(0, self.refresh_preview)

    def _font_path(self) -> Optional[str]:
        """依目前 combo 的顯示字串反查路徑（支援 editable text）。"""
        name = self.font_combo.currentText()
        for n, p in self._fonts:
            if n == name:
                return p
        return None

    def _mode(self) -> str:
        idx = self.mode_combo.currentIndex()
        return TEXT_MODES[idx][1] if 0 <= idx < len(TEXT_MODES) else "scroll"

    def _params(self) -> dict:
        return dict(
            text=self.text_edit.text() or "Hello",
            mode=self._mode(),
            duration=self.dur_spin.value(),
            font_size=self.size_slider.value(),
            font_color=_hex_to_rgb(self.fg_btn.color()),
            bg_color=_hex_to_rgb(self.bg_btn.color()),
            font_path=self._font_path(),
            bold=self.bold_check.currentIndex() == 1,
        )

    def refresh_preview(self) -> None:
        try:
            from ebadge_cli.image_converter import text_frames
            frames = text_frames(fps=12, **self._params())
            if frames:
                self.preview.set_frames(frames, fps=12)
            else:
                self.preview.clear()
        except Exception as exc:
            self.window_ref.log_msg(f"文字預覽失敗: {exc}")

    def _upload(self) -> None:
        if not self.window_ref.require_connection():
            return
        from ebadge_cli.image_converter import prepare_text
        from ebadge_cli.rcsp_transfer import run_e87_upload
        try:
            data = prepare_text(**self._params())
        except Exception as exc:
            QMessageBox.critical(self, "錯誤", str(exc)); return
        if not self.window_ref.check_upload_size(len(data), self):
            return
        self.window_ref.log_msg(f"文字({self._mode()}): {len(data)} bytes")
        addr = self.window_ref._connected_device.address
        ble_device = self.window_ref.take_ble_device()
        cb = self.window_ref.make_progress_cb()

        async def task():
            return await run_e87_upload(
                name=None, address=addr, file_bytes=data, upload_mode="video",
                timeout=120.0, verbose=True, on_progress=cb, device=ble_device,
            )

        def done(r):
            self.window_ref.log_msg("上傳成功!" if r.success else f"失敗: {r.error}")

        self.window_ref.run_async(task, done)


# ── 影片 / GIF ──

class VideoPage(BasePage):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("MP4/AVI/MOV/MKV/GIF/WEBP")
        btn = QPushButton("瀏覽…"); btn.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(QLabel("檔案:")); row.addWidget(self.path_edit, 1); row.addWidget(btn)
        self.form.addLayout(row)

        self.fps_combo = QComboBox()
        for fps in (24, 16, 12, 8):
            self.fps_combo.addItem(f"{fps} fps", userData=fps)
        self.fps_combo.setCurrentIndex(2)  # 預設 12
        self.fps_combo.currentIndexChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.fps_combo, label="視訊頻率:")

        self.quality_combo = QComboBox()
        # (標籤, ffmpeg -q:v, PIL quality)  數值越低品質越高
        self.quality_combo.addItem("高 (較大)", userData=(3, 88))
        self.quality_combo.addItem("中", userData=(5, 75))
        self.quality_combo.addItem("低 (較小)", userData=(8, 55))
        self.quality_combo.setCurrentIndex(1)
        self.quality_combo.currentIndexChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.quality_combo, label="品質:")

        self.dur_edit = QLineEdit()
        self.dur_edit.setPlaceholderText("完整影片 (可空)")
        self.dur_edit.textChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.dur_edit, label="截取(秒):")

        self.fit_combo, self.zoom_slider, self.zoom_label = _fit_zoom_controls(self)

        self.btn_upload = QPushButton("上傳影片")
        self.btn_upload.setObjectName("primary")
        self.btn_upload.clicked.connect(self._upload)
        self.form.addStretch(1)
        self.form.addWidget(self.btn_upload)

    def _browse(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "選擇影片", "",
            "Video (*.mp4 *.avi *.mov *.mkv *.gif *.webp *.apng *.png);;All (*.*)")
        if p:
            self.path_edit.setText(p)
            self.refresh_preview()

    def _duration(self) -> Optional[float]:
        s = self.dur_edit.text().strip()
        try:
            return float(s) if s else None
        except ValueError:
            return None

    def _fps(self) -> int:
        return self.fps_combo.currentData() or 12

    def _quality(self) -> tuple[int, int]:
        return self.quality_combo.currentData() or (5, 75)

    def refresh_preview(self) -> None:
        path = self.path_edit.text()
        if not path or not os.path.isfile(path):
            self.preview.clear(); return
        try:
            from ebadge_cli.image_converter import video_frames
            fit = FIT_MODES[self.fit_combo.currentIndex()][1]
            zoom = self.zoom_slider.value() / 100.0
            fps = self._fps()
            # 預覽限制 ≤ 5 秒 避免卡
            preview_dur = self._duration()
            if preview_dur is None or preview_dur > 5.0:
                preview_dur = 5.0
            frames = video_frames(path, fps=fps, duration=preview_dur,
                                   fit=fit, zoom=zoom)
            if frames:
                self.preview.set_frames(frames, fps=fps)
            else:
                self.preview.clear()
        except Exception as exc:
            self.window_ref.log_msg(f"影片預覽失敗: {exc}")
            self.preview.clear()

    def _upload(self) -> None:
        if not self.window_ref.require_connection():
            return
        if not self.path_edit.text():
            QMessageBox.warning(self, "提示", "請選擇檔案"); return
        from ebadge_cli.avi_builder import build_mjpg_avi
        from ebadge_cli.image_converter import (
            encode_video_jpeg, prepare_video, video_frames
        )
        from ebadge_cli.rcsp_transfer import run_e87_upload
        try:
            fit = FIT_MODES[self.fit_combo.currentIndex()][1]
            zoom = self.zoom_slider.value() / 100.0
            fps = self._fps()
            ffmpeg_q, pil_q = self._quality()
            # 預設 fit/zoom 直接走 CLI 的 prepare_video (ffmpeg 單次編碼，檔案小)
            if fit == "cover" and abs(zoom - 1.0) < 0.001:
                data = prepare_video(
                    self.path_edit.text(), fps=fps,
                    duration=self._duration(), jpeg_quality=ffmpeg_q,
                )
                frame_count = 0  # CLI 路徑不回傳幀數
            else:
                # 自訂 fit/zoom：走 PIL 路徑
                frames = video_frames(
                    self.path_edit.text(), fps=fps,
                    duration=self._duration(), fit=fit, zoom=zoom
                )
                jpegs = [encode_video_jpeg(f, quality=pil_q) for f in frames]
                data = build_mjpg_avi(jpegs, width=BADGE_SIZE, height=BADGE_SIZE, fps=fps)
                frame_count = len(frames)
        except Exception as exc:
            QMessageBox.critical(self, "錯誤", str(exc)); return
        if not self.window_ref.check_upload_size(len(data), self):
            return
        self.window_ref.log_msg(f"影片: {len(data)} bytes" +
                                 (f", {frame_count} 幀" if frame_count else ""))
        addr = self.window_ref._connected_device.address
        ble_device = self.window_ref.take_ble_device()
        cb = self.window_ref.make_progress_cb()

        async def task():
            return await run_e87_upload(
                name=None, address=addr, file_bytes=data, upload_mode="video",
                timeout=180.0, verbose=True, on_progress=cb, device=ble_device,
            )

        def done(r):
            self.window_ref.log_msg("上傳成功!" if r.success else f"失敗: {r.error}")

        self.window_ref.run_async(task, done)


# ── 圖案動畫 ──

PATTERN_TYPES = [
    ("gradient", "漸變"),
    ("pulse", "脈衝"),
    ("checker", "棋盤格"),
    ("rainbow", "彩虹"),
    ("wave", "波浪"),
]


class PatternPage(BasePage):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.type_combo = QComboBox()
        for k, label in PATTERN_TYPES:
            self.type_combo.addItem(f"{label} ({k})", userData=k)
        self.type_combo.currentIndexChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.type_combo, label="圖案:")

        self.frames_spin = QSpinBox()
        self.frames_spin.setRange(8, 240); self.frames_spin.setValue(60)
        self.frames_spin.valueChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.frames_spin, label="幀數:")

        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30); self.fps_spin.setValue(12)
        self.fps_spin.valueChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.fps_spin, label="FPS:")

        self.c1_btn = ColorButton("#ff0000")
        self.c1_btn.colorChanged.connect(lambda _: self.debounced_refresh())
        self.c2_btn = ColorButton("#0000ff")
        self.c2_btn.colorChanged.connect(lambda _: self.debounced_refresh())
        self._row(self.c1_btn, QLabel("   副色:"), self.c2_btn, label="主色:")

        self.btn_upload = QPushButton("上傳圖案")
        self.btn_upload.setObjectName("primary")
        self.btn_upload.clicked.connect(self._upload)
        self.form.addStretch(1)
        self.form.addWidget(self.btn_upload)

        QTimer.singleShot(0, self.refresh_preview)

    def _pattern_key(self) -> str:
        return self.type_combo.currentData() or "gradient"

    def refresh_preview(self) -> None:
        try:
            from ebadge_cli.image_converter import pattern_frames
            frames = pattern_frames(
                pattern=self._pattern_key(),
                frame_count=self.frames_spin.value(),
                color1=_hex_to_rgb(self.c1_btn.color()),
                color2=_hex_to_rgb(self.c2_btn.color()),
            )
            self.preview.set_frames(frames, fps=self.fps_spin.value())
        except Exception as exc:
            self.window_ref.log_msg(f"圖案預覽失敗: {exc}")
            self.preview.clear()

    def _upload(self) -> None:
        if not self.window_ref.require_connection():
            return
        from ebadge_cli.image_converter import prepare_pattern
        from ebadge_cli.rcsp_transfer import run_e87_upload
        try:
            data = prepare_pattern(
                pattern=self._pattern_key(),
                fps=self.fps_spin.value(),
                frame_count=self.frames_spin.value(),
                color1=_hex_to_rgb(self.c1_btn.color()),
                color2=_hex_to_rgb(self.c2_btn.color()),
            )
        except Exception as exc:
            QMessageBox.critical(self, "錯誤", str(exc)); return
        self.window_ref.log_msg(f"圖案: {len(data)} bytes")
        addr = self.window_ref._connected_device.address
        ble_device = self.window_ref.take_ble_device()
        cb = self.window_ref.make_progress_cb()

        async def task():
            return await run_e87_upload(
                name=None, address=addr, file_bytes=data, upload_mode="video",
                timeout=120.0, verbose=True, on_progress=cb, device=ble_device,
            )

        def done(r):
            self.window_ref.log_msg("上傳成功!" if r.success else f"失敗: {r.error}")

        self.window_ref.run_async(task, done)


# ── 設定頁 (綁定 / 時間同步 / 解綁) ──

PAGE_STYLE = (
    "QWidget{color:#e8e8e8;}"
    "QLineEdit,QComboBox,QSpinBox{background:#3a3a3a;color:#e8e8e8;"
    "border:1px solid #555;padding:3px 6px;border-radius:3px;}"
    "QPushButton{background:#3a3a3a;color:#e8e8e8;border:1px solid #555;"
    "padding:6px 14px;border-radius:4px;}"
    "QPushButton:hover{background:#4a4a4a;}"
    "QPushButton:pressed{background:#0a84ff;border-color:#0a84ff;}"
    "QPushButton#primary{background:#0a84ff;border-color:#0a84ff;color:white;font-weight:500;}"
    "QPushButton#primary:hover{background:#3a9bff;}"
    "QPushButton:disabled{color:#666;background:#2f2f2f;border-color:#3a3a3a;}"
    "QLabel#h{font-size:14px;font-weight:500;color:#ccc;}"
)


class SettingsPage(QWidget):
    """綁定 / 時間同步 / 解綁 / 檔案列表."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_ref = window
        self.setStyleSheet(PAGE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        # 時間同步
        root.addWidget(self._h("時間同步"))
        row_t = QHBoxLayout()
        self.lbl_time = QLabel("點「同步現在時間」把電腦時間寫入徽章")
        self.lbl_time.setStyleSheet("color:#888;")
        btn_sync = QPushButton("同步現在時間")
        btn_sync.clicked.connect(self._on_time_sync)
        row_t.addWidget(self.lbl_time, 1); row_t.addWidget(btn_sync)
        root.addLayout(row_t)

        root.addWidget(self._div())

        # 綁定
        root.addWidget(self._h("綁定裝置"))
        self.lang_combo = QComboBox()
        self.lang_combo.addItem("繁體/簡體中文", "zh")
        self.lang_combo.addItem("English", "en")
        self.hour_combo = QComboBox()
        self.hour_combo.addItem("24 小時制", False)
        self.hour_combo.addItem("12 小時制", True)
        self.device_id_edit = QLineEdit()
        self.device_id_edit.setPlaceholderText("留空=自動產生")
        form = QVBoxLayout()
        form.addLayout(self._labeled("語言:", self.lang_combo))
        form.addLayout(self._labeled("時間制式:", self.hour_combo))
        form.addLayout(self._labeled("Device ID:", self.device_id_edit))
        root.addLayout(form)
        btn_bind = QPushButton("綁定")
        btn_bind.setObjectName("primary")
        btn_bind.clicked.connect(self._on_bind)
        row_b = QHBoxLayout(); row_b.addStretch(1); row_b.addWidget(btn_bind)
        root.addLayout(row_b)

        # 綁定結果顯示
        self.bind_info = QLabel("")
        self.bind_info.setStyleSheet("color:#8ac6ff;font-family:Menlo,monospace;font-size:12px;")
        self.bind_info.setWordWrap(True)
        root.addWidget(self.bind_info)

        root.addWidget(self._div())

        # 解綁
        root.addWidget(self._h("危險區"))
        row_u = QHBoxLayout()
        self.lbl_unbind = QLabel("解除綁定後徽章會清除配對資訊")
        self.lbl_unbind.setStyleSheet("color:#888;")
        btn_unbind = QPushButton("解除綁定")
        btn_unbind.setStyleSheet("QPushButton{color:#ff6b6b;border-color:#ff6b6b;}")
        btn_unbind.clicked.connect(self._on_unbind)
        row_u.addWidget(self.lbl_unbind, 1); row_u.addWidget(btn_unbind)
        root.addLayout(row_u)

        root.addStretch(1)

    def _h(self, text: str) -> QLabel:
        lbl = QLabel(text); lbl.setObjectName("h"); return lbl

    def _div(self) -> QFrame:
        d = QFrame(); d.setFrameShape(QFrame.HLine)
        d.setStyleSheet("color:#333;"); return d

    def _labeled(self, label: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        lbl = QLabel(label); lbl.setMinimumWidth(100)
        row.addWidget(lbl); row.addWidget(widget, 1); return row

    def _on_time_sync(self) -> None:
        if not self.window_ref.require_connection():
            return
        from datetime import datetime
        from ebadge_cli.ble_constants import (
            CHAR_NOTIFY_C2E6, CHAR_WRITE_C2E6, SERVICE_C2E6,
        )
        from ebadge_cli.ble_session import BleMode, run_write_only
        from ebadge_cli.cli import _build_command_frame
        now = datetime.now()
        payload = [now.year & 0xFF, (now.year >> 8) & 0xFF,
                   now.month, now.day, now.hour, now.minute, now.second]
        frame = _build_command_frame(cmd=0x02, payload=payload)
        mode = BleMode(SERVICE_C2E6, CHAR_WRITE_C2E6, CHAR_NOTIFY_C2E6)
        addr = self.window_ref._connected_device.address
        ble_device = self.window_ref.take_ble_device()

        async def task():
            await run_write_only(mode, None, addr, 5.0, frame, device=ble_device)
            return now

        def done(t):
            self.window_ref.log_msg(f"時間已同步: {t.strftime('%Y-%m-%d %H:%M:%S')}")

        self.window_ref.run_async(task, done)

    def _on_bind(self) -> None:
        if not self.window_ref.require_connection():
            return
        # 這台 E87 的 pre-auth bind 不回應，改走 RCSP auth 握手
        from ebadge_cli.rcsp_probe import probe_device
        addr = self.window_ref._connected_device.address
        ble_device = self.window_ref.take_ble_device()

        log = self.window_ref.log_via_signal

        async def task():
            return await probe_device(device=ble_device, address=addr, on_log=log)

        def done(resp):
            if resp is None:
                self.window_ref.log_msg("綁定失敗（auth 或 bind 未完成）")
                return
            txt = (
                f"state={resp.state}  pact={resp.pact_version}  "
                f"fw={resp.firmwa_version}  platform={resp.platform}\n"
                f"serial={resp.serial_number}"
            )
            self.bind_info.setText(txt)
            self.window_ref.log_msg(
                f"綁定成功: fw={resp.firmwa_version} serial={resp.serial_number}"
            )
            self.window_ref._bind_info = resp

        self.window_ref.run_async(task, done)

    def _on_unbind(self) -> None:
        if not self.window_ref.require_connection():
            return
        reply = QMessageBox.question(
            self, "確認解綁",
            "確定要解除綁定嗎？徽章會失去和這個帳號的關聯。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        from ebadge_cli.ble_constants import (
            CHAR_NOTIFY_C2E6, CHAR_WRITE_C2E6, SERVICE_C2E6,
        )
        from ebadge_cli.ble_session import BleMode, run_write_only
        from ebadge_cli.cli import _build_command_frame
        # cmd 0x62 unbind (空 payload)
        frame = _build_command_frame(cmd=0x62, payload=[])
        mode = BleMode(SERVICE_C2E6, CHAR_WRITE_C2E6, CHAR_NOTIFY_C2E6)
        addr = self.window_ref._connected_device.address
        ble_device = self.window_ref.take_ble_device()

        async def task():
            await run_write_only(mode, None, addr, 5.0, frame, device=ble_device)
            return True

        def done(_):
            self.window_ref.log_msg("已發送解綁指令")
            self.bind_info.setText("")
            self.window_ref._bind_info = None

        self.window_ref.run_async(task, done)


# ── OTA 頁 ──

class OtaPage(QWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_ref = window
        self.setStyleSheet(PAGE_STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        # 目前版本
        root.addWidget(self._h("裝置韌體"))
        self.lbl_cur = QLabel("未知 — 按下方「讀取版本」")
        self.lbl_cur.setStyleSheet("color:#ccc;")
        root.addWidget(self.lbl_cur)
        btn_probe = QPushButton("讀取版本（走 RCSP auth）")
        btn_probe.clicked.connect(self._on_probe)
        row_r = QHBoxLayout(); row_r.addWidget(btn_probe); row_r.addStretch(1)
        root.addLayout(row_r)

        root.addWidget(self._div())

        # 線上檢查
        root.addWidget(self._h("雲端檢查更新"))
        self.model_combo = QComboBox()
        self.model_combo.addItems(["dev", "release"])
        row_m = QHBoxLayout()
        lbl_m = QLabel("OTA 模型:"); lbl_m.setMinimumWidth(100)
        row_m.addWidget(lbl_m); row_m.addWidget(self.model_combo); row_m.addStretch(1)
        root.addLayout(row_m)
        self.check_result = QLabel("尚未檢查")
        self.check_result.setStyleSheet("color:#8ac6ff;font-family:Menlo,monospace;font-size:12px;")
        self.check_result.setWordWrap(True)
        root.addWidget(self.check_result)
        btn_check = QPushButton("檢查更新")
        btn_check.clicked.connect(self._on_check)
        row_c = QHBoxLayout(); row_c.addWidget(btn_check); row_c.addStretch(1)
        root.addLayout(row_c)

        root.addWidget(self._div())

        # 韌體檔案 (手動或下載)
        root.addWidget(self._h("韌體檔案"))
        self.fw_path = QLineEdit()
        self.fw_path.setReadOnly(True)
        self.fw_path.setPlaceholderText("本機 firmware 檔案 (.ufw/.bin) 或先「下載線上韌體」")
        btn_browse = QPushButton("瀏覽…")
        btn_browse.clicked.connect(self._browse_fw)
        row_f = QHBoxLayout()
        lbl_f = QLabel("檔案:"); lbl_f.setMinimumWidth(100)
        row_f.addWidget(lbl_f); row_f.addWidget(self.fw_path, 1); row_f.addWidget(btn_browse)
        root.addLayout(row_f)

        btn_download = QPushButton("下載線上韌體")
        btn_download.clicked.connect(self._on_download)
        row_d = QHBoxLayout(); row_d.addWidget(btn_download); row_d.addStretch(1)
        root.addLayout(row_d)

        root.addWidget(self._div())

        # 推送
        btn_push = QPushButton("推送韌體到徽章")
        btn_push.setObjectName("primary")
        btn_push.clicked.connect(self._on_push)
        row_p = QHBoxLayout(); row_p.addStretch(1); row_p.addWidget(btn_push)
        root.addLayout(row_p)

        self._download_url: Optional[str] = None
        root.addStretch(1)

    def _h(self, text: str) -> QLabel:
        lbl = QLabel(text); lbl.setObjectName("h"); return lbl

    def _div(self) -> QFrame:
        d = QFrame(); d.setFrameShape(QFrame.HLine)
        d.setStyleSheet("color:#333;"); return d

    def _on_probe(self) -> None:
        if not self.window_ref.require_connection():
            return
        from ebadge_cli.rcsp_probe import probe_device
        ble_device = self.window_ref.take_ble_device()
        addr = self.window_ref._connected_device.address

        log = self.window_ref.log_via_signal

        async def task():
            return await probe_device(device=ble_device, address=addr, on_log=log)

        def done(resp):
            if resp is None:
                self.lbl_cur.setText("讀取失敗 — 請看 log")
                return
            self.window_ref._bind_info = resp
            self.lbl_cur.setText(
                f"firmware = {resp.firmwa_version}   serial = {resp.serial_number}   "
                f"platform = {resp.platform}"
            )
            self.window_ref.log_msg(
                f"讀到韌體版本: fw={resp.firmwa_version} serial={resp.serial_number}"
            )

        self.window_ref.run_async(task, done)

    def _on_check(self) -> None:
        resp = getattr(self.window_ref, "_bind_info", None)
        if resp is None:
            QMessageBox.warning(self, "提示", "請先到「設定」頁按綁定取得版本/序號")
            return
        from ebadge_cli.ota_api import ota_check
        model = self.model_combo.currentText()

        async def task():
            return ota_check(resp.serial_number, resp.firmwa_version, ota_model=model)

        def done(r):
            self.check_result.setText(str(r))
            url = (r or {}).get("download_address") or (r or {}).get("data", {}).get("download_address")
            if url:
                self._download_url = url
                self.window_ref.log_msg(f"雲端有新版，下載連結已記錄")
            else:
                self.window_ref.log_msg("未取得下載連結（可能已是最新版或 API 格式不同）")

        self.window_ref.run_async(task, done)

    def _on_download(self) -> None:
        if not self._download_url:
            QMessageBox.warning(self, "提示", "請先按「檢查更新」取得下載連結")
            return
        from ebadge_cli.ota_api import download_firmware
        url = self._download_url

        async def task():
            import os, tempfile
            data = download_firmware(url)
            path = os.path.join(tempfile.gettempdir(), "e87-firmware.ufw")
            with open(path, "wb") as f:
                f.write(data)
            return path

        def done(p):
            self.fw_path.setText(p)
            self.window_ref.log_msg(f"韌體已下載到 {p}")

        self.window_ref.run_async(task, done)

    def _browse_fw(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "選擇韌體", "", "Firmware (*.ufw *.bin);;All (*.*)"
        )
        if p:
            self.fw_path.setText(p)

    def _on_push(self) -> None:
        if not self.window_ref.require_connection():
            return
        path = self.fw_path.text()
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, "提示", "請先選擇或下載韌體檔案")
            return
        reply = QMessageBox.warning(
            self, "確認推送韌體",
            "韌體更新不可中斷，失敗可能導致裝置故障。\n繼續嗎？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        from ebadge_cli.ble_constants import (
            CHAR_NOTIFY_C2E6, CHAR_WRITE_C2E6, SERVICE_C2E6,
        )
        from ebadge_cli.ble_session import BleMode
        from ebadge_cli.ota_update import run_ota_update

        with open(path, "rb") as f:
            fw = f.read()

        mode = BleMode(SERVICE_C2E6, CHAR_WRITE_C2E6, CHAR_NOTIFY_C2E6)
        addr = self.window_ref._connected_device.address

        def progress(pct: float):
            from PySide6.QtCore import QTimer as _QT
            def apply():
                self.window_ref.set_progress(int(pct))
                self.window_ref.log_msg(f"OTA: {pct:.1f}%")
            _QT.singleShot(0, apply)

        async def task():
            return await run_ota_update(
                mode=mode, name=None, address=addr,
                firmware_bytes=fw, timeout=300.0, verbose=True,
                on_progress=progress,
            )

        def done(r):
            if r.success:
                self.window_ref.log_msg("OTA 完成！裝置將重啟。")
            else:
                self.window_ref.log_msg(f"OTA 失敗: {r.error}")

        self.window_ref.run_async(task, done)


# ── 主視窗 ──

@dataclass
class DeviceInfo:
    name: str
    address: str
    rssi: int | str = "?"
    ble_device: object = None  # 保留 bleak BLEDevice，避免重新掃描


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("E87 Badge Manager")
        self.resize(1000, 680)

        self._devices: list[DeviceInfo] = []
        self._connected_device: Optional[DeviceInfo] = None
        self._badge_memory_kb: Optional[int] = None

        # 中央容器
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 上方裝置列
        root.addWidget(self._build_device_bar())

        # 中段: 左側導航 + 右側 stacked pages
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setFixedWidth(140)
        self.nav.setIconSize(pix_size := self.nav.iconSize())
        self.nav.setSpacing(2)
        self.nav.setStyleSheet(
            "QListWidget{background:#2b2b2b;color:#ddd;border:none;font-size:13px;}"
            "QListWidget::item{padding:10px 8px;}"
            "QListWidget::item:selected{background:#0a84ff;color:white;}"
        )
        for key, label, emoji in NAV_ITEMS:
            item = QListWidgetItem(_make_nav_icon(emoji), label)
            item.setData(Qt.UserRole, key)
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._on_nav_changed)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        for key, label, _ in NAV_ITEMS:
            if key == "image":
                page = ImagePage(self)
            elif key == "multi":
                page = MultiPage(self)
            elif key == "qr":
                page = QrPage(self)
            elif key == "danmaku":
                page = DanmakuPage(self)
            elif key == "video":
                page = VideoPage(self)
            elif key == "pattern":
                page = PatternPage(self)
            elif key == "ota":
                page = OtaPage(self)
            elif key == "settings":
                page = SettingsPage(self)
            else:
                page = QWidget()  # unreachable
            self.pages[key] = page
            self.stack.addWidget(page)

        body.addWidget(self.nav)
        body.addWidget(self.stack, 1)
        body_w = QWidget()
        body_w.setLayout(body)
        root.addWidget(body_w, 1)

        # 底部 log / progress
        root.addWidget(self._build_bottom_bar())

        # 狀態列
        self.setStatusBar(QStatusBar())
        self._set_conn_status(False)

        self.nav.setCurrentRow(0)

    # ── UI builders ──

    def _build_device_bar(self) -> QWidget:
        bar = QFrame()
        bar.setFrameShape(QFrame.StyledPanel)
        bar.setStyleSheet(
            "QFrame{background:#2b2b2b;border-bottom:1px solid #111;}"
            "QLabel{color:#e8e8e8;}"
            "QComboBox{background:#3a3a3a;color:#e8e8e8;border:1px solid #555;padding:3px 6px;border-radius:3px;}"
            "QComboBox QAbstractItemView{background:#3a3a3a;color:#e8e8e8;selection-background-color:#0a84ff;}"
            "QDoubleSpinBox{background:#3a3a3a;color:#e8e8e8;border:1px solid #555;padding:2px 4px;border-radius:3px;}"
            "QPushButton{background:#3a3a3a;color:#e8e8e8;border:1px solid #555;padding:5px 12px;border-radius:4px;}"
            "QPushButton:hover{background:#4a4a4a;}"
            "QPushButton:pressed{background:#0a84ff;border-color:#0a84ff;}"
            "QPushButton:disabled{color:#666;background:#2f2f2f;border-color:#3a3a3a;}"
        )
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(4)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color:#d33;font-size:16px;")
        self.status_text = QLabel("未連線")
        self.status_text.setStyleSheet("font-weight:500;color:#e8e8e8;")
        self.memory_text = QLabel("")
        self.memory_text.setStyleSheet("color:#8ac6ff;font-size:12px;")

        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(180)
        self.device_combo.setMaximumWidth(240)
        self.device_combo.setPlaceholderText("請先掃描")

        self.scan_timeout_spin = QDoubleSpinBox()
        self.scan_timeout_spin.setRange(1.0, 60.0)
        self.scan_timeout_spin.setValue(5.0)
        self.scan_timeout_spin.setSuffix(" s")
        self.scan_timeout_spin.setFixedWidth(80)

        self.btn_scan = QPushButton("掃描")
        self.btn_scan.clicked.connect(self._on_scan)
        self.btn_connect = QPushButton("連線")
        self.btn_connect.setEnabled(False)
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_disconnect = QPushButton("斷線")
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._on_disconnect)
        self.btn_info = QPushButton("資訊")
        self.btn_info.setEnabled(False)
        self.btn_info.clicked.connect(self._query_badge_info)
        self.btn_battery = QPushButton("電量")
        self.btn_battery.setEnabled(False)
        self.btn_battery.clicked.connect(self._on_battery)

        # Row 1: 狀態點 + 狀態文字 + 記憶體資訊  ―  右邊 資訊/電量按鈕
        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(8)
        row1.addWidget(self.status_dot)
        row1.addWidget(self.status_text)
        row1.addWidget(self.memory_text)
        row1.addStretch(1)
        row1.addWidget(self.btn_info)
        row1.addWidget(self.btn_battery)

        # Row 2: 裝置下拉 + 掃描控制
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(6)
        row2.addWidget(QLabel("裝置:"))
        row2.addWidget(self.device_combo, 1)
        row2.addWidget(self.scan_timeout_spin)
        row2.addWidget(self.btn_scan)
        row2.addWidget(self.btn_connect)
        row2.addWidget(self.btn_disconnect)

        lay.addLayout(row1)
        lay.addLayout(row2)
        return bar

    def _build_bottom_bar(self) -> QWidget:
        w = QFrame()
        w.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(4)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(110)
        self.log.setStyleSheet("QTextEdit{background:#1e1e1e;color:#ddd;font-family:Menlo,monospace;font-size:11px;}")

        lay.addWidget(self.progress)
        lay.addWidget(self.log)
        return w

    # ── 連線狀態 ──

    def _set_conn_status(self, connected: bool) -> None:
        if connected and self._connected_device:
            d = self._connected_device
            self.status_dot.setStyleSheet("color:#2ecc71;font-size:16px;")
            self.status_text.setText(f"已連線 {d.name}")
            self.btn_connect.setEnabled(False)
            self.btn_disconnect.setEnabled(True)
            self.btn_info.setEnabled(True)
            self.btn_battery.setEnabled(True)
            self.btn_scan.setEnabled(False)
            self.device_combo.setEnabled(False)
        else:
            self.status_dot.setStyleSheet("color:#d33;font-size:16px;")
            self.status_text.setText("未連線")
            self.btn_connect.setEnabled(bool(self._devices))
            self.btn_disconnect.setEnabled(False)
            self.btn_info.setEnabled(False)
            self.btn_battery.setEnabled(False)
            self.btn_scan.setEnabled(True)
            self.device_combo.setEnabled(True)

    def require_connection(self) -> bool:
        if not self._connected_device:
            QMessageBox.warning(self, "提示", "請先連線裝置")
            return False
        return True

    # ── Log / progress ──

    def log_msg(self, msg: str) -> None:
        self.log.append(msg)

    def set_progress(self, pct: int) -> None:
        self.progress.setValue(max(0, min(100, pct)))

    def check_upload_size(self, size_bytes: int, parent: QWidget) -> bool:
        """根據裝置回報的剩餘空間判斷是否警告使用者。
        回傳 True 表示允許繼續，False 表示使用者取消。"""
        if self._badge_memory_kb is None:
            return True  # 無裝置資訊就放行 (可能是初連線/查詢失敗)
        avail_bytes = self._badge_memory_kb * 1024
        size_mb = size_bytes / (1024 * 1024)
        avail_mb = avail_bytes / (1024 * 1024)
        if size_bytes > avail_bytes:
            QMessageBox.critical(
                parent, "檔案超過容量",
                f"檔案 {size_mb:.2f} MB 超過裝置剩餘 {avail_mb:.2f} MB。\n\n"
                "請降低 fps / 截短時長 / 降品質後再試。",
            )
            return False
        if size_bytes > avail_bytes * 0.8:
            reply = QMessageBox.question(
                parent, "檔案偏大",
                f"檔案 {size_mb:.2f} MB / 剩餘 {avail_mb:.2f} MB（超過 80%）。\n"
                "大檔上傳可能不穩。仍要上傳嗎？",
                QMessageBox.Yes | QMessageBox.No,
            )
            return reply == QMessageBox.Yes
        return True

    def take_ble_device(self):
        """取得快取的 BLEDevice（持續重用，不清空）。
        bleak 的 BLEDevice 在掃描器存活期間都有效，直接傳給 BleakClient 最穩。
        `find_device_by_address` 重新掃描對停止廣播的裝置會失敗。"""
        if not self._connected_device:
            return None
        return self._connected_device.ble_device

    def make_progress_cb(self) -> Callable[[str], None]:
        """給 run_e87_upload(on_progress=) 用的 thread-safe callback."""
        def cb(msg: str) -> None:
            def apply():
                if "Progress:" in msg and "%" in msg:
                    try:
                        pct = int(msg.split("%")[0].split()[-1])
                        self.set_progress(pct)
                    except (ValueError, IndexError):
                        pass
                self.log_msg(msg)
            QTimer.singleShot(0, apply)
        return cb

    # ── Async helper ──

    def run_async(self, coro_factory: Callable, on_done: Callable | None = None) -> None:
        runner = AsyncRunner(self)
        self._runners = getattr(self, "_runners", [])
        self._runners.append(runner)  # keep ref
        self._last_runner = runner  # 給 log_via_signal 用

        def _done(result):
            self.set_progress(100)
            if on_done:
                on_done(result)
            QTimer.singleShot(400, lambda: self.set_progress(0))

        def _fail(exc):
            self.set_progress(0)
            self.log_msg(f"ERROR: {exc}")
            QMessageBox.critical(self, "錯誤", str(exc))

        runner.finished.connect(_done)
        runner.failed.connect(_fail)
        runner.progress.connect(self.log_msg)
        self.set_progress(0)
        runner.run(coro_factory)

    def log_via_signal(self, msg: str) -> None:
        """Thread-safe log emission: emits on the last runner's progress signal."""
        runner = getattr(self, "_last_runner", None)
        if runner is not None:
            runner.progress.emit(msg)

    # ── Nav ──

    def _on_nav_changed(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)

    # ── Scan ──

    def _on_scan(self) -> None:
        timeout = self.scan_timeout_spin.value()
        self.log_msg(f"掃描 E87 裝置 ({timeout}s)...")
        self.btn_scan.setEnabled(False)
        self.btn_connect.setEnabled(False)

        from ebadge_cli.ble_session import scan_devices

        async def task():
            # 只掃 E87 (名稱前綴匹配 "E87"、"E87-xxx")
            return await scan_devices(timeout=timeout, name="E87")

        def done(devices):
            self._devices = [
                DeviceInfo(d["name"], d["address"], d.get("rssi", "?"),
                           ble_device=d.get("device"))
                for d in devices if d.get("name")
            ]
            self.device_combo.clear()
            if not self._devices:
                self.log_msg("未找到 E87 裝置（請確認徽章已開機並在範圍內）")
                self.btn_scan.setEnabled(True)
                return
            for d in self._devices:
                # 縮短顯示：name + 最後 4 碼 address + RSSI
                tail = str(d.address)[-4:] if d.address else "????"
                self.device_combo.addItem(f"{d.name} …{tail}  RSSI:{d.rssi}")
            self.device_combo.setCurrentIndex(0)
            if len(self._devices) == 1:
                self.log_msg(f"找到 E87：{self._devices[0].name} RSSI:{self._devices[0].rssi}")
            else:
                self.log_msg(f"找到 {len(self._devices)} 個 E87（自動選訊號最強）")
                # 按 RSSI 排序，選訊號最強
                self._devices.sort(key=lambda d: (d.rssi if isinstance(d.rssi, int) else -999), reverse=True)
                self.device_combo.clear()
                for d in self._devices:
                    tail = str(d.address)[-4:] if d.address else "????"
                    self.device_combo.addItem(f"{d.name} …{tail}  RSSI:{d.rssi}")
                self.device_combo.setCurrentIndex(0)
            self.btn_scan.setEnabled(True)
            self.btn_connect.setEnabled(True)
            # 掃描到就直接連線
            self._on_connect()

        self.run_async(task, done)

    def _on_connect(self) -> None:
        idx = self.device_combo.currentIndex()
        if idx < 0 or idx >= len(self._devices):
            QMessageBox.warning(self, "提示", "請先掃描並選擇裝置")
            return
        self._connected_device = self._devices[idx]
        self.log_msg(f"已選擇裝置: {self._connected_device.name} ({self._connected_device.address})")
        self._set_conn_status(True)
        # 連線後自動依序抓資訊 + 電量
        self._auto_fetch_status()

    def _query_badge_info(self, on_complete: Optional[Callable] = None) -> None:
        if not self._connected_device:
            if on_complete: on_complete()
            return
        from ebadge_cli.badge_info import parse_badge_info
        from ebadge_cli.ble_constants import (
            CHAR_NOTIFY_C2E6, CHAR_WRITE_C2E6, SERVICE_C2E6,
        )
        from ebadge_cli.badge_frame import parse_frame
        from ebadge_cli.ble_session import BleMode, run_session
        from ebadge_cli.cli import _build_command_frame

        frame = _build_command_frame(cmd=0xC6, payload=[0x01])

        mode = BleMode(SERVICE_C2E6, CHAR_WRITE_C2E6, CHAR_NOTIFY_C2E6)
        addr = self._connected_device.address
        ble_device = self.take_ble_device()
        self.memory_text.setText("查詢中…")

        async def task():
            response: list[int] | None = None

            def on_notify(data: bytearray) -> None:
                nonlocal response
                response = list(data)

            for _ in range(3):
                response = None
                await run_session(mode, None, addr, 5.0, frame, on_notify,
                                   device=ble_device)
                if response is not None:
                    parsed = parse_frame(response)
                    if parsed and parsed.get("cmd") == 0xC7:
                        info = parse_badge_info(parsed.get("payload", []))
                        if info:
                            return info
            return None

        def done(info):
            if info is None:
                self.memory_text.setText("")
                self.log_msg("無法取得裝置資訊（可能需要 auth）")
            else:
                self._badge_memory_kb = info.memory
                self.memory_text.setText(
                    f"剩餘 {info.memory_mb:.1f} MB  /  {info.width}×{info.height}"
                )
                self.log_msg(
                    f"裝置資訊: {info.width}×{info.height}, 剩餘 {info.memory_mb:.2f} MB"
                )
            if on_complete:
                on_complete()

        self.run_async(task, done)

    def _on_battery(self) -> None:
        if not self.require_connection():
            return
        from ebadge_cli.rcsp_probe import probe_battery
        ble_device = self.take_ble_device()
        addr = self._connected_device.address
        log = self.log_via_signal

        async def task():
            return await probe_battery(device=ble_device, address=addr, on_log=log)

        def done(pct):
            if pct is None:
                self.log_msg("電量讀取失敗")
            else:
                self.log_msg(f"電量: {pct}%")
                self._update_header_status(battery_pct=pct)

        self.run_async(task, done)

    def _update_header_status(self, *, battery_pct: Optional[int] = None) -> None:
        """更新頂部 device bar 狀態行。"""
        if not self._connected_device:
            return
        parts = [f"已連線 {self._connected_device.name}"]
        if battery_pct is not None:
            parts.append(f"電量 {battery_pct}%")
            self._last_battery_pct = battery_pct
        elif getattr(self, "_last_battery_pct", None) is not None:
            parts.append(f"電量 {self._last_battery_pct}%")
        self.status_text.setText(" — ".join(parts))

    def _auto_fetch_status(self) -> None:
        """連線後自動依序抓裝置資訊 + 電量。"""
        self.log_msg("自動查詢裝置資訊中…")
        # 先查資訊（快且輕量），完成後再抓電量
        self._query_badge_info(on_complete=self._auto_fetch_battery)

    def _auto_fetch_battery(self) -> None:
        self.log_msg("自動查詢電量中…")
        self._on_battery()

    def _on_disconnect(self) -> None:
        if self._connected_device:
            self.log_msg(f"已斷線: {self._connected_device.name}")
        self._connected_device = None
        self._badge_memory_kb = None
        self.memory_text.setText("")
        self._set_conn_status(False)

def main() -> None:
    app = QApplication([])
    # 基本樣式微調
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
