"""
pipeline.py
Exposes the forensic whisper_server.py stages as importable functions.
All heavy logic lives here; ChunkWorker and LookaheadScheduler call these.

Stages:
  1. audio_load()        — soundfile → float32 numpy array
  2. vad()               — Silero VAD → speech / non-speech regions
  3. asr()               — faster-whisper per speech region
  4. correct_end_times() — waveform RMS trailing-window correction
  5. correct_drift()     — linear regression VAD vs ASR drift
  6. refine_alignment()  — ±50ms phoneme onset/offset snap
  7. validate()          — overlap fix, confidence gate
"""

from __future__ import annotations

import logging
import traceback
from typing import Optional

import numpy as np
import soundfile as sf

log = logging.getLogger("nova.pipeline")

SAMPLE_RATE = 16_000

QUALITY_PRESETS = {
    "INSTANT":  {"beam_size": 1, "best_of": 1, "temperature": [0.0]},
    "FAST":     {"beam_size": 1, "best_of": 1, "temperature": [0.0, 0.2]},
    "BALANCED": {"beam_size": 3, "best_of": 1, "temperature": [0.0, 0.2, 0.4]},
    "BEST":     {"beam_size": 5, "best_of": 1, "temperature": [0.0, 0.2, 0.4, 0.6, 0.8]},
}

# ── Stage 1: Audio load ────────────────────────────────────────────────────

def audio_load(wav_path: str) -> np.ndarray:
    audio, sr = sf.read(wav_path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
    return audio


# ── Stage 2: Silero VAD ────────────────────────────────────────────────────

_vad_model  = None
_vad_utils  = None
_vad_loaded = False

def _load_vad():
    global _vad_model, _vad_utils, _vad_loaded
    if _vad_loaded:
        return _vad_model is not None
    try:
        import torch
        _vad_model, _vad_utils = torch.hub.load(
            "snakers4/silero-vad", "silero_vad",
            force_reload=False, onnx=False, verbose=False,
        )
        _vad_loaded = True
        log.info("Silero VAD loaded.")
        return True
    except Exception:
        _vad_loaded = True
        log.warning("Silero VAD unavailable — full audio passed to ASR.")
        return False


def vad(audio: np.ndarray) -> tuple[list[dict], list[dict]]:
    """
    Returns (speech_regions, nonspeech_regions).
    Each region: {start: float, end: float, audio: np.ndarray}  (times in seconds)
    Falls back to single full-audio speech region if VAD unavailable.
    """
    if not _load_vad() or _vad_model is None:
        total = len(audio) / SAMPLE_RATE
        return [{"start": 0.0, "end": total, "audio": audio}], []

    try:
        import torch
        get_speech_ts = _vad_utils[0]
        wav_tensor    = torch.from_numpy(audio)
        total_dur     = len(audio) / SAMPLE_RATE

        speech_ts = get_speech_ts(
            wav_tensor, _vad_model,
            threshold=0.45,
            min_speech_duration_ms=200,
            min_silence_duration_ms=250,
            return_seconds=True,
            sampling_rate=SAMPLE_RATE,
        )

        ENERGY_THR = 0.001
        speech, nonspeech = [], []
        prev = 0.0

        for ts in speech_ts:
            s, e = ts["start"], ts["end"]
            seg  = audio[int(s * SAMPLE_RATE): int(e * SAMPLE_RATE)]
            if s > prev + 0.05:
                gap = audio[int(prev * SAMPLE_RATE): int(s * SAMPLE_RATE)]
                if len(gap) > 0 and float(np.abs(gap).mean()) > ENERGY_THR:
                    nonspeech.append({"start": prev, "end": s, "audio": gap})
            speech.append({"start": s, "end": e, "audio": seg})
            prev = e

        if total_dur - prev > 0.05:
            tail = audio[int(prev * SAMPLE_RATE):]
            if len(tail) > 0 and float(np.abs(tail).mean()) > ENERGY_THR:
                nonspeech.append({"start": prev, "end": total_dur, "audio": tail})

        log.debug("VAD: %d speech, %d non-speech", len(speech), len(nonspeech))
        return speech, nonspeech

    except Exception:
        log.warning("VAD failed:\n%s", traceback.format_exc())
        total = len(audio) / SAMPLE_RATE
        return [{"start": 0.0, "end": total, "audio": audio}], []


# ── Stage 3: ASR ───────────────────────────────────────────────────────────

def asr(
    model,
    speech_regions: list[dict],
    full_audio: np.ndarray,
    language: Optional[str] = None,
    quality: str = "BALANCED",
    translate: bool = False,
) -> tuple[list[dict], str]:
    preset     = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["BALANCED"])
    task       = "translate" if translate else "transcribe"
    segments   = []
    detected   = language
    regions    = speech_regions or [{"start": 0.0,
                                     "end": len(full_audio)/SAMPLE_RATE,
                                     "audio": full_audio}]

    for ri, region in enumerate(regions):
        chunk = region["audio"].astype(np.float32)
        if len(chunk) < 1600:   # < 0.1s skip
            continue

        segs_iter, info = model.transcribe(
            chunk,
            language=detected,
            task=task,
            beam_size=preset["beam_size"],
            best_of=preset["best_of"],
            temperature=preset["temperature"],
            condition_on_previous_text=True,
            vad_filter=False,
            word_timestamps=True,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )

        if detected is None:
            detected = info.language

        offset = region["start"]
        for seg in segs_iter:
            text = seg.text.strip()
            if not text:
                continue
            start = (seg.words[0].start if seg.words else seg.start) + offset
            end   = (seg.words[-1].end  if seg.words else seg.end)   + offset
            words = [
                {"word": w.word.strip(),
                 "start": round(w.start + offset, 3),
                 "end":   round(w.end   + offset, 3),
                 "prob":  round(w.probability, 4)}
                for w in (seg.words or [])
            ]
            segments.append({
                "start":      round(start, 3),
                "end":        round(end,   3),
                "text":       text,
                "type":       "speech",
                "confidence": round(seg.avg_logprob, 4),
                "_words":     words,
            })

        log.debug("ASR region %d/%d done", ri + 1, len(regions))

    return segments, detected or "en"


