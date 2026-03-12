"""
main_window.py  —  Nova Player  (full media player edition)
═══════════════════════════════════════════════════════════
Layout:
 ┌──────────────────────────────────────────────────────┐
 │  Menu bar                                            │
 ├──────────────────────────────────────────────────────┤
 │  Toolbar  (Open · Model · Language · Quality)        │
 ├────────────────────────────────┬─────────────────────┤
 │                                │  Playlist panel     │
 │   VlcWidget  +  SubtitleOverlay│  (toggle F9)        │
 │                                │                     │
 ├────────────────────────────────┴─────────────────────┤
 │  Chapter / progress bar  (coloured chunk markers)    │
 ├──────────────────────────────────────────────────────┤
 │  ⏮ ⏪ ⏯ ⏩ ⏭  ──────  🔊━━━  ⏱ 00:00/00:00  ⛶ ┤
 ├──────────────────────────────────────────────────────┤
 │  Subtitle generation progress bar + status           │
 └──────────────────────────────────────────────────────┘

Keyboard shortcuts:
  Space        play / pause
  Left / Right seek ±10s
  Up / Down    volume ±5
  F / F11      fullscreen toggle
  Escape       exit fullscreen
  O            open file
  N / P        next / previous in playlist
  M            mute toggle
  S            toggle subtitle overlay
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QSlider, QPushButton, QLabel, QFileDialog, QSizePolicy,
    QStatusBar, QComboBox, QToolBar, QListWidget, QListWidgetItem,
    QProgressBar, QFrame, QSpacerItem,
)
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSlot, QSize, QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import (
    QAction, QKeySequence, QIcon, QPainter, QColor, QPen,
    QLinearGradient, QFont,
)

from nova_player.player.vlc_widget          import VlcWidget
from nova_player.subtitle.dsrt_file         import DsrtFile, ChunkStatus
from nova_player.subtitle.subtitle_overlay  import SubtitleOverlay, SubtitleSync
from nova_player.ai.lookahead_scheduler     import LookaheadScheduler
from nova_player.ai.audio_extractor         import AudioExtractor


# ── Chunk-aware seek bar ──────────────────────────────────────────────────────
class ChapterBar(QWidget):
    """
    Custom seek/progress bar that paints coloured chunk-status markers
    on top of the standard slider track.

    Colours:
      ✓ COMPLETE   → green  #4caf50
      ⚙ RUNNING    → amber  #ff9800
      ✗ FAILED     → red    #f44336
      ○ PENDING    → grey   #37474f
    """

    CHUNK_COLORS = {
        ChunkStatus.COMPLETE:     QColor("#4caf50"),
        ChunkStatus.TRANSCRIBING: QColor("#ff9800"),
        ChunkStatus.EXTRACTING:   QColor("#ff9800"),
        ChunkStatus.FAILED:       QColor("#f44336"),
        ChunkStatus.PENDING:      QColor("#37474f"),
    }

    seeked = __import__("PyQt6.QtCore", fromlist=["pyqtSignal"]).pyqtSignal(int)  # ms

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._total_ms  = 0
        self._current_ms= 0
        self._dsrt: Optional[DsrtFile] = None

    def set_total(self, ms: int):   self._total_ms   = ms;  self.update()
    def set_current(self, ms: int): self._current_ms = ms;  self.update()
    def set_dsrt(self, d: DsrtFile):self._dsrt       = d;   self.update()

    def paintEvent(self, _):
        p  = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        track_y = H // 2 - 3
        track_h = 6

        # Background track
        p.setBrush(QColor("#263238")); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, track_y, W, track_h, 3, 3)

        if self._total_ms <= 0:
            return

        # Chunk status segments
        if self._dsrt:
            for chunk in self._dsrt._chunks:
                x1 = int(chunk.start_ms / self._total_ms * W)
                x2 = int(chunk.end_ms   / self._total_ms * W)
                color = self.CHUNK_COLORS.get(chunk.status, QColor("#37474f"))
                p.setBrush(color)
                p.drawRect(x1, track_y, max(1, x2 - x1 - 1), track_h)

        # Played region overlay
        ratio = min(1.0, self._current_ms / self._total_ms)
        p.setBrush(QColor("#e94560"))
        p.setOpacity(0.6)
        p.drawRoundedRect(0, track_y, int(ratio * W), track_h, 3, 3)
        p.setOpacity(1.0)

        # Playhead handle
        hx = int(ratio * W)
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(QColor("#e94560"), 2))
        p.drawEllipse(hx - 7, H // 2 - 7, 14, 14)

    def mousePressEvent(self, e):
        if self._total_ms > 0:
            ratio = e.position().x() / self.width()
            self.seeked.emit(int(ratio * self._total_ms))

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton and self._total_ms > 0:
            ratio = max(0, min(1, e.position().x() / self.width()))
            self.seeked.emit(int(ratio * self._total_ms))


# ── On-Screen Display (OSD) ───────────────────────────────────────────────────
class OSD(QLabel):
    """Fades in/out a short message over the video (volume, seek, etc.)"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("""
            background: rgba(0,0,0,160);
            color: white;
            font-size: 22px;
            font-weight: bold;
            border-radius: 10px;
            padding: 10px 20px;
        """)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_msg(self, text: str, ms: int = 1500):
        self.setText(text)
        self.adjustSize()
        # Centre in parent
        pw = self.parent().width()
        ph = self.parent().height()
        self.move((pw - self.width()) // 2, ph // 3)
        self.show(); self.raise_()
        self._timer.start(ms)


# ── Subtitle generation progress panel ───────────────────────────────────────
class SubtitleProgressPanel(QWidget):
    """
    Compact panel shown below controls:
      [████░░░░░░]  12/20 chunks  |  243 cues  |  Chunk 5 ready — 18 cues
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)

        self._bar = QProgressBar()
        self._bar.setFixedHeight(12)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet("""
            QProgressBar { background:#1a1a2e; border-radius:6px; }
            QProgressBar::chunk { background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #4caf50, stop:1 #8bc34a); border-radius:6px; }
        """)

        self._lbl = QLabel("Subtitles: waiting for media…")
        self._lbl.setStyleSheet("color:#a0a0b0; font-size:11px;")

        layout.addWidget(self._bar, 0)
        layout.addWidget(self._lbl, 1)

    def update_progress(self, done: int, total: int,
                         cues: int, msg: str = ""):
        self._bar.setMaximum(max(1, total))
        self._bar.setValue(done)
        status = f"{done}/{total} chunks  |  {cues} cues"
        if msg:
            status += f"  |  {msg}"
        self._lbl.setText(status)

    def set_complete(self, total_cues: int):
        self._bar.setValue(self._bar.maximum())
        self._lbl.setText(
            f"✓ Subtitles complete  —  {total_cues} cues"
        )
        self._lbl.setStyleSheet("color:#4caf50; font-size:11px; font-weight:bold;")


