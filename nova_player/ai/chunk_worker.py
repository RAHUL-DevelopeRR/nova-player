"""
chunk_worker.py
Per-chunk background thread that runs the full forensic pipeline:
  FFmpeg extract → VAD → ASR → end-time fix → drift → alignment → validate
Emits results via callbacks (safe to call from any thread).
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import Callable, Optional

from nova_player.ai import pipeline
from nova_player.ai.audio_extractor import AudioExtractor
from nova_player.subtitle.dsrt_file import DsrtFile, ChunkStatus

log = logging.getLogger("nova.chunk_worker")

MICRO_CHUNK_MS  = 10_000
CONTEXT_OVERLAP_MS = 5_000

# ── Shared model cache (one model instance per size) ──────────────────────
_model_cache: dict[str, object] = {}
_model_lock   = threading.Lock()


def get_model(size: str):
    with _model_lock:
        if size not in _model_cache:
            from faster_whisper import WhisperModel
            log.info("Loading WhisperModel('%s')…", size)
            _model_cache[size] = WhisperModel(
                size, device="cpu", compute_type="int8"
            )
            log.info("WhisperModel('%s') ready.", size)
        return _model_cache[size]


class ChunkWorker(threading.Thread):
    """
    Transcribes one DsrtChunk.

    micro=True  → only first MICRO_CHUNK_MS seconds with 'tiny' model
                  (instant preview), then schedules a full-quality re-run.
    micro=False → full chunk duration with user's chosen model.
    """

    def __init__(
        self,
        chunk_idx:   int,
        media_path:  str,
        dsrt:        DsrtFile,
        model_size:  str,
        language:    Optional[str],
        quality:     str,
        on_done:     Callable[[int, list], None],
        on_status:   Callable[[str], None],
        full_wav:    Optional[str] = None,
        micro:       bool = False,
    ):
        super().__init__(daemon=True)
        self.chunk_idx  = chunk_idx
        self.media_path = media_path
        self.dsrt       = dsrt
        self.model_size = model_size
        self.language   = language
        self.quality    = quality
        self.on_done    = on_done
        self.on_status  = on_status
        self.full_wav   = full_wav
        self.micro      = micro
        self.abort      = False     # set True by LookaheadScheduler on seek

    def run(self):
        chunk = self.dsrt.get_chunk(self.chunk_idx)
        if chunk is None:
            return

        effective_model = "tiny" if self.micro else self.model_size
        context_ms      = 0 if self.micro else CONTEXT_OVERLAP_MS
        ctx_start_ms    = max(0, chunk.start_ms - context_ms)
        actual_overlap  = chunk.start_ms - ctx_start_ms
        duration_ms     = (
            min(MICRO_CHUNK_MS, chunk.duration_ms)
            if self.micro else chunk.duration_ms
        )
        total_extract   = duration_ms + actual_overlap

        extractor = AudioExtractor()
        tmp_wav   = tempfile.mktemp(suffix=".wav", prefix="nova_chunk_")

        try:
            # ── Step 1: Audio extraction ──────────────────────────────────
            chunk.status = ChunkStatus.EXTRACTING
            label = f"Chunk {self.chunk_idx+1}/{self.dsrt.total_chunks()}"
            self.on_status(f"{label}: {'micro' if self.micro else 'full'} extracting…")

            ok = False
            if self.full_wav and os.path.exists(self.full_wav):
                ok = extractor.slice_wav(
                    self.full_wav, tmp_wav, ctx_start_ms, total_extract
                )
            if not ok:
                ok = extractor.extract_chunk(
                    self.media_path, tmp_wav, ctx_start_ms, total_extract
                )
            if not ok or self.abort:
                chunk.status = ChunkStatus.FAILED
                chunk.error  = "Audio extraction failed"
                return

            # ── Step 2–7: Forensic pipeline ───────────────────────────────
            chunk.status = ChunkStatus.TRANSCRIBING
            self.on_status(f"{label}: transcribing ({effective_model})…")

            audio    = pipeline.audio_load(tmp_wav)
            speech_r, _ = pipeline.vad(audio)

            if self.abort: return

            model    = get_model(effective_model)
            segments, lang = pipeline.asr(
                model, speech_r, audio,
                language=self.language,
                quality="INSTANT" if self.micro else self.quality,
            )

            if self.abort: return

            segments = pipeline.correct_end_times(segments, audio)
            segments, drift_ms = pipeline.correct_drift(segments, speech_r)
            segments, refined  = pipeline.refine_alignment(segments, audio)
            segments, info     = pipeline.validate(segments, audio)

            # Strip context overlap
            if actual_overlap > 0:
                overlap_s = actual_overlap / 1000
                segments  = [
                    {**s,
                     "start": round(s["start"] - overlap_s, 3),
                     "end":   round(s["end"]   - overlap_s, 3)}
                    for s in segments
                    if s["start"] >= overlap_s
                ]

            # ── Step 3: Write to DsrtFile ─────────────────────────────────
            self.dsrt.remove_cues_for_chunk(self.chunk_idx)
            self.dsrt.add_cues(
                self.chunk_idx, segments,
                chunk_start_s=chunk.start_ms / 1000
            )
            chunk.status = ChunkStatus.COMPLETE
            self.dsrt.save()

            conf = info.get("overall_confidence", 0)
            self.on_status(
                f"{label} {'preview' if self.micro else 'complete'} "
                f"— {len(segments)} cues  confidence {conf:.0f}%"
            )
            self.on_done(self.chunk_idx, segments)

            # Re-queue full-quality pass after micro preview
            if self.micro and not self.abort:
                full_worker = ChunkWorker(
                    chunk_idx=self.chunk_idx,
                    media_path=self.media_path,
                    dsrt=self.dsrt,
                    model_size=self.model_size,
                    language=self.language,
                    quality=self.quality,
                    on_done=self.on_done,
                    on_status=self.on_status,
                    full_wav=self.full_wav,
                    micro=False,
                )
                full_worker.start()

        except Exception as exc:
            log.exception("ChunkWorker error on chunk %d", self.chunk_idx)
            chunk.status = ChunkStatus.FAILED
            chunk.error  = str(exc)
        finally:
            try:
                os.unlink(tmp_wav)
            except OSError:
                pass
