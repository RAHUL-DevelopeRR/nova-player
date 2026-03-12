"""
dsrt_file.py
Persistent, thread-safe subtitle cache in .dsrt (JSON) format.

Key design decisions:
  - SortedDict keyed by start_ms for O(log n) cue lookup at any position
  - Atomic save via temp-file rename (never corrupt on crash)
  - Chunks are append-friendly: add_cues() / remove_cues_for_chunk() are
    safe to call from background threads (protected by threading.Lock)
  - Backward seeks are zero-cost: cues already in _cue_map, just bisect
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sortedcontainers import SortedDict


class ChunkStatus(Enum):
    PENDING      = "PENDING"
    EXTRACTING   = "EXTRACTING"
    TRANSCRIBING = "TRANSCRIBING"
    COMPLETE     = "COMPLETE"
    FAILED       = "FAILED"


@dataclass
class DsrtCue:
    id:         int
    start_ms:   float   # absolute ms from media start
    end_ms:     float
    text:       str
    chunk_idx:  int
    cue_type:   str = "speech"   # "speech" | "sound_event"
    confidence: float = 0.0


@dataclass
class DsrtChunk:
    index:    int
    start_ms: int
    end_ms:   int
    status:   ChunkStatus = ChunkStatus.PENDING
    error:    str | None  = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class DsrtFile:
    """
    Thread-safe subtitle database for one media file.

    Usage:
        dsrt = DsrtFile.create(media_path, total_ms)
        dsrt = DsrtFile.load("/path/to/video.dsrt")

        cue = dsrt.get_active_cue(player.get_time())
        dsrt.add_cues(chunk_idx=0, segments=[...], chunk_start_s=0.0)
        dsrt.save()
    """

    DSRT_VERSION = 2
    DEFAULT_CHUNK_MS = 60_000

    def __init__(self):
        self._lock      = threading.Lock()
        self._cue_map   = SortedDict()      # float(start_ms) → DsrtCue
        self._next_id   = 1
        self._chunks: list[DsrtChunk] = []
        self._path: Path | None = None
        self.media_path: str  = ""
        self.total_ms:   int  = 0
        self.chunk_ms:   int  = self.DEFAULT_CHUNK_MS

    # ── Factory methods ────────────────────────────────────────────────────

    @classmethod
    def create(cls, media_path: str, total_ms: int,
               chunk_ms: int = DEFAULT_CHUNK_MS) -> "DsrtFile":
        obj = cls()
        obj.media_path = media_path
        obj.total_ms   = total_ms
        obj.chunk_ms   = chunk_ms
        obj._path      = Path(media_path).with_suffix(".dsrt")
        obj._build_chunks()
        return obj

    @classmethod
    def load(cls, dsrt_path: str | Path) -> "DsrtFile":
        data = json.loads(Path(dsrt_path).read_text(encoding="utf-8"))
        obj  = cls()
        obj.media_path = data["mediaFile"]
        obj.total_ms   = data["totalMs"]
        obj.chunk_ms   = data.get("chunkMs", cls.DEFAULT_CHUNK_MS)
        obj._path      = Path(dsrt_path)

        for c in data.get("chunks", []):
            chunk = DsrtChunk(
                index=c["index"],
                start_ms=c["startMs"],
                end_ms=c["endMs"],
                status=ChunkStatus(c.get("status", "PENDING")),
                error=c.get("error"),
            )
            obj._chunks.append(chunk)

        max_id = 0
        for c in data.get("cues", []):
            cue = DsrtCue(
                id=c["id"],
                start_ms=float(c["startMs"]),
                end_ms=float(c["endMs"]),
                text=c["text"],
                chunk_idx=c["chunkIndex"],
                cue_type=c.get("type", "speech"),
                confidence=c.get("confidence", 0.0),
            )
            key = float(c["startMs"])
            while key in obj._cue_map:
                key += 0.001
            obj._cue_map[key] = cue
            max_id = max(max_id, c["id"])

        obj._next_id = max_id + 1
        return obj

    # ── Cue access — O(log n) ──────────────────────────────────────────────

    def get_active_cue(self, current_ms: float) -> DsrtCue | None:
        """
        Returns the cue active at current_ms, or None.
        Safe to call from the Qt main thread every 50ms.
        """
        with self._lock:
            if not self._cue_map:
                return None
            idx = self._cue_map.bisect_right(current_ms) - 1
            if idx < 0:
                return None
            key = self._cue_map.keys()[idx]
            cue = self._cue_map[key]
            return cue if cue.end_ms > current_ms else None

    def get_cues_in_range(self, start_ms: float,
                          end_ms: float) -> list[DsrtCue]:
        """Returns all cues that overlap [start_ms, end_ms]."""
        with self._lock:
            result = []
            lo = self._cue_map.bisect_left(start_ms)
            for key in self._cue_map.keys()[max(0, lo - 1):]:
                cue = self._cue_map[key]
                if cue.start_ms > end_ms:
                    break
                if cue.end_ms > start_ms:
                    result.append(cue)
            return result

    # ── Cue writing ────────────────────────────────────────────────────────

    def add_cues(self, chunk_idx: int, segments: list[dict],
                 chunk_start_s: float):
        """
        Add transcription segments to the cue map.
        segments: list of dicts with keys start, end (seconds rel. to chunk),
                  text, type (optional), confidence (optional).
        chunk_start_s: absolute start of chunk in seconds.
        """
        with self._lock:
            for seg in segments:
                abs_start = (chunk_start_s + seg["start"]) * 1000
                abs_end   = (chunk_start_s + seg["end"])   * 1000
                cue = DsrtCue(
                    id=self._next_id,
                    start_ms=abs_start,
                    end_ms=abs_end,
                    text=seg["text"],
                    chunk_idx=chunk_idx,
                    cue_type=seg.get("type", "speech"),
                    confidence=seg.get("confidence", 0.0),
                )
                self._next_id += 1
                key = abs_start
                while key in self._cue_map:
                    key += 0.001
                self._cue_map[key] = cue

    def remove_cues_for_chunk(self, chunk_idx: int):
        """Remove all cues for a chunk (called before re-processing)."""
        with self._lock:
            keys_to_remove = [
                k for k, v in self._cue_map.items()
                if v.chunk_idx == chunk_idx
            ]
            for k in keys_to_remove:
                del self._cue_map[k]

    # ── Chunk helpers ──────────────────────────────────────────────────────

    def get_chunk(self, idx: int) -> DsrtChunk | None:
        return self._chunks[idx] if 0 <= idx < len(self._chunks) else None

    def is_chunk_complete(self, idx: int) -> bool:
        c = self.get_chunk(idx)
        return c is not None and c.status == ChunkStatus.COMPLETE

    def total_chunks(self) -> int:
        return len(self._chunks)

    def completed_chunks(self) -> int:
        return sum(1 for c in self._chunks
                   if c.status == ChunkStatus.COMPLETE)

    def total_cues(self) -> int:
        with self._lock:
            return len(self._cue_map)

    # ── Persistence ────────────────────────────────────────────────────────

    def save(self):
        """Atomically save to .dsrt file (temp-file rename)."""
        if not self._path:
            raise ValueError("No path set — use DsrtFile.create() or load()")

        with self._lock:
            data = {
                "version":   self.DSRT_VERSION,
                "mediaFile": self.media_path,
                "totalMs":   self.total_ms,
                "chunkMs":   self.chunk_ms,
                "chunks": [
                    {
                        "index":   c.index,
                        "startMs": c.start_ms,
                        "endMs":   c.end_ms,
                        "status":  c.status.value,
                        "error":   c.error,
                    }
                    for c in self._chunks
                ],
                "cues": [
                    {
                        "id":         v.id,
                        "startMs":    v.start_ms,
                        "endMs":      v.end_ms,
                        "text":       v.text,
                        "chunkIndex": v.chunk_idx,
                        "type":       v.cue_type,
                        "confidence": v.confidence,
                    }
                    for v in self._cue_map.values()
                ],
            }

        tmp = self._path.with_suffix(".dsrt.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)   # atomic rename — never corrupt on crash

    @property
    def path(self) -> Path | None:
        return self._path

    # ── Private ────────────────────────────────────────────────────────────

    def _build_chunks(self):
        n = max(1, -(-self.total_ms // self.chunk_ms))  # ceil div
        for i in range(n):
            s = i * self.chunk_ms
            e = min(s + self.chunk_ms, self.total_ms)
            self._chunks.append(DsrtChunk(index=i, start_ms=s, end_ms=e))
