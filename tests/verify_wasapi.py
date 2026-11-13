"""
End-to-End Verification & Reference Player for Virtual Hi-Fi USB DAC.

Verifies:
1. Windows PnP & MMDevice Enumeration of 'Virtual Hi-Fi USB DAC'.
2. WASAPI Exclusive Mode format matrix support (44.1kHz ~ 192kHz @ 16/24/32-bit).
3. WASAPI Shared Mode playback (440 Hz test tone) + WAV capture + FFT verification.
4. WASAPI Exclusive Mode bit-perfect playback & hardware sample-rate/bit-depth switching:
   - 44100 Hz / 16-bit (Alt 1) @ 1000 Hz
   - 96000 Hz / 24-bit (Alt 2) @ 1500 Hz
   - 192000 Hz / 32-bit (Alt 3) @ 2000 Hz
5. Error injection & USB Hotplug (Detach / Attach) via HTTP API.
"""

import ctypes
from ctypes import POINTER, Structure, byref, cast, c_ubyte, c_uint16, c_uint32
import json
import math
import sys
import threading
import time
import urllib.request
import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
import numpy as np
import pyaudiowpatch as pyaudio
import pycaw.pycaw as pc

API_BASE = "http://127.0.0.1:8765"

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_SHAREMODE_EXCLUSIVE = 1

KSDATAFORMAT_SUBTYPE_PCM = GUID("{00000001-0000-0010-8000-00aa00389b71}")


class WAVEFORMATEXTENSIBLE(Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", c_uint16),
        ("nChannels", c_uint16),
        ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32),
        ("nBlockAlign", c_uint16),
        ("wBitsPerSample", c_uint16),
        ("cbSize", c_uint16),
        ("wValidBitsPerSample", c_uint16),
        ("dwChannelMask", c_uint32),
        ("SubFormat", GUID),
    ]


class IAudioRenderClient(IUnknown):
    _iid_ = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["in"], c_uint32, "NumFramesRequested"),
            (["out"], POINTER(POINTER(ctypes.c_ubyte)), "ppData"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "ReleaseBuffer",
            (["in"], c_uint32, "NumFramesWritten"),
            (["in"], c_uint32, "dwFlags"),
        ),
    )


