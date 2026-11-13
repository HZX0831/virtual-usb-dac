import asyncio
import math
import os
import struct
import tempfile
import unittest
import aiohttp
import numpy as np

from usb_dac.config import DacConfig
from usb_dac.uac_descriptors import (
    build_device_descriptor,
    build_uac2_config_descriptor,
    build_uac1_config_descriptor,
    build_uac2_clock_range_ParamBlock,
    UAC2_CLOCK_SOURCE_ID,
)
from usb_dac.audio_analyzer import AudioStreamAnalyzer
from usb_dac.usbip_server import (
    VirtualUsbDacServer,
    USBIP_VERSION,
    OP_REQ_DEVLIST,
    OP_REP_DEVLIST,
    OP_REQ_IMPORT,
    OP_REP_IMPORT,
    USBIP_CMD_SUBMIT,
    USBIP_RET_SUBMIT,
    USBIP_DIR_OUT,
    USBIP_DIR_IN,
)
from usb_dac.api_server import VerificationApiServer


def generate_sine_pcm(freq_hz: float, sample_rate: int, duration_sec: float, bit_depth: int, amplitude: float = 0.5) -> bytes:
    num_samples = int(sample_rate * duration_sec)
    t = np.arange(num_samples, dtype=np.float64) / sample_rate
    mono = amplitude * np.sin(2.0 * math.pi * freq_hz * t)
    stereo = np.column_stack((mono, mono)).flatten()

    if bit_depth == 16:
        pcm16 = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")
        return pcm16.tobytes()
    elif bit_depth == 24:
        pcm32 = np.clip(stereo * 8388607.0, -8388608, 8388607).astype(np.int32)
        u32 = pcm32 & 0xFFFFFF
        b0 = (u32 & 0xFF).astype(np.uint8)
        b1 = ((u32 >> 8) & 0xFF).astype(np.uint8)
        b2 = ((u32 >> 16) & 0xFF).astype(np.uint8)
        packed = np.column_stack((b0, b1, b2)).flatten()
        return packed.tobytes()
    elif bit_depth == 32:
        pcm32 = np.clip(stereo * 2147483647.0, -2147483648, 2147483647).astype("<i4")
        return pcm32.tobytes()
    raise ValueError(f"Unsupported bit_depth: {bit_depth}")


