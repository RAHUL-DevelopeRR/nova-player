"""
subtitle_overlay.py
Transparent QLabel that floats over the VLC video widget.
Updated every 50ms by SubtitleSync.
"""

from PyQt6.QtWidgets import QLabel, QWidget, QSizePolicy
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QPalette
from nova_player.subtitle.dsrt_file import DsrtFile, DsrtCue


class SubtitleOverlay(QLabel):
    """
    Semi-transparent subtitle label rendered on top of the video.
    Font size, position and style configurable via set_style().
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom
        )
        self.setWordWrap(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet("""
            QLabel {
                color: #FFFFFF;
                background-color: rgba(0, 0, 0, 160);
                font-size: 20px;
                font-weight: bold;
                font-family: 'Segoe UI', Arial, sans-serif;
                padding: 6px 12px;
                border-radius: 4px;
            }
        """)
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        self.hide()

    def show_cue(self, cue: DsrtCue | None):
        if cue and cue.text.strip():
            self.setText(cue.text.strip())
            self.show()
        else:
            self.setText("")
            self.hide()

    def set_font_size(self, size: int):
        current = self.styleSheet()
        import re
        new = re.sub(r"font-size:\s*\d+px", f"font-size: {size}px", current)
        self.setStyleSheet(new)

    def reposition(self, parent_width: int, parent_height: int):
        """Stick label to bottom-center of the video widget."""
        margin    = 24
        max_width = int(parent_width * 0.85)
        self.setMaximumWidth(max_width)
        self.adjustSize()
        w = min(self.sizeHint().width() + 24, max_width)
        h = self.sizeHint().height()
        x = (parent_width - w) // 2
        y = parent_height - h - margin
        self.setGeometry(x, y, w, h)


class SubtitleSync:
    """
    Drives the SubtitleOverlay by polling player.get_time() every 50ms.
    Designed to run on the Qt main thread (QTimer-based).
    """

    def __init__(self, player, overlay: SubtitleOverlay):
        self._player  = player
        self._overlay = overlay
        self._dsrt: DsrtFile | None = None
        self._timer   = QTimer()
        self._timer.setInterval(50)   # 20fps subtitle update
        self._timer.timeout.connect(self._tick)

    def set_dsrt(self, dsrt: DsrtFile):
        self._dsrt = dsrt

    def start(self): self._timer.start()
    def stop(self):  self._timer.stop(); self._overlay.hide()

    def _tick(self):
        if not self._dsrt:
            return
        t   = self._player.get_time()
        cue = self._dsrt.get_active_cue(float(t))
        self._overlay.show_cue(cue)
