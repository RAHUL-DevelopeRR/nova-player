"""
lookahead_scheduler.py
The playback-overhead subtitle generation brain.

Runs as a QThread, wakes every TICK_MS to:
  1. Read current playhead position from VLC
  2. Detect seeks (position jump > 2s vs expected)
  3. Keep LOOKAHEAD_CHUNKS chunks transcribed ahead of playhead
  4. On backward seek: do nothing (cues already in .dsrt)
  5. On forward seek past buffer: abort far jobs, fire micro-chunk
"""

from __future__ import annotations

import logging
import time
import threading
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from nova_player.ai.chunk_worker import ChunkWorker
from nova_player.subtitle.dsrt_file import DsrtFile

log = logging.getLogger("nova.scheduler")

CHUNK_MS        = 60_000   # must match DsrtFile.DEFAULT_CHUNK_MS
LOOKAHEAD       = 2        # chunks to keep ready ahead of playhead
TICK_MS         = 500      # scheduler wake interval
SEEK_THRESHOLD  = 2_000    # ms jump to classify as a user seek


class LookaheadScheduler(QThread):
    """
    Drives playback-overhead subtitle generation.

    Signals:
        chunk_ready(int, list)  — (chunk_idx, segments) — emitted from worker thread
        status_update(str)      — human-readable status bar message
        generation_complete()   — all chunks are done
    """

    chunk_ready        = pyqtSignal(int, list)
    status_update      = pyqtSignal(str)
    generation_complete= pyqtSignal()

    def __init__(
        self,
        player,
        dsrt:       DsrtFile,
        media_path: str,
        model_size: str  = "small",
        language:   Optional[str] = None,
        quality:    str  = "BALANCED",
    ):
        super().__init__()
        self._player     = player
        self._dsrt       = dsrt
        self._media_path = media_path
        self._model_size = model_size
        self._language   = language
        self._quality    = quality
        self._stop_flag  = threading.Event()
        self._active:    dict[int, ChunkWorker] = {}
        self._lock       = threading.Lock()
        self._full_wav:  Optional[str] = None

    def set_full_wav(self, path: str):
        """Provide pre-extracted full audio WAV for faster chunk slicing."""
        self._full_wav = path

    def stop(self):
        self._stop_flag.set()
        with self._lock:
            for w in self._active.values():
                w.abort = True
        self.wait(timeout=3000)

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self):
        prev_time_ms = 0
        prev_wall    = time.time()

        while not self._stop_flag.is_set():
            time.sleep(TICK_MS / 1000)

            now_ms   = self._player.get_time()
            now_wall = time.time()

            if now_ms < 0:
                prev_wall = now_wall
                continue

            # ── Seek detection ────────────────────────────────────────────
            elapsed_wall  = (now_wall - prev_wall) * 1000
            expected_ms   = prev_time_ms + elapsed_wall
            is_seek       = abs(now_ms - expected_ms) > SEEK_THRESHOLD

            if is_seek:
                log.info("Seek detected: %.0fms → %.0fms", prev_time_ms, now_ms)
                self._on_seek(now_ms)

            prev_time_ms = now_ms
            prev_wall    = now_wall

            # ── Lookahead scheduling ──────────────────────────────────────
            total_ms     = self._player.get_length()
            if total_ms <= 0:
                continue

            total_chunks = max(1, -(-total_ms // CHUNK_MS))
            cur_chunk    = now_ms // CHUNK_MS

            # Reap finished workers
            with self._lock:
                done = [i for i, w in self._active.items()
                        if not w.is_alive()]
                for i in done:
                    del self._active[i]

            # Schedule lookahead window
            for offset in range(LOOKAHEAD + 1):
                target = int(cur_chunk + offset)
                if target >= total_chunks:
                    break
                if self._dsrt.is_chunk_complete(target):
                    continue
                with self._lock:
                    if target in self._active:
                        continue
                    self._launch(target, micro=(offset == 0))

            # All done?
            if self._dsrt.completed_chunks() == total_chunks:
                log.info("All chunks complete — scheduler done.")
                self.generation_complete.emit()
                self.status_update.emit(
                    f"Subtitles complete — {self._dsrt.total_cues()} cues"
                )
                break

    # ── Seek handler ───────────────────────────────────────────────────────

    def _on_seek(self, new_time_ms: int):
        new_chunk = new_time_ms // CHUNK_MS

        with self._lock:
            # Abort workers far ahead of new position (>LOOKAHEAD chunks)
            stale = [
                idx for idx in list(self._active)
                if idx > new_chunk + LOOKAHEAD
            ]
            for idx in stale:
                log.debug("Aborting stale worker for chunk %d", idx)
                self._active[idx].abort = True
                del self._active[idx]

        # Backward seek into cached territory: instant, no work needed
        if self._dsrt.is_chunk_complete(new_chunk):
            self.status_update.emit(
                f"Seeked to {new_time_ms//1000}s — subtitles from cache ✓"
            )
            return

        # Forward seek past buffer: fire instant micro-chunk
        with self._lock:
            if new_chunk not in self._active:
                self._launch(new_chunk, micro=True)
        self.status_update.emit(
            f"Seeked to {new_time_ms//1000}s — generating subtitles…"
        )

    # ── Worker launch ──────────────────────────────────────────────────────

    def _launch(self, chunk_idx: int, micro: bool = False):
        """Must be called with self._lock held."""
        worker = ChunkWorker(
            chunk_idx=chunk_idx,
            media_path=self._media_path,
            dsrt=self._dsrt,
            model_size=self._model_size,
            language=self._language,
            quality=self._quality,
            on_done=lambda idx, segs: self.chunk_ready.emit(idx, segs),
            on_status=lambda msg: self.status_update.emit(msg),
            full_wav=self._full_wav,
            micro=micro,
        )
        worker.start()
        self._active[chunk_idx] = worker
        log.debug("Launched %s worker for chunk %d",
                  "micro" if micro else "full", chunk_idx)