class TestUsbDacUnit(unittest.TestCase):
    def test_descriptors_validity(self):
        cfg = DacConfig()
        dev_desc = build_device_descriptor(cfg)
        self.assertEqual(len(dev_desc), 18)
        self.assertEqual(dev_desc[0], 18)
        self.assertEqual(dev_desc[1], 1)

        uac2_cfg = build_uac2_config_descriptor(cfg)
        _, _, total_len_uac2 = struct.unpack("<BBH", uac2_cfg[:4])
        self.assertEqual(total_len_uac2, len(uac2_cfg))
        offset = 0
        while offset < len(uac2_cfg):
            blen = uac2_cfg[offset]
            self.assertGreater(blen, 0)
            offset += blen
        self.assertEqual(offset, len(uac2_cfg))

        uac1_cfg = build_uac1_config_descriptor(cfg)
        _, _, total_len_uac1 = struct.unpack("<BBH", uac1_cfg[:4])
        self.assertEqual(total_len_uac1, len(uac1_cfg))
        offset = 0
        while offset < len(uac1_cfg):
            blen = uac1_cfg[offset]
            self.assertGreater(blen, 0)
            offset += blen
        self.assertEqual(offset, len(uac1_cfg))

        range_block = build_uac2_clock_range_ParamBlock(cfg.sample_rates)
        (num_ranges,) = struct.unpack("<H", range_block[:2])
        self.assertEqual(num_ranges, len(cfg.sample_rates))
        self.assertEqual(len(range_block), 2 + 12 * len(cfg.sample_rates))

    def test_audio_analyzer_16_24_32bit_and_fft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            analyzer = AudioStreamAnalyzer(captures_dir=tmpdir)
            for bits, subsize, rate, target_freq in [
                (16, 2, 48000, 1000.0),
                (24, 3, 96000, 1500.0),
                (32, 4, 192000, 2000.0),
            ]:
                analyzer.reset_stats()
                analyzer.start_recording(sample_rate=rate, bit_resolution=bits, channels=2)
                pcm = generate_sine_pcm(target_freq, rate, 0.15, bits, amplitude=0.5)
                analyzer.feed_pcm_packet(pcm, sample_rate=rate, subframe_size=subsize, bit_resolution=bits, channels=2)
                snap = analyzer.get_snapshot()
                rec_info = analyzer.stop_recording()

                self.assertTrue(snap["is_playing"])
                self.assertFalse(snap["is_silent"])
                self.assertAlmostEqual(snap["dominant_freq_hz"], target_freq, delta=5.0)
                # Amplitude 0.5 sine wave -> Peak ~ -6.02 dBFS, RMS ~ -9.03 dBFS
                self.assertAlmostEqual(snap["peak_db"], -6.02, delta=0.5)
                self.assertAlmostEqual(snap["rms_db"], -9.03, delta=0.5)
                self.assertTrue(os.path.isfile(rec_info["path"]))
                self.assertGreater(rec_info["frames_captured"], 0)

    def test_usbip_and_api_end_to_end_protocol(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                cfg = DacConfig(usbip_port=13240, api_port=18765)
                analyzer = AudioStreamAnalyzer(captures_dir=tmpdir)
                dac_server = VirtualUsbDacServer(config=cfg, analyzer=analyzer)
                api_server = VerificationApiServer(config=cfg, dac_server=dac_server, analyzer=analyzer)

                await dac_server.start()
                await api_server.start()
                try:
                    # 1. Test OP_REQ_DEVLIST
                    reader, writer = await asyncio.open_connection("127.0.0.1", 13240)
                    writer.write(struct.pack(">HHI", USBIP_VERSION, OP_REQ_DEVLIST, 0))
                    await writer.drain()
                    rep_hdr = await reader.readexactly(12)
                    ver, rep_cmd, status, ndev = struct.unpack(">HHII", rep_hdr)
                    self.assertEqual(rep_cmd, OP_REP_DEVLIST)
                    self.assertEqual(status, 0)
                    self.assertEqual(ndev, 1)
                    dev_raw = await reader.readexactly(312 + 8)
                    writer.close()
                    await writer.wait_closed()

                    # 2. Test OP_REQ_IMPORT & URB stream
                    reader, writer = await asyncio.open_connection("127.0.0.1", 13240)
                    busid_bytes = b"1-1".ljust(32, b"\x00")
                    writer.write(struct.pack(">HHI", USBIP_VERSION, OP_REQ_IMPORT, 0) + busid_bytes)
                    await writer.drain()
                    imp_hdr = await reader.readexactly(8 + 312)
                    ver, rep_cmd, status = struct.unpack(">HHI", imp_hdr[:8])
                    self.assertEqual(rep_cmd, OP_REP_IMPORT)
                    self.assertEqual(status, 0)

                    # 3. Send SET_INTERFACE Alt=2 (24-bit) on Interface 1
                    setup_set_if = struct.pack("<BBHHH", 0x01, 0x0B, 2, 1, 0)
                    cmd_set_if = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_CMD_SUBMIT, 1, 1, USBIP_DIR_OUT, 0,
                        0, 0, 0, -1, 0, setup_set_if
                    )
                    writer.write(cmd_set_if)
                    await writer.drain()
                    ret_set_if = await reader.readexactly(48)
                    ret_cmd, ret_seq = struct.unpack(">II", ret_set_if[:8])
                    self.assertEqual(ret_cmd, USBIP_RET_SUBMIT)
                    self.assertEqual(ret_seq, 1)

                    # 4. Send UAC2 Clock Sample Rate = 96000 Hz
                    setup_clk = struct.pack("<BBHHH", 0x21, 0x01, 0x0100, (UAC2_CLOCK_SOURCE_ID << 8) | 0, 4)
                    clk_payload = struct.pack("<I", 96000)
                    cmd_clk = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_CMD_SUBMIT, 2, 1, USBIP_DIR_OUT, 0,
                        0, 4, 0, -1, 0, setup_clk
                    )
                    writer.write(cmd_clk + clk_payload)
                    await writer.drain()
                    await reader.readexactly(48)

                    # 5. Send Isochronous OUT packets on EP 1 with 96kHz 24-bit 1000Hz tone
                    pcm_data = generate_sine_pcm(1000.0, 96000, 0.1, 24, amplitude=0.5)
                    num_pkts = 8
                    pkt_size = len(pcm_data) // num_pkts
                    trimmed_len = pkt_size * num_pkts
                    pcm_data = pcm_data[:trimmed_len]
                    iso_descs = b"".join(
                        struct.pack(">IIII", i * pkt_size, pkt_size, 0, 0)
                        for i in range(num_pkts)
                    )
                    cmd_iso = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_CMD_SUBMIT, 3, 1, USBIP_DIR_OUT, 1,
                        0, trimmed_len, 100, num_pkts, 1, b"\x00" * 8
                    )
                    writer.write(cmd_iso + pcm_data + iso_descs)
                    await writer.drain()
                    ret_iso = await reader.readexactly(48 + num_pkts * 16)
                    ret_cmd, ret_seq = struct.unpack(">II", ret_iso[:8])
                    self.assertEqual(ret_cmd, USBIP_RET_SUBMIT)
                    self.assertEqual(ret_seq, 3)

                    # 6. Query HTTP /api/status to verify hardware & audio metrics
                    async with aiohttp.ClientSession() as session:
                        async with session.get("http://127.0.0.1:18765/api/status") as resp:
                            self.assertEqual(resp.status, 200)
                            st = await resp.json()
                            self.assertTrue(st["hardware"]["attached"])
                            self.assertEqual(st["hardware"]["alt_setting"], 2)
                            self.assertEqual(st["hardware"]["bit_depth"], 24)
                            self.assertEqual(st["hardware"]["sample_rate"], 96000)
                            self.assertTrue(st["audio"]["is_playing"])
                            self.assertFalse(st["audio"]["is_silent"])
                            self.assertAlmostEqual(st["audio"]["dominant_freq_hz"], 1000.0, delta=5.0)

                    writer.close()
                    await writer.wait_closed()
                finally:
                    await api_server.stop()
                    await dac_server.stop()

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
