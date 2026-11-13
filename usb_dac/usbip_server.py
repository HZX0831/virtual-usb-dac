import asyncio
import logging
import struct
import time
from collections import deque
from typing import Dict, Any, List, Optional
from .config import DacConfig
from .uac_descriptors import (
    USB_DT_DEVICE,
    USB_DT_CONFIG,
    USB_DT_STRING,
    USB_DT_DEVICE_QUALIFIER,
    USB_DT_OTHER_SPEED_CONFIG,
    USB_DT_BOS,
    UAC_FEATURE_UNIT_ID,
    UAC2_CLOCK_SOURCE_ID,
    build_device_descriptor,
    build_device_qualifier_descriptor,
    build_bos_descriptor,
    build_config_descriptor,
    build_language_id_descriptor,
    build_string_descriptor,
    build_uac2_clock_range_ParamBlock,
    build_uac2_volume_range_param_block,
)
from .audio_analyzer import AudioStreamAnalyzer

logger = logging.getLogger("usbip_server")

# USB/IP Protocol Constants
USBIP_VERSION = 0x0111
OP_REQ_DEVLIST = 0x8005
OP_REP_DEVLIST = 0x0005
OP_REQ_IMPORT = 0x8003
OP_REP_IMPORT = 0x0003

USBIP_CMD_SUBMIT = 0x00000001
USBIP_CMD_UNLINK = 0x00000002
USBIP_RET_SUBMIT = 0x00000003
USBIP_RET_UNLINK = 0x00000004

USBIP_DIR_OUT = 0
USBIP_DIR_IN = 1

USB_SPEED_FULL = 2
USB_SPEED_HIGH = 3


