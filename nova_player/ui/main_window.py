"""
main_window.py
Nova Player — Main application window.
Layout:
  ┌─────────────────────────────────────────────┐
  │  Menu bar                                   │
  ├─────────────────────────────────────────────┤
  │                                             │
  │          VlcWidget (video + overlay)        │
  │                                             │
  ├─────────────────────────────────────────────┤
  │  Seek slider                                │
  ├──────────┬──────────────────────────────────┤
  │ Controls │ Status bar (generation progress) │
  └──────────┴──────────────────────────────────┘
"""

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSlider, QPushButton, QLabel, QFileDialog,
    QStackedWidget, QSizePolicy, QStatusBar,
    QComboBox, QSpinBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QAction, QIcon, QKeySequence

from nova_player.player.vlc_widget   import VlcWidget
from nova_player.subtitle.dsrt_file  import DsrtFile
from nova_player.subtitle.subtitle_overlay import SubtitleOverlay, SubtitleSync
from nova_player.ai.lookahead_scheduler    import LookaheadScheduler
from nova_player.ai.audio_extractor        import AudioExtractor


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nova Player")
        self.resize(1024, 640)

        self._dsrt:       DsrtFile | None           = None
        self._scheduler:  LookaheadScheduler | None = None
        self._seeking     = False   # True while user drags seek slider
        self._total_ms    = 0

        self._build_ui()
        self._build_menu()
        self._connect_signals()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Video area with subtitle overlay
        self._video = VlcWidget(self)
        self._overlay = SubtitleOverlay(self._video)
        self._sync    = SubtitleSync(self._video, self._overlay)

        root.addWidget(self._video, stretch=1)

        # Seek slider
        self._seek_slider = QSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 1000)
        self._seek_slider.setFixedHeight(18)
        self._seek_slider.setObjectName("seekSlider")
        root.addWidget(self._seek_slider)

        # Controls bar
        ctrl = QWidget()
        ctrl.setFixedHeight(52)
        ctrl.setObjectName("controlsBar")
        ctrl_layout = QHBoxLayout(ctrl)
        ctrl_layout.setContentsMargins(10, 4, 10, 4)

        self._btn_open   = QPushButton("📂 Open")
        self._btn_play   = QPushButton("▶ Play")
        self._btn_pause  = QPushButton("⏸ Pause")
        self._btn_stop   = QPushButton("⏹ Stop")
        self._lbl_time   = QLabel("00:00 / 00:00")
        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(80)
        self._vol_slider.setFixedWidth(90)
        self._lbl_vol    = QLabel("🔊")

        # Model selector
        self._combo_model = QComboBox()
        for m in ["tiny", "base", "small", "medium", "large-v3"]:
            self._combo_model.addItem(m)
        self._combo_model.setCurrentText("small")
        self._combo_model.setToolTip("Whisper model")

        for btn in (self._btn_open, self._btn_play,
                    self._btn_pause, self._btn_stop):
            btn.setFixedWidth(90)

        ctrl_layout.addWidget(self._btn_open)
        ctrl_layout.addWidget(self._btn_play)
        ctrl_layout.addWidget(self._btn_pause)
        ctrl_layout.addWidget(self._btn_stop)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self._lbl_time)
        ctrl_layout.addStretch()
        ctrl_layout.addWidget(self._lbl_vol)
        ctrl_layout.addWidget(self._vol_slider)
        ctrl_layout.addWidget(QLabel("Model:"))
        ctrl_layout.addWidget(self._combo_model)

        root.addWidget(ctrl)

        # Status bar
        self._status = QStatusBar()
        self._status.setObjectName("statusBar")
        self._status_label = QLabel("Ready")
        self._status.addWidget(self._status_label, 1)
        self.setStatusBar(self._status)

        # Resize overlay on window resize
        self._video.resizeEvent = self._on_video_resize

    def _build_menu(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("&File")
        act_open = QAction("&Open…", self)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self._on_open)
        file_menu.addAction(act_open)

        act_export = QAction("&Export SRT…", self)
        act_export.triggered.connect(self._on_export_srt)
        file_menu.addAction(act_export)
        file_menu.addSeparator()

        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        sub_menu = mb.addMenu("&Subtitles")
        act_clear = QAction("&Clear cache (.dsrt)", self)
        act_clear.triggered.connect(self._on_clear_cache)
        sub_menu.addAction(act_clear)

    def _connect_signals(self):
        self._btn_open.clicked.connect(self._on_open)
        self._btn_play.clicked.connect(self._video.play)
        self._btn_pause.clicked.connect(self._video.pause)
        self._btn_stop.clicked.connect(self._on_stop)

        self._seek_slider.sliderPressed.connect(
            lambda: setattr(self, "_seeking", True))
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        self._vol_slider.valueChanged.connect(self._video.set_volume)

        self._video.time_changed.connect(self._on_time_changed)
        self._video.length_changed.connect(self._on_length_changed)
        self._video.end_reached.connect(self._on_end_reached)
        self._video.media_opened.connect(self._on_media_opened)

    # ── Slots ──────────────────────────────────────────────────────────────

    @pyqtSlot()
    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Media File", "",
            "Media Files (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v "
            "*.mp3 *.aac *.wav *.flac *.ogg);;"
            "All Files (*)"
        )
        if path:
            self.open_file(path)

    def open_file(self, path: str):
        self._stop_scheduler()
        self._video.load(path)

    @pyqtSlot(str)
    def _on_media_opened(self, path: str):
        # Wait for VLC to report length, then start subtitle generation
        QTimer.singleShot(800, lambda: self._start_subtitle_generation(path))

    def _start_subtitle_generation(self, media_path: str):
        total_ms = self._video.get_length()
        if total_ms <= 0:
            # Retry once more
            QTimer.singleShot(1000,
                lambda: self._start_subtitle_generation(media_path))
            return

        self._total_ms = total_ms
        dsrt_path = Path(media_path).with_suffix(".dsrt")

        # Load existing .dsrt or create fresh
        if dsrt_path.exists():
            try:
                self._dsrt = DsrtFile.load(str(dsrt_path))
                n_cached = self._dsrt.completed_chunks()
                self._set_status(
                    f"Loaded .dsrt cache — {n_cached}/{self._dsrt.total_chunks()} "
                    f"chunks, {self._dsrt.total_cues()} cues"
                )
            except Exception as e:
                self._dsrt = DsrtFile.create(media_path, total_ms)
                self._set_status(f"Corrupt .dsrt, starting fresh: {e}")
        else:
            self._dsrt = DsrtFile.create(media_path, total_ms)
            self._set_status("New media — starting subtitle generation…")

        # Wire subtitle sync
        self._sync.set_dsrt(self._dsrt)
        self._sync.start()

        # Start lookahead scheduler
        model = self._combo_model.currentText()
        self._scheduler = LookaheadScheduler(
            player=self._video,
            dsrt=self._dsrt,
            media_path=media_path,
            model_size=model,
        )
        self._scheduler.chunk_ready.connect(self._on_chunk_ready)
        self._scheduler.status_update.connect(self._set_status)
        self._scheduler.generation_complete.connect(
            lambda: self._set_status(
                f"✓ Subtitles complete — {self._dsrt.total_cues()} cues"
            )
        )
        self._scheduler.start()

    @pyqtSlot(int, list)
    def _on_chunk_ready(self, chunk_idx: int, segments: list):
        # Subtitle overlay updates automatically via SubtitleSync timer
        # Just update status
        total = self._dsrt.total_chunks() if self._dsrt else "?"
        done  = self._dsrt.completed_chunks() if self._dsrt else 0
        self._set_status(
            f"Chunk {chunk_idx+1}/{total} ready — "
            f"{done}/{total} chunks done  |"
            f"  {self._dsrt.total_cues() if self._dsrt else 0} cues total"
        )

    @pyqtSlot(int)
    def _on_time_changed(self, ms: int):
        if not self._seeking:
            ratio = ms / max(1, self._total_ms)
            self._seek_slider.blockSignals(True)
            self._seek_slider.setValue(int(ratio * 1000))
            self._seek_slider.blockSignals(False)
        self._lbl_time.setText(
            f"{self._ms_to_str(ms)} / {self._ms_to_str(self._total_ms)}"
        )

    @pyqtSlot(int)
    def _on_length_changed(self, ms: int):
        self._total_ms = ms

    @pyqtSlot()
    def _on_seek_released(self):
        self._seeking = False
        ratio = self._seek_slider.value() / 1000
        target_ms = int(ratio * self._total_ms)
        self._video.seek(target_ms)

    @pyqtSlot()
    def _on_stop(self):
        self._stop_scheduler()
        self._sync.stop()
        self._video.stop()

    @pyqtSlot()
    def _on_end_reached(self):
        self._set_status("Playback ended.")

    @pyqtSlot()
    def _on_export_srt(self):
        if not self._dsrt or self._dsrt.total_cues() == 0:
            self._set_status("No subtitles to export yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export SRT", "", "SubRip (*.srt)"
        )
        if path:
            self._export_srt(path)
            self._set_status(f"Exported SRT → {path}")

    @pyqtSlot()
    def _on_clear_cache(self):
        if self._dsrt and self._dsrt.path and self._dsrt.path.exists():
            self._dsrt.path.unlink()
            self._set_status("Cache cleared — will regenerate on next open.")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _stop_scheduler(self):
        if self._scheduler:
            self._scheduler.stop()
            self._scheduler = None

    def _set_status(self, msg: str):
        self._status_label.setText(msg)

    def _on_video_resize(self, event):
        VlcWidget.resizeEvent(self._video, event)
        if self._overlay.isVisible():
            self._overlay.reposition(
                self._video.width(), self._video.height()
            )

    def _export_srt(self, out_path: str):
        def ms_to_srt(ms: float) -> str:
            ms  = int(ms)
            h, ms = divmod(ms, 3_600_000)
            m, ms = divmod(ms,    60_000)
            s, ms = divmod(ms,     1_000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        with open(out_path, "w", encoding="utf-8") as f:
            with self._dsrt._lock:
                for idx, cue in enumerate(
                        self._dsrt._cue_map.values(), 1):
                    f.write(
                        f"{idx}\n"
                        f"{ms_to_srt(cue.start_ms)} --> "
                        f"{ms_to_srt(cue.end_ms)}\n"
                        f"{cue.text}\n\n"
                    )

    @staticmethod
    def _ms_to_str(ms: int) -> str:
        s   = ms // 1000
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def closeEvent(self, event):
        self._stop_scheduler()
        self._sync.stop()
        self._video.stop()
        super().closeEvent(event)