# ── Playlist panel ────────────────────────────────────────────────────────────
class PlaylistPanel(QWidget):
    play_item = __import__("PyQt6.QtCore", fromlist=["pyqtSignal"]).pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(200)
        self.setMaximumWidth(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        hdr = QLabel("📋  Playlist")
        hdr.setStyleSheet("font-weight:bold; font-size:13px; color:#e94560;")
        layout.addWidget(hdr)

        self._list = QListWidget()
        self._list.setStyleSheet("""
            QListWidget {
                background:#0f0f1a; border:1px solid #0f3460;
                border-radius:4px; color:#e0e0e0;
            }
            QListWidget::item:selected { background:#e94560; color:#fff; }
            QListWidget::item:hover    { background:#1a1a3e; }
        """)
        self._list.itemDoubleClicked.connect(
            lambda item: self.play_item.emit(item.data(Qt.ItemDataRole.UserRole))
        )
        layout.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("+ Add")
        btn_clr = QPushButton("✕ Clear")
        for b in (btn_add, btn_clr):
            b.setFixedHeight(26)
        btn_add.clicked.connect(self._on_add)
        btn_clr.clicked.connect(self._list.clear)
        btn_row.addWidget(btn_add); btn_row.addWidget(btn_clr)
        layout.addLayout(btn_row)

        self._paths: list[str] = []

    def add_file(self, path: str):
        name = Path(path).name
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, path)
        self._list.addItem(item)
        self._paths.append(path)

    def current_index(self) -> int:
        return self._list.currentRow()

    def set_current_by_path(self, path: str):
        for i, p in enumerate(self._paths):
            if p == path:
                self._list.setCurrentRow(i)
                return

    def next_path(self) -> Optional[str]:
        row = self._list.currentRow()
        if row + 1 < self._list.count():
            self._list.setCurrentRow(row + 1)
            return self._paths[row + 1]
        return None

    def prev_path(self) -> Optional[str]:
        row = self._list.currentRow()
        if row > 0:
            self._list.setCurrentRow(row - 1)
            return self._paths[row - 1]
        return None

    def _on_add(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add to Playlist", "",
            "Media Files (*.mp4 *.mkv *.avi *.mov *.wmv *.flv "
            "*.webm *.m4v *.mp3 *.aac *.wav *.flac *.ogg);;"
            "All Files (*)"
        )
        for p in paths:
            self.add_file(p)