class VirtualUsbDacServer:
    """Asyncio USB/IP Server emulating a UAC2 / UAC1 Hi-Fi USB DAC."""

    def __init__(self, config: DacConfig, analyzer: AudioStreamAnalyzer) -> None:
        self.config = config
        self.analyzer = analyzer
        self._server: Optional[asyncio.AbstractServer] = None
        self._active_writers: List[asyncio.StreamWriter] = []

        # Hardware negotiated state
        self.attached: bool = False
        self.client_addr: Optional[str] = None
        self.current_config_value: int = 0
        self.interface_alt_setting: Dict[int, int] = {0: 0, 1: 0}
        self.current_sample_rate: int = config.default_sample_rate
        self.current_bit_depth: int = 16
        self.current_subframe_size: int = 2
        self.current_channels: int = 2
        self.mute: bool = False
        self.volume_db: float = 0.0

        # Error injection state
        self.inject_error_remaining: int = 0
        self.inject_error_status: int = -71  # -EPROTO

        # Virtual crystal oscillator clock state for real-time Isochronous pacing
        self._clock_next_urb_time: float = 0.0

        # Control & event logs
        self.usb_logs = deque(maxlen=200)

    def log_usb_event(self, event_type: str, detail: str, extra: Optional[Dict[str, Any]] = None) -> None:
        entry = {
            "timestamp": round(time.time(), 4),
            "time_str": time.strftime("%H:%M:%S"),
            "type": event_type,
            "detail": detail,
        }
        if extra:
            entry.update(extra)
        self.usb_logs.append(entry)
        logger.info("[%s] %s", event_type, detail)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.config.usbip_host, self.config.usbip_port
        )
        self.log_usb_event(
            "SERVER_START",
            f"USB/IP server listening on {self.config.usbip_host}:{self.config.usbip_port} "
            f"(VID=0x{self.config.vid:04X}, PID=0x{self.config.pid:04X}, UAC{self.config.uac_version})",
        )

    async def stop(self) -> None:
        await self.disconnect_active_clients("Server stopping")
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def disconnect_active_clients(self, reason: str = "Simulated unplug") -> int:
        count = len(self._active_writers)
        for writer in list(self._active_writers):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._active_writers.clear()
        if self.attached:
            self.attached = False
            self.interface_alt_setting = {0: 0, 1: 0}
            self.analyzer.on_alt_setting_change(0)
            self.log_usb_event("DEVICE_DETACH", f"Virtual USB DAC detached ({reason})")
        return count

    def inject_errors(self, count: int = 10, errno_status: int = -71) -> None:
        self.inject_error_remaining = max(0, int(count))
        self.inject_error_status = int(errno_status)
        self.log_usb_event(
            "ERROR_INJECT",
            f"Injecting {self.inject_error_remaining} URB errors (status={self.inject_error_status})",
        )

    def _pack_usbip_device_info(self) -> bytes:
        path = f"/sys/devices/pci0000:00/0000:00:01.2/usb{self.config.busnum}/{self.config.bus_id}".encode("ascii")
        busid = self.config.bus_id.encode("ascii")
        if self.config.uac_version == 2:
            dev_class, dev_subclass, dev_proto = 0xEF, 0x02, 0x01
            speed = USB_SPEED_HIGH
        else:
            dev_class, dev_subclass, dev_proto = 0x00, 0x00, 0x00
            speed = USB_SPEED_FULL

        return struct.pack(
            ">256s32sIIIHHHBBBBBB",
            path.ljust(256, b"\x00"),
            busid.ljust(32, b"\x00"),
            self.config.busnum,
            self.config.devnum,
            speed,
            self.config.vid,
            self.config.pid,
            self.config.bcd_device,
            dev_class,
            dev_subclass,
            dev_proto,
            1,  # bConfigurationValue
            1,  # bNumConfigurations
            2,  # bNumInterfaces (AC + AS)
        )

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            hdr = await reader.readexactly(8)
            version, command, status = struct.unpack(">HHI", hdr)
            reply_ver = version if version else USBIP_VERSION

            if command == OP_REQ_DEVLIST:
                dev_info = self._pack_usbip_device_info()
                proto = 0x20 if self.config.uac_version == 2 else 0x00
                if0 = struct.pack(">BBBB", 0x01, 0x01, proto, 0)
                if1 = struct.pack(">BBBB", 0x01, 0x02, proto, 0)
                rep = struct.pack(">HHII", reply_ver, OP_REP_DEVLIST, 0, 1) + dev_info + if0 + if1
                writer.write(rep)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            if command == OP_REQ_IMPORT:
                busid_raw = await reader.readexactly(32)
                busid = busid_raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
                self.log_usb_event("IMPORT_REQ", f"Client {peer} importing busid={busid}")

                if busid != self.config.bus_id:
                    rep = struct.pack(">HHI", reply_ver, OP_REP_IMPORT, 1)
                    writer.write(rep)
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    return

                rep = struct.pack(">HHI", reply_ver, OP_REP_IMPORT, 0) + self._pack_usbip_device_info()
                writer.write(rep)
                await writer.drain()

                self._active_writers.append(writer)
                self.attached = True
                self.client_addr = f"{peer[0]}:{peer[1]}" if peer else "local"
                self.log_usb_event("DEVICE_ATTACH", f"Virtual USB DAC attached to client {self.client_addr}")

                await self._urb_loop(reader, writer)
        except asyncio.IncompleteReadError:
            pass
        except ConnectionResetError:
            pass
        except Exception as exc:
            logger.warning("Client connection error: %s", exc)
        finally:
            if writer in self._active_writers:
                self._active_writers.remove(writer)
            if not self._active_writers and self.attached:
                self.attached = False
                self.interface_alt_setting = {0: 0, 1: 0}
                self.analyzer.on_alt_setting_change(0)
                self.log_usb_event("DEVICE_DETACH", "Virtual USB DAC client disconnected")
            try:
                writer.close()
            except Exception:
                pass

    async def _urb_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            raw_hdr = await reader.readexactly(48)
            command, seqnum, devid, direction, ep = struct.unpack(">IIIII", raw_hdr[:20])

            if command == USBIP_CMD_UNLINK:
                (unlink_seqnum,) = struct.unpack(">I", raw_hdr[20:24])
                # Return RET_UNLINK with status 0 (already completed synchronously)
                ret_hdr = struct.pack(
                    ">IIIIIi24s",
                    USBIP_RET_UNLINK,
                    seqnum,
                    0,
                    0,
                    0,
                    0,
                    b"\x00" * 24,
                )
                writer.write(ret_hdr)
                await writer.drain()
                continue

            if command != USBIP_CMD_SUBMIT:
                logger.warning("Unknown USB/IP command: 0x%08x", command)
                break

            (
                transfer_flags,
                transfer_buffer_length,
                start_frame,
                number_of_packets,
                interval,
                setup,
            ) = struct.unpack(">iiiii8s", raw_hdr[20:48])

            is_isoch = 0 < number_of_packets <= 1024

            # Read transfer buffer if OUT
            out_data = b""
            if direction == USBIP_DIR_OUT and transfer_buffer_length > 0:
                out_data = await reader.readexactly(transfer_buffer_length)

            # Read iso_packet_descriptor array if Isochronous
            iso_raw = b""
            if is_isoch:
                iso_raw = await reader.readexactly(number_of_packets * 16)

            if ep == 0:
                status, resp_data = self._handle_control_transfer(
                    setup, direction, out_data, transfer_buffer_length
                )
                if direction == USBIP_DIR_IN:
                    actual_len = len(resp_data)
                    payload = resp_data
                else:
                    actual_len = len(out_data)
                    payload = b""

                ret_num_pkts = number_of_packets if is_isoch else -1
                ret_hdr = struct.pack(
                    ">IIIIIiiiii8s",
                    USBIP_RET_SUBMIT,
                    seqnum,
                    0,
                    0,
                    0,
                    status,
                    actual_len,
                    start_frame,
                    ret_num_pkts,
                    0,
                    b"\x00" * 8,
                )
                writer.write(ret_hdr + payload)
                await writer.drain()

            elif is_isoch:
                ret_pkt, delay_sec = self._handle_isoch_transfer(
                    seqnum,
                    direction,
                    ep,
                    transfer_buffer_length,
                    start_frame,
                    number_of_packets,
                    out_data,
                    iso_raw,
                )
                if delay_sec > 0.001:
                    await asyncio.sleep(delay_sec)
                writer.write(ret_pkt)
                await writer.drain()

            else:
                # Non-isoch non-EP0 transfer fallback
                actual_len = len(out_data) if direction == USBIP_DIR_OUT else 0
                ret_hdr = struct.pack(
                    ">IIIIIiiiii8s",
                    USBIP_RET_SUBMIT,
                    seqnum,
                    0,
                    0,
                    0,
                    0,
                    actual_len,
                    start_frame,
                    -1,
                    0,
                    b"\x00" * 8,
                )
                writer.write(ret_hdr)
                await writer.drain()

    def _handle_control_transfer(
        self, setup: bytes, direction: int, out_data: bytes, max_len: int
    ):
        bmRequestType, bRequest, wValue, wIndex, wLength = struct.unpack("<BBHHH", setup)
        req_type = (bmRequestType >> 5) & 0x03
        recipient = bmRequestType & 0x1F

        # 1. Standard USB Requests (req_type == 0)
        if req_type == 0:
            if bRequest == 0x00:  # GET_STATUS
                return 0, b"\x00\x00"[:wLength]
            if bRequest in (0x01, 0x03):  # CLEAR_FEATURE / SET_FEATURE
                return 0, b""
            if bRequest == 0x06:  # GET_DESCRIPTOR
                desc_type = (wValue >> 8) & 0xFF
                desc_idx = wValue & 0xFF
                data = self._get_descriptor(desc_type, desc_idx)
                if data is None:
                    return -32, b""  # -EPIPE (STALL)
                return 0, data[:wLength]
            if bRequest == 0x08:  # GET_CONFIGURATION
                return 0, struct.pack("<B", max(1, self.current_config_value))[:wLength]
            if bRequest == 0x09:  # SET_CONFIGURATION
                self.current_config_value = wValue & 0xFF
                self.log_usb_event("SET_CONFIG", f"Configuration set to {self.current_config_value}")
                return 0, b""
            if bRequest == 0x0A:  # GET_INTERFACE
                iface = wIndex & 0xFF
                alt = self.interface_alt_setting.get(iface, 0)
                return 0, struct.pack("<B", alt)[:wLength]
            if bRequest == 0x0B:  # SET_INTERFACE
                iface = wIndex & 0xFF
                alt = wValue & 0xFF
                self.interface_alt_setting[iface] = alt
                if iface == 1:
                    self.analyzer.on_alt_setting_change(alt)
                    if alt > 0:
                        spec = self.config.get_alt_spec(alt)
                        self.current_bit_depth = spec.bit_resolution
                        self.current_subframe_size = spec.subframe_size
                        self.current_channels = spec.channels
                        self.log_usb_event(
                            "SET_INTERFACE",
                            f"AudioStreaming Alt={alt} active ({self.current_bit_depth}-bit, "
                            f"{self.current_channels}ch @ {self.current_sample_rate}Hz)",
                            {
                                "alt_setting": alt,
                                "bit_depth": self.current_bit_depth,
                                "sample_rate": self.current_sample_rate,
                            },
                        )
                    else:
                        self.log_usb_event(
                            "SET_INTERFACE",
                            "AudioStreaming Alt=0 (Zero-bandwidth / Idle)",
                            {"alt_setting": 0},
                        )
                return 0, b""

        # 2. Class-Specific USB Audio Requests (req_type == 1)
        elif req_type == 1:
            if self.config.uac_version == 2:
                return self._handle_uac2_class_request(
                    bmRequestType, bRequest, wValue, wIndex, wLength, out_data
                )
            else:
                return self._handle_uac1_class_request(
                    bmRequestType, bRequest, wValue, wIndex, wLength, out_data
                )

        return 0, b"\x00" * min(wLength, max_len) if direction == USBIP_DIR_IN else b""

    def _get_descriptor(self, desc_type: int, desc_idx: int) -> Optional[bytes]:
        if desc_type == USB_DT_DEVICE:
            return build_device_descriptor(self.config)
        if desc_type in (USB_DT_CONFIG, USB_DT_OTHER_SPEED_CONFIG):
            return build_config_descriptor(self.config)
        if desc_type == USB_DT_DEVICE_QUALIFIER:
            return build_device_qualifier_descriptor(self.config)
        if desc_type == USB_DT_BOS:
            return build_bos_descriptor()
        if desc_type == USB_DT_STRING:
            if desc_idx == 0:
                return build_language_id_descriptor()
            if desc_idx == 1:
                return build_string_descriptor(self.config.manufacturer)
            if desc_idx == 2:
                return build_string_descriptor(self.config.product)
            if desc_idx == 3:
                return build_string_descriptor(self.config.serial)
            return build_string_descriptor("Virtual Hi-Fi USB DAC")
        return None

    def _handle_uac2_class_request(
        self, bmRequestType: int, bRequest: int, wValue: int, wIndex: int, wLength: int, out_data: bytes
    ):
        is_get = bool(bmRequestType & 0x80)
        entity_id = (wIndex >> 8) & 0xFF
        cs = (wValue >> 8) & 0xFF  # Control Selector

        # Clock Source Entity (ID 0x10)
        if entity_id == UAC2_CLOCK_SOURCE_ID:
            if cs == 0x01:  # CS_SAM_FREQ_CONTROL
                if bRequest == 0x01:  # CUR
                    if is_get:
                        return 0, struct.pack("<I", self.current_sample_rate)[:wLength]
                    elif len(out_data) >= 4:
                        (new_rate,) = struct.unpack("<I", out_data[:4])
                        self.current_sample_rate = new_rate
                        self.log_usb_event(
                            "CLOCK_RATE_SET",
                            f"UAC2 Clock Sample Rate set to {new_rate} Hz",
                            {"sample_rate": new_rate},
                        )
                        return 0, b""
                elif bRequest == 0x02 and is_get:  # RANGE
                    block = build_uac2_clock_range_ParamBlock(self.config.sample_rates)
                    return 0, block[:wLength]
            elif cs == 0x02 and is_get:  # CS_CLOCK_VALID_CONTROL
                return 0, b"\x01"[:wLength]

        # Feature Unit Entity (ID 0x02)
        elif entity_id == UAC_FEATURE_UNIT_ID:
            if cs == 0x01:  # FU_MUTE_CONTROL
                if bRequest == 0x01:  # CUR
                    if is_get:
                        return 0, struct.pack("<B", 1 if self.mute else 0)[:wLength]
                    elif len(out_data) >= 1:
                        self.mute = bool(out_data[0])
                        self.log_usb_event("MUTE_SET", f"Hardware Mute set to {self.mute}", {"mute": self.mute})
                        return 0, b""
            elif cs == 0x02:  # FU_VOLUME_CONTROL
                if bRequest == 0x01:  # CUR
                    if is_get:
                        raw_vol = int(max(-32768, min(32767, round(self.volume_db * 256.0))))
                        return 0, struct.pack("<h", raw_vol)[:wLength]
                    elif len(out_data) >= 2:
                        (raw_vol,) = struct.unpack("<h", out_data[:2])
                        self.volume_db = round(raw_vol / 256.0, 2)
                        self.log_usb_event(
                            "VOLUME_SET", f"Hardware Volume set to {self.volume_db} dB", {"volume_db": self.volume_db}
                        )
                        return 0, b""
                elif bRequest == 0x02 and is_get:  # RANGE
                    return 0, build_uac2_volume_range_param_block()[:wLength]

        # Default fallback for other class queries
        if is_get:
            return 0, b"\x00" * wLength
        return 0, b""

    def _handle_uac1_class_request(
        self, bmRequestType: int, bRequest: int, wValue: int, wIndex: int, wLength: int, out_data: bytes
    ):
        is_get = bool(bmRequestType & 0x80)
        recipient = bmRequestType & 0x1F
        cs = (wValue >> 8) & 0xFF

        # Endpoint Sample Rate Control (EP 0x01)
        if recipient == 0x02 and cs == 0x01:
            if not is_get and bRequest == 0x01 and len(out_data) >= 3:
                new_rate = out_data[0] | (out_data[1] << 8) | (out_data[2] << 16)
                self.current_sample_rate = new_rate
                self.log_usb_event(
                    "CLOCK_RATE_SET",
                    f"UAC1 Endpoint Sample Rate set to {new_rate} Hz",
                    {"sample_rate": new_rate},
                )
                return 0, b""
            elif is_get:
                return 0, struct.pack("<I", self.current_sample_rate)[:3][:wLength]

        # Feature Unit (ID 0x02)
        entity_id = (wIndex >> 8) & 0xFF
        if entity_id == UAC_FEATURE_UNIT_ID:
            if cs == 0x01:  # Mute
                if is_get:
                    return 0, struct.pack("<B", 1 if self.mute else 0)[:wLength]
                elif len(out_data) >= 1:
                    self.mute = bool(out_data[0])
                    self.log_usb_event("MUTE_SET", f"Hardware Mute set to {self.mute}", {"mute": self.mute})
                    return 0, b""
            elif cs == 0x02:  # Volume
                if not is_get and len(out_data) >= 2:
                    (raw_vol,) = struct.unpack("<h", out_data[:2])
                    self.volume_db = round(raw_vol / 256.0, 2)
                    self.log_usb_event(
                        "VOLUME_SET", f"Hardware Volume set to {self.volume_db} dB", {"volume_db": self.volume_db}
                    )
                    return 0, b""
                elif is_get:
                    if bRequest == 0x82:  # GET_MIN
                        return 0, struct.pack("<h", -0x6000)[:wLength]
                    if bRequest == 0x83:  # GET_MAX
                        return 0, struct.pack("<h", 0x0000)[:wLength]
                    if bRequest == 0x84:  # GET_RES
                        return 0, struct.pack("<h", 0x0080)[:wLength]
                    raw_vol = int(max(-32768, min(32767, round(self.volume_db * 256.0))))
                    return 0, struct.pack("<h", raw_vol)[:wLength]

        return 0, (b"\x00" * wLength) if is_get else b""

    def _handle_isoch_transfer(
        self,
        seqnum: int,
        direction: int,
        ep: int,
        transfer_buffer_length: int,
        start_frame: int,
        number_of_packets: int,
        out_data: bytes,
        iso_raw: bytes,
    ) -> bytes:
        inject_err = False
        if self.inject_error_remaining > 0:
            self.inject_error_remaining -= 1
            inject_err = True

        ret_iso_descs = bytearray()
        extracted_pcm = bytearray()

        for i in range(number_of_packets):
            off, length, actual_len, pkt_status = struct.unpack(
                ">IIII", iso_raw[i * 16 : (i + 1) * 16]
            )
            if direction == USBIP_DIR_OUT and not inject_err:
                pkt_end = min(len(out_data), off + length)
                if off < pkt_end:
                    extracted_pcm.extend(out_data[off:pkt_end])
                ret_iso_descs.extend(struct.pack(">IIII", off, length, length, 0))
            else:
                err_code = self.inject_error_status & 0xFFFFFFFF if inject_err else 0
                ret_iso_descs.extend(struct.pack(">IIII", off, length, 0, err_code))

        if extracted_pcm and not inject_err:
            self.analyzer.feed_pcm_packet(
                bytes(extracted_pcm),
                sample_rate=self.current_sample_rate,
                subframe_size=self.current_subframe_size,
                bit_resolution=self.current_bit_depth,
                channels=self.current_channels,
            )

        # Calculate real-time virtual crystal oscillator pacing delay
        frame_stride = max(1, self.current_subframe_size * self.current_channels)
        num_frames = len(extracted_pcm) // frame_stride
        if num_frames > 0 and self.current_sample_rate > 0:
            urb_duration = num_frames / float(self.current_sample_rate)
        else:
            uframe_sec = 0.000125 if self.config.uac_version == 2 else 0.001
            urb_duration = number_of_packets * uframe_sec

        now = time.perf_counter()
        if self._clock_next_urb_time < now - 0.02:
            self._clock_next_urb_time = now
        self._clock_next_urb_time += urb_duration
        delay_sec = max(0.0, self._clock_next_urb_time - now)

        urb_status = self.inject_error_status if inject_err else 0
        error_count = number_of_packets if inject_err else 0
        actual_total = 0 if inject_err else transfer_buffer_length

        ret_hdr = struct.pack(
            ">IIIIIiiiii8s",
            USBIP_RET_SUBMIT,
            seqnum,
            0,
            0,
            0,
            urb_status,
            actual_total,
            start_frame,
            number_of_packets,
            error_count,
            b"\x00" * 8,
        )
        return ret_hdr + bytes(ret_iso_descs), delay_sec

    def get_hardware_state(self) -> Dict[str, Any]:
        return {
            "attached": self.attached,
            "client_addr": self.client_addr,
            "uac_version": self.config.uac_version,
            "vid": f"0x{self.config.vid:04X}",
            "pid": f"0x{self.config.pid:04X}",
            "manufacturer": self.config.manufacturer,
            "product": self.config.product,
            "serial": self.config.serial,
            "bus_id": self.config.bus_id,
            "alt_setting": self.interface_alt_setting.get(1, 0),
            "sample_rate": self.current_sample_rate,
            "bit_depth": self.current_bit_depth,
            "subframe_size": self.current_subframe_size,
            "channels": self.current_channels,
            "mute": self.mute,
            "volume_db": self.volume_db,
            "supported_sample_rates": self.config.sample_rates,
            "inject_error_remaining": self.inject_error_remaining,
        }