# ── Stage 4: End-time correction ───────────────────────────────────────────

def correct_end_times(
    segments: list[dict],
    audio: np.ndarray,
    trailing_window_ms: int = 350,
    rms_frame_ms: int = 10,
) -> list[dict]:
    try:
        frame_s  = int(rms_frame_ms / 1000 * SAMPLE_RATE)
        win_s    = int(trailing_window_ms / 1000 * SAMPLE_RATE)
        fixes    = 0
        for seg in segments:
            end_idx    = int(seg["end"] * SAMPLE_RATE)
            trail_end  = min(end_idx + win_s, len(audio))
            if trail_end <= end_idx:
                continue
            trail     = audio[end_idx:trail_end]
            seg_audio = audio[int(seg["start"] * SAMPLE_RATE): end_idx]
            if len(seg_audio) < frame_s:
                continue
            seg_rms   = float(np.sqrt(np.mean(seg_audio ** 2)))
            threshold = seg_rms * 0.10
            silence   = None
            for i in range(0, len(trail) - frame_s, frame_s):
                if float(np.sqrt(np.mean(trail[i:i + frame_s] ** 2))) < threshold:
                    silence = i
                    break
            if silence is None:
                seg["end"] = round(
                    min(seg["end"] + trailing_window_ms/1000,
                        len(audio)/SAMPLE_RATE), 3)
                fixes += 1
            elif silence > 0:
                seg["end"] = round(seg["end"] + silence/SAMPLE_RATE, 3)
                fixes += 1
        log.debug("Stage 4: %d end-time fixes", fixes)
    except Exception:
        log.warning("correct_end_times failed:\n%s", traceback.format_exc())
    return segments


# ── Stage 5: Drift correction ──────────────────────────────────────────────