# ── Main Window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):

    # ── Construction ────────────────────────────────────────────────────────

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎬 Nova Player")
        self.resize(1200, 720)

        self._dsrt:       Optional[DsrtFile]           = None
        self._scheduler:  Optional[LookaheadScheduler] = None
        self._total_ms    = 0
        self._muted       = False
        self._subs_visible= True
        self._fullscreen  = False
        self._vol_before_mute = 80
        self._current_path: Optional[str] = None

        self._build_toolbar()
        self._build_central()
        self._build_menu()
        self._connect_signals()

        # Auto-hide controls timer in fullscreen
        self._hide_controls_timer = QTimer(self)
        self._hide_controls_timer.setSingleShot(True)
        self._hide_controls_timer.setInterval(3000)
        self._hide_controls_timer.timeout.connect(self._maybe_hide_controls)

    # ── Toolbar ──────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        tb = QToolBar("Controls")
        tb.setMovable(False)
        tb.setObjectName("mainToolbar")
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)

        tb.addAction(self._act("📂 Open",  "Ctrl+O", self._on_open))
        tb.addSeparator()

        tb.addWidget(QLabel(" Model: "))
        self._combo_model = QComboBox()
        for m in ["tiny", "base", "small", "medium", "large-v3"]:
            self._combo_model.addItem(m)
        self._combo_model.setCurrentText("small")
        self._combo_model.setFixedWidth(100)
        self._combo_model.setToolTip("Whisper model — small is best CPU balance")
        tb.addWidget(self._combo_model)

        tb.addWidget(QLabel(" Quality: "))
        self._combo_quality = QComboBox()
        for q in ["INSTANT", "FAST", "BALANCED", "BEST"]:
            self._combo_quality.addItem(q)
        self._combo_quality.setCurrentText("BALANCED")
        self._combo_quality.setFixedWidth(100)
        tb.addWidget(self._combo_quality)

        tb.addWidget(QLabel(" Lang: "))
        self._combo_lang = QComboBox()
        langs = [("Auto", None), ("English", "en"), ("Spanish", "es"),
                 ("French", "fr"), ("German", "de"), ("Hindi", "hi"),
                 ("Tamil", "ta"), ("Japanese", "ja"), ("Chinese", "zh"),
                 ("Arabic", "ar"), ("Portuguese", "pt"), ("Russian", "ru")]
        for display, code in langs:
            self._combo_lang.addItem(display, code)
        self._combo_lang.setFixedWidth(90)
        tb.addWidget(self._combo_lang)

        tb.addSeparator()
        self._act_subs = self._act("💬 Subs ON", "S", self._toggle_subs)
        tb.addAction(self._act_subs)
        tb.addAction(self._act("📤 Export SRT", None, self._on_export_srt))
        tb.addSeparator()
        tb.addAction(self._act("⛶ Fullscreen", "F11", self._toggle_fullscreen))
        tb.addAction(self._act("📋 Playlist",  "F9",  self._toggle_playlist))

    # ── Central widget ───────────────────────────────────────────────────────

    def _build_central(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Horizontal splitter: video | playlist
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: video + OSD
        video_container = QWidget()
        video_container.setObjectName("videoContainer")
        vc_layout = QVBoxLayout(video_container)
        vc_layout.setContentsMargins(0, 0, 0, 0)

        self._video   = VlcWidget(video_container)
        self._overlay = SubtitleOverlay(self._video)
        self._osd     = OSD(self._video)
        self._sync    = SubtitleSync(self._video, self._overlay)
        vc_layout.addWidget(self._video)

        # Right: playlist
        self._playlist = PlaylistPanel()
        self._playlist.play_item.connect(self.open_file)

        self._splitter.addWidget(video_container)
        self._splitter.addWidget(self._playlist)
        self._splitter.setStretchFactor(0, 4)
        self._splitter.setStretchFactor(1, 1)
        root.addWidget(self._splitter, stretch=1)

        # Chapter / progress bar
        self._chapter_bar = ChapterBar()
        self._chapter_bar.seeked.connect(self._video.seek)
        root.addWidget(self._chapter_bar)

        # Controls row
        self._ctrl_widget = self._build_controls()
        root.addWidget(self._ctrl_widget)

        # Subtitle progress panel
        self._sub_panel = SubtitleProgressPanel()
        root.addWidget(self._sub_panel)

    def _build_controls(self) -> QWidget:
        w = QWidget()
        w.setObjectName("controlsBar")
        w.setFixedHeight(56)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(6)

        def btn(label: str, tip: str, cb) -> QPushButton:
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedSize(44, 36)
            b.clicked.connect(cb)
            return b

        self._btn_prev   = btn("⏮", "Previous (P)",    self._on_prev)
        self._btn_back   = btn("⏪", "Rewind 10s (←)",  self._on_rewind)
        self._btn_play   = btn("▶", "Play (Space)",     self._on_play_pause)
        self._btn_fwd    = btn("⏩", "Forward 10s (→)", self._on_forward)
        self._btn_next   = btn("⏭", "Next (N)",        self._on_next)

        self._btn_play.setFixedSize(52, 36)

        # Volume
        self._btn_mute = btn("🔊", "Mute (M)", self._toggle_mute)
        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(80)
        self._vol_slider.setFixedWidth(90)
        self._vol_slider.setToolTip("Volume")
        self._video.set_volume(80)

        # Time label
        self._lbl_time = QLabel("00:00:00 / 00:00:00")
        self._lbl_time.setStyleSheet("font-family: monospace; font-size: 12px; color:#b0b0c0;")
        self._lbl_time.setFixedWidth(160)

        # Fullscreen button (right side)
        self._btn_fs = btn("⛶", "Fullscreen (F11)", self._toggle_fullscreen)

        for b in (self._btn_prev, self._btn_back, self._btn_play,
                  self._btn_fwd, self._btn_next):
            layout.addWidget(b)

        layout.addStretch()
        layout.addWidget(self._lbl_time)
        layout.addStretch()
        layout.addWidget(self._btn_mute)
        layout.addWidget(self._vol_slider)
        layout.addWidget(self._btn_fs)

        return w

    # ── Menu bar ─────────────────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        # File
        fm = mb.addMenu("&File")
        fm.addAction(self._act("Open File…",    "Ctrl+O", self._on_open))
        fm.addAction(self._act("Add to Playlist…", None, self._playlist._on_add))
        fm.addSeparator()
        fm.addAction(self._act("Export SRT…",   None,    self._on_export_srt))
        fm.addSeparator()
        fm.addAction(self._act("Quit",          "Ctrl+Q", self.close))

        # Playback
        pm = mb.addMenu("&Playback")
        pm.addAction(self._act("Play / Pause",  "Space",  self._on_play_pause))
        pm.addAction(self._act("Stop",          "Ctrl+S", self._on_stop))
        pm.addSeparator()
        pm.addAction(self._act("Seek +10s",     "Right",  self._on_forward))
        pm.addAction(self._act("Seek −10s",     "Left",   self._on_rewind))
        pm.addAction(self._act("Seek +60s",     "Ctrl+Right", lambda: self._seek_relative(60_000)))
        pm.addAction(self._act("Seek −60s",     "Ctrl+Left",  lambda: self._seek_relative(-60_000)))
        pm.addSeparator()
        pm.addAction(self._act("Next",          "N",      self._on_next))
        pm.addAction(self._act("Previous",      "P",      self._on_prev))

        # Audio
        am = mb.addMenu("&Audio")
        am.addAction(self._act("Volume +5",     "Up",     self._vol_up))
        am.addAction(self._act("Volume −5",     "Down",   self._vol_down))
        am.addAction(self._act("Mute",          "M",      self._toggle_mute))

        # Subtitles
        sm = mb.addMenu("&Subtitles")
        sm.addAction(self._act("Toggle Subtitles", "S",   self._toggle_subs))
        sm.addAction(self._act("Clear .dsrt Cache",None,  self._on_clear_cache))
        sm.addAction(self._act("Export SRT…",   None,     self._on_export_srt))

        # View
        vm = mb.addMenu("&View")
        vm.addAction(self._act("Fullscreen",    "F11",    self._toggle_fullscreen))
        vm.addAction(self._act("Toggle Playlist","F9",   self._toggle_playlist))

    # ── Signal connections ───────────────────────────────────────────────────

    def _connect_signals(self):
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        self._video.time_changed.connect(self._on_time_changed)
        self._video.length_changed.connect(self._on_length_changed)
        self._video.end_reached.connect(self._on_end_reached)
        self._video.media_opened.connect(self._on_media_opened)
        self._video.error.connect(lambda m: self._osd.show_msg(f"⚠ {m}", 3000))

    # ── Public API ───────────────────────────────────────────────────────────

    def open_file(self, path: str):
        self._stop_scheduler()
        self._sync.stop()
        self._current_path = path
        self.setWindowTitle(f"🎬 Nova Player — {Path(path).name}")
        self._btn_play.setText("⏸")
        self._video.load(path)
        self._playlist.add_file(path)
        self._playlist.set_current_by_path(path)

    # ── Playback control slots ───────────────────────────────────────────────

    @pyqtSlot()
    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Media File", "",
            "Media Files (*.mp4 *.mkv *.avi *.mov *.wmv *.flv "
            "*.webm *.m4v *.mp3 *.aac *.wav *.flac *.ogg);;"
            "All Files (*)"
        )
        if path:
            self.open_file(path)

    @pyqtSlot()
    def _on_play_pause(self):
        if self._video.is_playing():
            self._video.pause()
            self._btn_play.setText("▶")
            self._osd.show_msg("⏸  Paused")
        else:
            self._video.play()
            self._btn_play.setText("⏸")
            self._osd.show_msg("▶  Playing")

    @pyqtSlot()
    def _on_stop(self):
        self._stop_scheduler()
        self._sync.stop()
        self._video.stop()
        self._btn_play.setText("▶")
        self._chapter_bar.set_current(0)

    @pyqtSlot()
    def _on_rewind(self):  self._seek_relative(-10_000)

    @pyqtSlot()
    def _on_forward(self): self._seek_relative( 10_000)

    def _seek_relative(self, delta_ms: int):
        t = self._video.get_time() + delta_ms
        t = max(0, min(t, self._total_ms))
        self._video.seek(t)
        arrow = "⏪" if delta_ms < 0 else "⏩"
        self._osd.show_msg(f"{arrow}  {abs(delta_ms)//1000}s")

    @pyqtSlot()
    def _on_next(self):
        p = self._playlist.next_path()
        if p: self.open_file(p)

    @pyqtSlot()
    def _on_prev(self):
        p = self._playlist.prev_path()
        if p: self.open_file(p)

    # ── Volume ───────────────────────────────────────────────────────────────

    @pyqtSlot(int)
    def _on_volume_changed(self, v: int):
        self._video.set_volume(v)
        icon = "🔇" if v == 0 else ("🔉" if v < 50 else "🔊")
        self._btn_mute.setText(icon)

    def _vol_up(self):
        v = min(100, self._vol_slider.value() + 5)
        self._vol_slider.setValue(v)
        self._osd.show_msg(f"🔊  {v}%")

    def _vol_down(self):
        v = max(0, self._vol_slider.value() - 5)
        self._vol_slider.setValue(v)
        self._osd.show_msg(f"🔊  {v}%")

    def _toggle_mute(self):
        if self._muted:
            self._vol_slider.setValue(self._vol_before_mute)
            self._muted = False
        else:
            self._vol_before_mute = self._vol_slider.value()
            self._vol_slider.setValue(0)
            self._muted = True
        self._osd.show_msg("🔇  Muted" if self._muted else "🔊  Unmuted")

    # ── Subtitle controls ────────────────────────────────────────────────────

    def _toggle_subs(self):
        self._subs_visible = not self._subs_visible
        if self._subs_visible:
            self._sync.start()
            self._act_subs.setText("💬 Subs ON")
            self._osd.show_msg("💬 Subtitles ON")
        else:
            self._sync.stop()
            self._act_subs.setText("💬 Subs OFF")
            self._osd.show_msg("💬 Subtitles OFF")

    # ── Fullscreen ───────────────────────────────────────────────────────────

    def _toggle_fullscreen(self):
        if self._fullscreen:
            self.showNormal()
            self._ctrl_widget.show()
            self._sub_panel.show()
            self.menuBar().show()
            self.findChild(QToolBar, "mainToolbar").show()
            self._fullscreen = False
            self._osd.show_msg("Exit Fullscreen")
        else:
            self.showFullScreen()
            self._ctrl_widget.hide()
            self._sub_panel.hide()
            self.menuBar().hide()
            self.findChild(QToolBar, "mainToolbar").hide()
            self._fullscreen = True
            self._osd.show_msg("⛶  Fullscreen — Esc to exit")
            self._hide_controls_timer.start()

    def _maybe_hide_controls(self):
        pass  # controls already hidden in fullscreen

    # ── Playlist ─────────────────────────────────────────────────────────────

    def _toggle_playlist(self):
        visible = self._playlist.isVisible()
        self._playlist.setVisible(not visible)

    # ── Media / subtitle wiring ───────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_media_opened(self, path: str):
        self._sub_panel._lbl.setStyleSheet("color:#a0a0b0; font-size:11px;")
        QTimer.singleShot(800, lambda: self._start_subtitle_generation(path))

    def _start_subtitle_generation(self, media_path: str):
        total_ms = self._video.get_length()
        if total_ms <= 0:
            QTimer.singleShot(800, lambda: self._start_subtitle_generation(media_path))
            return

        self._total_ms = total_ms
        self._chapter_bar.set_total(total_ms)

        dsrt_path = Path(media_path).with_suffix(".dsrt")
        if dsrt_path.exists():
            try:
                self._dsrt = DsrtFile.load(str(dsrt_path))
                n = self._dsrt.completed_chunks()
                self._sub_panel.update_progress(
                    n, self._dsrt.total_chunks(),
                    self._dsrt.total_cues(),
                    f"Loaded cache — {n}/{self._dsrt.total_chunks()} chunks"
                )
            except Exception as e:
                self._dsrt = DsrtFile.create(media_path, total_ms)
                self._sub_panel.update_progress(0, self._dsrt.total_chunks(), 0,
                                                f"Corrupt cache, restarting")
        else:
            self._dsrt = DsrtFile.create(media_path, total_ms)
            self._sub_panel.update_progress(0, self._dsrt.total_chunks(), 0,
                                            "Starting generation…")

        self._chapter_bar.set_dsrt(self._dsrt)
        self._sync.set_dsrt(self._dsrt)
        if self._subs_visible:
            self._sync.start()

        model   = self._combo_model.currentText()
        quality = self._combo_quality.currentText()
        lang    = self._combo_lang.currentData()

        self._scheduler = LookaheadScheduler(
            player=self._video,
            dsrt=self._dsrt,
            media_path=media_path,
            model_size=model,
            language=lang,
            quality=quality,
        )
        self._scheduler.chunk_ready.connect(self._on_chunk_ready)
        self._scheduler.status_update.connect(self._on_scheduler_status)
        self._scheduler.generation_complete.connect(self._on_generation_complete)
        self._scheduler.start()

    @pyqtSlot(int, list)
    def _on_chunk_ready(self, chunk_idx: int, segments: list):
        if not self._dsrt:
            return
        done  = self._dsrt.completed_chunks()
        total = self._dsrt.total_chunks()
        cues  = self._dsrt.total_cues()
        self._sub_panel.update_progress(
            done, total, cues,
            f"Chunk {chunk_idx+1} ready — {len(segments)} cues"
        )
        self._chapter_bar.update()  # repaint chunk colours

    @pyqtSlot(str)
    def _on_scheduler_status(self, msg: str):
        if self._dsrt:
            self._sub_panel.update_progress(
                self._dsrt.completed_chunks(),
                self._dsrt.total_chunks(),
                self._dsrt.total_cues(),
                msg,
            )

    @pyqtSlot()
    def _on_generation_complete(self):
        if self._dsrt:
            self._sub_panel.set_complete(self._dsrt.total_cues())
            self._chapter_bar.update()

    @pyqtSlot(int)
    def _on_time_changed(self, ms: int):
        self._chapter_bar.set_current(ms)
        self._lbl_time.setText(
            f"{self._ms_to_str(ms)} / {self._ms_to_str(self._total_ms)}"
        )

    @pyqtSlot(int)
    def _on_length_changed(self, ms: int):
        self._total_ms = ms
        self._chapter_bar.set_total(ms)

    @pyqtSlot()
    def _on_end_reached(self):
        self._btn_play.setText("▶")
        self._osd.show_msg("⏹  Ended")
        # Auto-advance playlist
        QTimer.singleShot(1000, self._on_next)

    # ── Export / cache ───────────────────────────────────────────────────────

    @pyqtSlot()
    def _on_export_srt(self):
        if not self._dsrt or self._dsrt.total_cues() == 0:
            self._osd.show_msg("No subtitles yet", 2000)
            return
        default = ""
        if self._current_path:
            default = str(Path(self._current_path).with_suffix(".srt"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Export SRT", default, "SubRip (*.srt)"
        )
        if path:
            self._export_srt(path)
            self._osd.show_msg(f"✓ Exported SRT", 2000)

    @pyqtSlot()
    def _on_clear_cache(self):
        if self._dsrt and self._dsrt.path and self._dsrt.path.exists():
            self._dsrt.path.unlink()
            self._osd.show_msg("🗑  Cache cleared")

    # ── Keyboard shortcuts ───────────────────────────────────────────────────

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Space:
            self._on_play_pause()
        elif key == Qt.Key.Key_Left:
            self._on_rewind()
        elif key == Qt.Key.Key_Right:
            self._on_forward()
        elif key == Qt.Key.Key_Up:
            self._vol_up()
        elif key == Qt.Key.Key_Down:
            self._vol_down()
        elif key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self._toggle_fullscreen()
        elif key == Qt.Key.Key_Escape:
            if self._fullscreen:
                self._toggle_fullscreen()
        elif key == Qt.Key.Key_O:
            self._on_open()
        elif key == Qt.Key.Key_M:
            self._toggle_mute()
        elif key == Qt.Key.Key_S:
            self._toggle_subs()
        elif key == Qt.Key.Key_N:
            self._on_next()
        elif key == Qt.Key.Key_P:
            self._on_prev()
        else:
            super().keyPressEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if self._fullscreen:
            self._hide_controls_timer.start()

    # ── Resize overlay ───────────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._overlay.isVisible():
            self._overlay.reposition(
                self._video.width(), self._video.height()
            )
        if self._osd.isVisible():
            pw = self._video.width()
            ph = self._video.height()
            self._osd.move(
                (pw - self._osd.width()) // 2,
                ph // 3,
            )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _stop_scheduler(self):
        if self._scheduler:
            self._scheduler.stop()
            self._scheduler = None

    def _export_srt(self, out_path: str):
        def _ts(ms: float) -> str:
            ms = int(ms)
            h, ms = divmod(ms, 3_600_000)
            m, ms = divmod(ms, 60_000)
            s, ms = divmod(ms, 1_000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
        with open(out_path, "w", encoding="utf-8") as f:
            with self._dsrt._lock:
                for idx, cue in enumerate(self._dsrt._cue_map.values(), 1):
                    f.write(f"{idx}\n{_ts(cue.start_ms)} --> {_ts(cue.end_ms)}\n{cue.text}\n\n")

    @staticmethod
    def _ms_to_str(ms: int) -> str:
        s    = max(0, ms) // 1000
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _act(label: str, shortcut, cb) -> QAction:
        a = QAction(label)
        if shortcut:
            a.setShortcut(shortcut)
        a.triggered.connect(cb)
        return a

    def closeEvent(self, event):
        self._stop_scheduler()
        self._sync.stop()
        self._video.stop()
        super().closeEvent(event)