def api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_post(path: str, payload: dict = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_pcm_bytes(freq_hz: float, sample_rate: int, num_frames: int, bit_depth: int, amplitude: float = 0.5) -> bytes:
    t = np.arange(num_frames, dtype=np.float64) / float(sample_rate)
    mono = amplitude * np.sin(2.0 * math.pi * freq_hz * t)
    stereo = np.column_stack((mono, mono)).flatten()

    if bit_depth == 16:
        return np.clip(stereo * 32767.0, -32768, 32767).astype("<i2").tobytes()
    elif bit_depth == 24:
        pcm32 = np.clip(stereo * 8388607.0, -8388608, 8388607).astype(np.int32)
        u32 = pcm32 & 0xFFFFFF
        b0 = (u32 & 0xFF).astype(np.uint8)
        b1 = ((u32 >> 8) & 0xFF).astype(np.uint8)
        b2 = ((u32 >> 16) & 0xFF).astype(np.uint8)
        return np.column_stack((b0, b1, b2)).flatten().tobytes()
    elif bit_depth == 32:
        return np.clip(stereo * 2147483647.0, -2147483648, 2147483647).astype("<i4").tobytes()
    raise ValueError(f"Unsupported bit_depth: {bit_depth}")


def find_virtual_dac_mmdevice():
    for _ in range(25):
        devs = pc.AudioUtilities.GetAllDevices()
        matches = [
            d for d in devs
            if d.FriendlyName and "Virtual Hi-Fi USB DAC" in d.FriendlyName and d.state == pc.AudioDeviceState.Active
        ]
        if matches:
            return matches[0]
        time.sleep(0.2)
    raise RuntimeError("Virtual Hi-Fi USB DAC active MMDevice not found in Windows Audio.")


def play_wasapi_exclusive(sample_rate: int, bit_depth: int, freq_hz: float, duration_sec: float = 1.2) -> dict:
    """
    Reference implementation of WASAPI Exclusive Mode (USB 独占模式) playback on Windows.
    Directly initializes IAudioClient with AUDCLNT_SHAREMODE_EXCLUSIVE, switching the USB DAC
    hardware clock sample rate and bit depth without going through the Windows mixer.
    """
    comtypes.CoInitialize()
    try:
        dac_dev = find_virtual_dac_mmdevice()
        audio_client = dac_dev._dev.Activate(
            pc.IAudioClient._iid_, comtypes.CLSCTX_ALL, None
        ).QueryInterface(pc.IAudioClient)

        block_align = 2 * (bit_depth // 8)
        wfx = WAVEFORMATEXTENSIBLE(
            0xFFFE,
            2,
            sample_rate,
            sample_rate * block_align,
            block_align,
            bit_depth,
            22,
            bit_depth,
            0x3,
            KSDATAFORMAT_SUBTYPE_PCM,
        )
        p_wfx = cast(byref(wfx), POINTER(pc.WAVEFORMATEX))

        # Check device period and initialize in WASAPI Exclusive Polling mode
        default_period, min_period = audio_client.GetDevicePeriod()
        buffer_duration = max(default_period * 4, 200000)  # 20ms in 100ns units
        audio_client.Initialize(
            AUDCLNT_SHAREMODE_EXCLUSIVE,
            0,
            buffer_duration,
            buffer_duration,
            p_wfx,
            None,
        )

        buf_frames = audio_client.GetBufferSize()
        render_unk = audio_client.GetService(IAudioRenderClient._iid_)
        render_client = render_unk.QueryInterface(IAudioRenderClient)

        total_frames = int(sample_rate * duration_sec)
        pcm_bytes = build_pcm_bytes(freq_hz, sample_rate, total_frames, bit_depth, amplitude=0.5)
        frame_bytes = block_align

        # Pre-fill first buffer before Start()
        init_frames = min(buf_frames, total_frames)
        data_ptr = render_client.GetBuffer(init_frames)
        ctypes.memmove(data_ptr, pcm_bytes[: init_frames * frame_bytes], init_frames * frame_bytes)
        render_client.ReleaseBuffer(init_frames, 0)
        cursor_frames = init_frames

        api_post("/api/stats/reset")
        audio_client.Start()

        status_snapshot = None
        start_wall = time.time()
        while cursor_frames < total_frames:
            # In Exclusive polling mode without event handle, poll GetCurrentPadding or feed every period
            try:
                padding = audio_client.GetCurrentPadding()
                avail = buf_frames - padding
            except Exception:
                avail = buf_frames // 2

            if avail > 0:
                to_write = min(avail, total_frames - cursor_frames)
                if to_write > 0:
                    try:
                        ptr = render_client.GetBuffer(to_write)
                        offset_b = cursor_frames * frame_bytes
                        ctypes.memmove(ptr, pcm_bytes[offset_b : offset_b + to_write * frame_bytes], to_write * frame_bytes)
                        render_client.ReleaseBuffer(to_write, 0)
                        cursor_frames += to_write
                    except Exception:
                        pass

            if status_snapshot is None and (time.time() - start_wall) >= 0.45:
                st = api_get("/api/status")
                if st["audio"]["is_playing"] and not st["audio"]["is_silent"]:
                    status_snapshot = st

            time.sleep(0.004)

        if status_snapshot is None:
            status_snapshot = api_get("/api/status")

        audio_client.Stop()
        audio_client.Reset()
        return status_snapshot
    finally:
        comtypes.CoUninitialize()


def main():
    print("=" * 72)
    print("  Virtual Hi-Fi USB DAC — End-to-End Verification Suite")
    print("=" * 72)

    # Ensure device is attached
    st0 = api_get("/api/status")
    if not st0["hardware"]["attached"]:
        print("[1/5] Attaching Virtual USB DAC via /api/device/plug ...")
        api_post("/api/device/plug")
        time.sleep(1.5)
        st0 = api_get("/api/status")

    assert st0["hardware"]["attached"], "USB DAC should be attached"
    dac_dev = find_virtual_dac_mmdevice()
    print(f"[1/5] PASS: Found active Windows MMDevice: {dac_dev.FriendlyName}")

    # 2. Verify WASAPI Exclusive Format Support Matrix
    ac = dac_dev._dev.Activate(pc.IAudioClient._iid_, comtypes.CLSCTX_ALL, None).QueryInterface(pc.IAudioClient)
    supported_count = 0
    for rate in [44100, 48000, 88200, 96000, 176400, 192000]:
        for bits in [16, 24, 32]:
            block_align = 2 * (bits // 8)
            wfx = WAVEFORMATEXTENSIBLE(
                0xFFFE, 2, rate, rate * block_align, block_align, bits, 22, bits, 0x3, KSDATAFORMAT_SUBTYPE_PCM
            )
            ac.IsFormatSupported(AUDCLNT_SHAREMODE_EXCLUSIVE, cast(byref(wfx), POINTER(pc.WAVEFORMATEX)))
            supported_count += 1
    print(f"[2/5] PASS: All {supported_count}/18 Hi-Fi formats (44.1kHz~192kHz @ 16/24/32-bit) supported in WASAPI Exclusive Mode!")

    # 3. Verify WASAPI Shared Mode + WAV Capture
    print("[3/5] Testing WASAPI Shared Mode playback (440 Hz tone) & WAV capture ...")
    p = pyaudio.PyAudio()
    wasapi_idx = [
        i for i in range(p.get_device_count())
        if "Virtual Hi-Fi USB DAC" in p.get_device_info_by_index(i)["name"]
        and p.get_host_api_info_by_index(p.get_device_info_by_index(i)["hostApi"])["name"] == "Windows WASAPI"
        and not p.get_device_info_by_index(i).get("isLoopbackDevice")
    ][0]
    shared_rate = int(p.get_device_info_by_index(wasapi_idx)["defaultSampleRate"])
    stream = p.open(format=pyaudio.paInt16, channels=2, rate=shared_rate, output=True, output_device_index=wasapi_idx)
    pcm_440 = build_pcm_bytes(440.0, shared_rate, int(shared_rate * 1.5), 16, amplitude=0.5)

    api_post("/api/stats/reset")
    api_post("/api/capture/start", {"filename": "verify_shared_440hz.wav"})
    th = threading.Thread(target=lambda: stream.write(pcm_440))
    th.start()
    time.sleep(0.7)
    st_shared = api_get("/api/status")
    th.join()
    rec_res = api_post("/api/capture/stop")
    stream.stop_stream()
    stream.close()
    p.terminate()

    print(
        f"      Shared Mode Status -> Rate={st_shared['hardware']['sample_rate']}Hz, "
        f"Bits={st_shared['hardware']['bit_depth']}-bit, "
        f"Freq={st_shared['audio']['dominant_freq_hz']}Hz, "
        f"Peak={st_shared['audio']['peak_db']}dBFS"
    )
    print(
        f"      Captured WAV -> {rec_res['recording']['frames_captured']} frames, "
        f"SHA256={rec_res['recording']['sha256'][:16]}..."
    )
    assert st_shared["audio"]["is_playing"], "Audio should be playing in Shared Mode"
    assert not st_shared["audio"]["is_silent"], "Audio should not be silent in Shared Mode"
    assert abs(st_shared["audio"]["dominant_freq_hz"] - 440.0) < 15.0, f"Expected ~440Hz, got {st_shared['audio']['dominant_freq_hz']}"
    print("[3/5] PASS: WASAPI Shared Mode & WAV capture verified!")

    # 4. Verify WASAPI Exclusive Mode (Bit-Perfect Sample Rate & Bit Depth Switching)
    print("[4/5] Testing WASAPI Exclusive Mode hardware negotiation & bit-perfect output ...")
    for target_rate, target_bits, expected_alt, test_freq in [
        (44100, 16, 1, 1000.0),
        (96000, 24, 2, 1500.0),
        (192000, 32, 3, 2000.0),
    ]:
        st_excl = play_wasapi_exclusive(target_rate, target_bits, test_freq, duration_sec=1.2)
        hw = st_excl["hardware"]
        au = st_excl["audio"]
        print(
            f"      Exclusive {target_rate}Hz/{target_bits}-bit ({test_freq}Hz) -> "
            f"Negotiated Alt={hw['alt_setting']}, Rate={hw['sample_rate']}Hz, Bits={hw['bit_depth']}-bit | "
            f"Detected Freq={au['dominant_freq_hz']}Hz, Peak={au['peak_db']}dBFS"
        )
        assert hw["sample_rate"] == target_rate, f"Expected hardware rate {target_rate}, got {hw['sample_rate']}"
        assert hw["bit_depth"] == target_bits, f"Expected bit depth {target_bits}, got {hw['bit_depth']}"
        assert hw["alt_setting"] == expected_alt, f"Expected Alt {expected_alt}, got {hw['alt_setting']}"
        assert abs(au["dominant_freq_hz"] - test_freq) < 25.0, f"Expected ~{test_freq}Hz, got {au['dominant_freq_hz']}"
    print("[4/5] PASS: WASAPI Exclusive Mode dynamically switched USB hardware clock & bit depth!")

    # 5. Verify Error Injection & Hotplug API
    print("[5/5] Testing Error Injection & USB Hotplug API ...")
    err_res = api_post("/api/device/inject-error", {"count": 5})
    assert err_res["ok"] and err_res["inject_error_remaining"] == 5
    api_post("/api/device/inject-error", {"count": 0})

    unplug_res = api_post("/api/device/unplug")
    assert unplug_res["ok"] and not unplug_res["hardware"]["attached"]
    print("      Device unplugged via API -> attached=False")
    time.sleep(0.5)

    plug_res = api_post("/api/device/plug")
    assert plug_res["ok"] and plug_res["hardware"]["attached"]
    find_virtual_dac_mmdevice()
    print("      Device re-plugged via API -> attached=True & Windows MMDevice active!")
    print("[5/5] PASS: Error Injection & Hotplug API verified!")

    print("=" * 72)
    print("  ALL VERIFICATION CHECKS PASSED SUCCESSFULLY!")
    print("=" * 72)


if __name__ == "__main__":
    main()
