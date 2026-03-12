"""
vlc_widget.py
Embeds a LibVLC MediaPlayer inside a PyQt6 QFrame.
Handles cross-platform window handle injection (Win32 / X11 / macOS).
"""

import sys
import vlc
from PyQt6.QtWidgets import QFrame, QSizePolicy
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QPalette, QColor


class VlcWidget(QFrame):
    """
    A QFrame that owns a vlc.MediaPlayer.
    Signals:
        time_changed(int)     — current position in ms, fires every 200ms
        length_changed(int)   — total duration in ms, fires once per media
        end_reached()         — media playback finished
        playing()             — playback started / resumed
        paused()              — playback paused
        media_opened(str)     — new media path loaded
        error(str)            — VLC error message
    """

    time_changed   = pyqtSignal(int)
    length_changed = pyqtSignal(int)
    end_reached    = pyqtSignal()
    playing        = pyqtSignal()
    paused         = pyqtSignal()
    media_opened   = pyqtSignal(str)
    error          = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(640, 360)

        # Black background
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("black"))
        self.setPalette(palette)
        self.setAutoFillBackground(True)

        # VLC instance — disable built-in subtitles (we handle them ourselves)
        args = [
            "--no-xlib",
            "--no-video-title-show",
            "--no-sub-autodetect-file",
            "--quiet",
        ]
        self._instance = vlc.Instance(" ".join(args))
        self._player   = self._instance.media_player_new()

        # Poll timer — fires every 200ms to emit time_changed
        self._poll = QTimer(self)
        self._poll.setInterval(200)
        self._poll.timeout.connect(self._on_poll)

        self._media_path  = None
        self._length_ms   = 0

    # ── Public API ──────────────────────────────────────────────────────────

    def load(self, path: str):
        """Load a media file and start playback."""
        self._media_path = path
        media = self._instance.media_new(path)
        self._player.set_media(media)
        self._attach_window()
        self._player.play()
        self._poll.start()
        self.media_opened.emit(path)

    def play(self):  self._player.play()
    def pause(self): self._player.pause()

    def stop(self):
        self._player.stop()
        self._poll.stop()

    def seek(self, ms: int):
        """Seek to absolute position in milliseconds."""
        self._player.set_time(max(0, ms))

    def set_volume(self, pct: int):
        """Set volume 0–100."""
        self._player.audio_set_volume(max(0, min(100, pct)))

    def get_time(self)  -> int: return self._player.get_time()
    def get_length(self) -> int: return self._player.get_length()
    def is_playing(self) -> bool: return self._player.is_playing()

    def media_path(self) -> str | None:
        return self._media_path

    # ── Internal ────────────────────────────────────────────────────────────

    def _attach_window(self):
        """Inject native window handle into VLC."""
        wid = int(self.winId())
        if sys.platform == "win32":
            self._player.set_hwnd(wid)
        elif sys.platform == "darwin":
            self._player.set_nsobject(wid)
        else:
            self._player.set_xwindow(wid)

    def _on_poll(self):
        t = self._player.get_time()
        if t >= 0:
            self.time_changed.emit(t)

        length = self._player.get_length()
        if length > 0 and length != self._length_ms:
            self._length_ms = length
            self.length_changed.emit(length)

        state = self._player.get_state()
        if state == vlc.State.Ended:
            self._poll.stop()
            self.end_reached.emit()
        elif state == vlc.State.Error:
            self._poll.stop()
            self.error.emit("VLC playback error")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._attach_window()
