import hashlib
import math
import os
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import numpy as np


@dataclass
class AudioMetrics:
    is_playing: bool = False
    is_silent: bool = True
    rms_db: float = -120.0
    peak_db: float = -120.0
    rms_db_left: float = -120.0
    rms_db_right: float = -120.0
    peak_db_left: float = -120.0
    peak_db_right: float = -120.0
    dominant_freq_hz: float = 0.0
    dominant_freq_left_hz: float = 0.0
    dominant_freq_right_hz: float = 0.0
    total_packets: int = 0
    total_bytes: int = 0
    total_frames: int = 0
    last_packet_time: float = 0.0
    bitrate_kbps: float = 0.0
    waveform_preview: List[float] = field(default_factory=list)
    spectrum_preview: List[float] = field(default_factory=list)


class AudioStreamAnalyzer:
    """Real-time PCM stream analyzer for USB Isochronous Audio OUT packets."""

    def __init__(self, captures_dir: str = "captures") -> None:
        self.captures_dir = captures_dir
        os.makedirs(self.captures_dir, exist_ok=True)
        self.metrics = AudioMetrics()
        self._sample_buffer = deque(maxlen=16384)  # Stereo float32 frames
        self._byte_Window = deque(maxlen=100)  # (timestamp, nbytes)
        self._remainder = b""

        # Recording state
        self.is_recording: bool = False
        self.recording_path: Optional[str] = None
        self._recording_buffer: bytearray = bytearray()
        self._recording_rate: int = 48000
        self._recording_bits: int = 16
        self._recording_channels: int = 2
        self._recording_start_time: float = 0.0
        self.last_recording_info: Optional[Dict[str, Any]] = None

    def reset_stats(self) -> None:
        self.metrics = AudioMetrics()
        self._sample_buffer.clear()
        self._byte_Window.clear()
        self._remainder = b""

    def on_alt_setting_change(self, alt_setting: int) -> None:
        self._remainder = b""
        if alt_setting == 0:
            self.metrics.is_playing = False

    def feed_pcm_packet(
        self,
        raw_bytes: bytes,
        sample_rate: int,
        subframe_size: int,
        bit_resolution: int,
        channels: int = 2,
    ) -> None:
        if not raw_bytes:
            return

        now = time.time()
        self.metrics.total_packets += 1
        self.metrics.total_bytes += len(raw_bytes)
        self.metrics.last_packet_time = now
        self.metrics.is_playing = True
        self._byte_Window.append((now, len(raw_bytes)))

        if self.is_recording:
            self._recording_buffer.extend(raw_bytes)

        frame_stride = subframe_size * channels
        data = self._remainder + raw_bytes
        usable_len = (len(data) // frame_stride) * frame_stride
        if usable_len == 0:
            self._remainder = data
            return

        self._remainder = data[usable_len:]
        chunk = data[:usable_len]
        num_frames = usable_len // frame_stride
        self.metrics.total_frames += num_frames

        samples = self._unpack_pcm_to_float(chunk, subframe_size, bit_resolution, channels)
        if samples.size == 0:
            return

        for frame in samples[-2048:]:
            self._sample_buffer.append(frame)

        self._compute_metrics(sample_rate)

    @staticmethod
    def _unpack_pcm_to_float(
        chunk: bytes, subframe_size: int, bit_resolution: int, channels: int
    ) -> np.ndarray:
        """Convert packed 16-bit, 24-bit, or 32-bit little-endian PCM into float32 [-1.0, 1.0] shape (N, channels)."""
        if subframe_size == 2:
            arr = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
        elif subframe_size == 3:
            # 24-bit packed 3-byte little-endian signed integers
            raw_u8 = np.frombuffer(chunk, dtype=np.uint8).reshape(-1, 3)
            int32_vals = (
                raw_u8[:, 0].astype(np.int32)
                | (raw_u8[:, 1].astype(np.int32) << 8)
                | (raw_u8[:, 2].astype(np.int32) << 16)
            )
            # Sign-extend 24-bit to 32-bit
            int32_vals = np.where(int32_vals & 0x800000, int32_vals - 0x1000000, int32_vals)
            arr = int32_vals.astype(np.float32) / 8388608.0
        elif subframe_size == 4:
            arr = np.frombuffer(chunk, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            return np.empty((0, channels), dtype=np.float32)

        return arr.reshape(-1, channels)

    def _compute_metrics(self, sample_rate: int) -> None:
        if len(self._sample_buffer) < 64:
            return

        arr = np.array(self._sample_buffer, dtype=np.float32)
        left = arr[:, 0]
        right = arr[:, 1] if arr.shape[1] > 1 else left

        eps = 1e-12
        rms_l = float(np.sqrt(np.mean(np.square(left))))
        rms_r = float(np.sqrt(np.mean(np.square(right))))
        rms_all = float(np.sqrt(0.5 * (rms_l * rms_l + rms_r * rms_r)))

        peak_l = float(np.max(np.abs(left)))
        peak_r = float(np.max(np.abs(right)))
        peak_all = max(peak_l, peak_r)

        self.metrics.rms_db_left = round(max(-120.0, 20.0 * math.log10(max(rms_l, eps))), 2)
        self.metrics.rms_db_right = round(max(-120.0, 20.0 * math.log10(max(rms_r, eps))), 2)
        self.metrics.rms_db = round(max(-120.0, 20.0 * math.log10(max(rms_all, eps))), 2)

        self.metrics.peak_db_left = round(max(-120.0, 20.0 * math.log10(max(peak_l, eps))), 2)
        self.metrics.peak_db_right = round(max(-120.0, 20.0 * math.log10(max(peak_r, eps))), 2)
        self.metrics.peak_db = round(max(-120.0, 20.0 * math.log10(max(peak_all, eps))), 2)

        self.metrics.is_silent = self.metrics.rms_db < -85.0

        # FFT frequency analysis on up to 4096 samples
        n_fft = min(len(left), 4096)
        # Round down to power of 2
        n_fft = 1 << (n_fft.bit_length() - 1)
        if n_fft >= 256 and not self.metrics.is_silent:
            freq_l, spec_preview = self._estimate_dominant_freq(left[-n_fft:], sample_rate)
            freq_r, _ = self._estimate_dominant_freq(right[-n_fft:], sample_rate)
            self.metrics.dominant_freq_left_hz = round(freq_l, 2)
            self.metrics.dominant_freq_right_hz = round(freq_r, 2)
            self.metrics.dominant_freq_hz = round(freq_l if rms_l >= rms_r else freq_r, 2)
            self.metrics.spectrum_preview = spec_preview
        else:
            self.metrics.dominant_freq_hz = 0.0
            self.metrics.dominant_freq_left_hz = 0.0
            self.metrics.dominant_freq_right_hz = 0.0
            self.metrics.spectrum_preview = [-100.0] * 32

        # Waveform preview (64 points)
        preview_len = min(len(left), 128)
        step = max(1, preview_len // 64)
        self.metrics.waveform_preview = [round(float(x), 4) for x in left[-preview_len::step][:64]]

    @staticmethod
    def _estimate_dominant_freq(signal: np.ndarray, sample_rate: int):
        n = len(signal)
        window = np.hanning(n)
        windowed = (signal - np.mean(signal)) * window
        spec = np.abs(np.fft.rfft(windowed))
        if len(spec) < 4:
            return 0.0, [-100.0] * 32

        # Ignore DC bin 0
        spec[0] = 0.0
        peak_idx = int(np.argmax(spec))

        # Parabolic interpolation around peak bin for high accuracy
        if 1 <= peak_idx < len(spec) - 1:
            alpha = float(spec[peak_idx - 1])
            beta = float(spec[peak_idx])
            gamma = float(spec[peak_idx + 1])
            denom = alpha - 2.0 * beta + gamma
            p = 0.5 * (alpha - gamma) / denom if abs(denom) > 1e-12 else 0.0
            true_bin = peak_idx + p
        else:
            true_bin = float(peak_idx)

        freq_hz = true_bin * sample_rate / float(n)

        # Build 32-band spectrum preview in dB
        norm_spec = spec / (n * 0.25 + 1e-12)
        bins_per_band = max(1, len(norm_spec) // 32)
        bands = []
        for i in range(32):
            sub = norm_spec[i * bins_per_band : (i + 1) * bins_per_band]
            val = float(np.max(sub)) if sub.size > 0 else 1e-9
            bands.append(round(max(-100.0, 20.0 * math.log10(max(val, 1e-9))), 1))

        return max(0.0, freq_hz), bands

    def start_recording(
        self, sample_rate: int, bit_resolution: int, channels: int = 2, filename: Optional[str] = None
    ) -> str:
        if not filename:
            filename = f"usb_capture_{int(time.time())}_{sample_rate}Hz_{bit_resolution}bit.wav"
        self.recording_path = os.path.abspath(os.path.join(self.captures_dir, filename))
        self._recording_buffer = bytearray()
        self._recording_rate = sample_rate
        self._recording_bits = bit_resolution
        self._recording_channels = channels
        self._recording_start_time = time.time()
        self.is_recording = True
        return self.recording_path

    def stop_recording(self) -> Dict[str, Any]:
        self.is_recording = False
        duration = max(0.0, time.time() - self._recording_start_time) if self._recording_start_time else 0.0
        raw_bytes = bytes(self._recording_buffer)
        sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
        sampwidth = max(2, self._recording_bits // 8)
        frame_stride = sampwidth * self._recording_channels
        usable_len = (len(raw_bytes) // frame_stride) * frame_stride
        pcm_payload = raw_bytes[:usable_len]

        if self.recording_path and usable_len >= 0:
            with wave.open(self.recording_path, "wb") as wf:
                wf.setnchannels(self._recording_channels)
                wf.setsampwidth(sampwidth)
                wf.setframerate(self._recording_rate)
                wf.writeframes(pcm_payload)

        info = {
            "path": self.recording_path,
            "bytes_captured": len(pcm_payload),
            "frames_captured": usable_len // frame_stride if frame_stride else 0,
            "duration_sec": round(duration, 3),
            "sample_rate": self._recording_rate,
            "bit_depth": self._recording_bits,
            "channels": self._recording_channels,
            "sha256": sha256_hex,
        }
        self.last_recording_info = info
        return info

    def get_snapshot(self) -> Dict[str, Any]:
        now = time.time()
        # If no packet arrived in last 250ms, mark stream idle
        if self.metrics.is_playing and (now - self.metrics.last_packet_time) > 0.25:
            self.metrics.is_playing = False
            self.metrics.is_silent = True
            self.metrics.rms_db = -120.0
            self.metrics.peak_db = -120.0
            self.metrics.dominant_freq_hz = 0.0

        # Estimate recent bitrate over last 1.0s
        recent_bytes = sum(b for t, b in self._byte_Window if now - t <= 1.0)
        self.metrics.bitrate_kbps = round((recent_bytes * 8.0) / 1000.0, 1) if self.metrics.is_playing else 0.0

        return {
            "is_playing": self.metrics.is_playing,
            "is_silent": self.metrics.is_silent,
            "rms_db": self.metrics.rms_db,
            "peak_db": self.metrics.peak_db,
            "rms_db_left": self.metrics.rms_db_left,
            "rms_db_right": self.metrics.rms_db_right,
            "peak_db_left": self.metrics.peak_db_left,
            "peak_db_right": self.metrics.peak_db_right,
            "dominant_freq_hz": self.metrics.dominant_freq_hz,
            "dominant_freq_left_hz": self.metrics.dominant_freq_left_hz,
            "dominant_freq_right_hz": self.metrics.dominant_freq_right_hz,
            "total_packets": self.metrics.total_packets,
            "total_bytes": self.metrics.total_bytes,
            "total_frames": self.metrics.total_frames,
            "bitrate_kbps": self.metrics.bitrate_kbps,
            "is_recording": self.is_recording,
            "recording_path": self.recording_path if self.is_recording else None,
            "last_recording": self.last_recording_info,
            "waveform_preview": self.metrics.waveform_preview,
            "spectrum_preview": self.metrics.spectrum_preview,
        }