def correct_drift(
    segments: list[dict],
    speech_regions: list[dict],
) -> tuple[list[dict], float]:
    mean_drift_ms = 0.0
    if not speech_regions or len(segments) < 2:
        return segments, mean_drift_ms
    try:
        drifts = []
        for seg in segments:
            best = min(speech_regions,
                       key=lambda r: abs(r["start"] - seg["start"]))
            drifts.append((seg["start"], seg["start"] - best["start"]))

        times      = np.array([d[0] for d in drifts])
        drift_vals = np.array([d[1] for d in drifts])
        mean_drift = float(np.mean(drift_vals))
        std_drift  = float(np.std(drift_vals))
        mean_drift_ms = mean_drift * 1000
        coeffs = np.polyfit(times, drift_vals, 1) if len(times) >= 3 else [0.0, mean_drift]
        slope  = coeffs[0] * 1000 * 60

        def _apply(v, t):
            correction = coeffs[0] * t + coeffs[1]
            return round(max(0.0, v - correction), 3)

        if abs(slope) > 5.0:
            for seg in segments:
                seg["start"] = _apply(seg["start"], seg["start"])
                seg["end"]   = _apply(seg["end"],   seg["end"])
        elif abs(mean_drift) > 0.02 and std_drift < 0.02:
            for seg in segments:
                seg["start"] = round(max(0.0, seg["start"] - mean_drift), 3)
                seg["end"]   = round(max(0.0, seg["end"]   - mean_drift), 3)

        log.debug("Stage 5 drift: mean=%.1fms slope=%.2fms/min",
                  mean_drift_ms, slope)
    except Exception:
        log.warning("correct_drift failed:\n%s", traceback.format_exc())
    return segments, mean_drift_ms


# ── Stage 6: Alignment refinement ─────────────────────────────────────────

def refine_alignment(
    segments: list[dict],
    audio: np.ndarray,
    search_window_ms: int = 50,
    onset_ratio: float = 0.15,
) -> tuple[list[dict], int]:
    refined = 0
    try:
        frame_ms  = 2
        fs        = int(frame_ms / 1000 * SAMPLE_RATE)
        ws        = int(search_window_ms / 1000 * SAMPLE_RATE)
        total_s   = len(audio)

        for seg in segments:
            changed = False
            for attr in ("start", "end"):
                ci  = int(seg[attr] * SAMPLE_RATE)
                wlo = max(0, ci - ws); whi = min(total_s, ci + ws)
                region = audio[wlo:whi]
                if len(region) <= fs * 4:
                    continue
                peak = float(np.max(np.abs(region)))
                thr  = peak * onset_ratio
                rng  = range(0, len(region)-fs, fs) if attr == "start" \
                       else range(len(region)-fs, 0, -fs)
                for i in rng:
                    if float(np.max(np.abs(region[i:i+fs]))) >= thr:
                        nv = (wlo + i + (fs if attr == "end" else 0)) / SAMPLE_RATE
                        if abs(nv - seg[attr]) < search_window_ms/1000:
                            seg[attr] = round(nv, 3)
                            changed = True
                        break
            if changed:
                refined += 1
    except Exception:
        log.warning("refine_alignment failed:\n%s", traceback.format_exc())
    return segments, refined


# ── Stage 7: Validate & fix overlaps ──────────────────────────────────────

def validate(
    segments: list[dict],
    audio: np.ndarray,
    confidence_gate: float = -1.5,
) -> tuple[list[dict], dict]:
    total_dur = len(audio) / SAMPLE_RATE
    filtered  = [s for s in segments
                 if s.get("confidence", 0) >= confidence_gate]
    filtered.sort(key=lambda s: s["start"])

    # Fix overlaps
    overlaps_fixed = 0
    for i in range(1, len(filtered)):
        if filtered[i]["start"] < filtered[i-1]["end"]:
            filtered[i-1]["end"] = filtered[i]["start"]
            overlaps_fixed += 1

    # Clamp to media duration
    for seg in filtered:
        seg["end"] = min(seg["end"], total_dur)
        seg["start"] = min(seg["start"], seg["end"] - 0.05)

    confidences = [s["confidence"] for s in filtered if s.get("confidence")]
    overall     = round(
        (1 - abs(float(np.mean(confidences)))) * 100 if confidences else 0, 1
    )
    info = {
        "overall_confidence": overall,
        "overlaps_fixed":     overlaps_fixed,
        "segments_after_gate":len(filtered),
    }
    return filtered, info
