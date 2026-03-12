"""
audio_extractor.py
FFmpeg-based audio extraction. Extracts full audio or a slice from a media file.
"""

import os
import subprocess
import tempfile
import logging
from pathlib import Path

log = logging.getLogger("nova.audio")


class AudioExtractor:
    DEFAULT_FFMPEG = "ffmpeg"

    def __init__(self, ffmpeg_path: str = DEFAULT_FFMPEG):
        self.ffmpeg = ffmpeg_path

    def is_available(self) -> bool:
        try:
            r = subprocess.run([self.ffmpeg, "-version"],
                               capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def extract_full(self, media_path: str, out_wav: str) -> bool:
        """Extract full audio track as 16kHz mono PCM WAV."""
        cmd = [
            self.ffmpeg, "-y",
            "-i", media_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar",     "16000",
            "-ac",     "1",
            out_wav,
        ]
        return self._run(cmd)

    def extract_chunk(
        self,
        media_path: str,
        out_wav: str,
        start_ms: int,
        duration_ms: int,
    ) -> bool:
        """Extract a time slice from media file."""
        cmd = [
            self.ffmpeg, "-y",
            "-ss",     str(start_ms / 1000),
            "-i",      media_path,
            "-t",      str(duration_ms / 1000),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar",     "16000",
            "-ac",     "1",
            out_wav,
        ]
        return self._run(cmd)

    def slice_wav(
        self,
        full_wav: str,
        out_wav: str,
        start_ms: int,
        duration_ms: int,
    ) -> bool:
        """Slice a pre-extracted WAV — faster than re-demuxing media."""
        cmd = [
            self.ffmpeg, "-y",
            "-ss",     str(start_ms / 1000),
            "-i",      full_wav,
            "-t",      str(duration_ms / 1000),
            "-acodec", "copy",
            out_wav,
        ]
        return self._run(cmd)

    def _run(self, cmd: list[str]) -> bool:
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                timeout=300,
            )
            if r.returncode != 0:
                log.warning("FFmpeg error: %s", r.stderr.decode(errors="replace")[-500:])
            return r.returncode == 0
        except Exception as e:
            log.error("FFmpeg failed: %s", e)
            return False
