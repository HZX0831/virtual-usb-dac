from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any


@dataclass
class AltSettingSpec:
    alt_setting: int
    channels: int
    subframe_size: int  # bytes per sample (2, 3, or 4)
    bit_resolution: int  # 16, 24, or 32
    max_packet_size: int = 1024


@dataclass
class DacConfig:
    vid: int = 0x1209
    pid: int = 0xF001
    bcd_device: int = 0x0200
    manufacturer: str = "Antigravity Audio Lab"
    product: str = "Virtual Hi-Fi USB DAC"
    serial: str = "AGY-DAC-0001"
    uac_version: int = 2  # 2 for UAC 2.0 (usbaudio2.sys), 1 for UAC 1.0 (usbaudio.sys)
    sample_rates: List[int] = field(
        default_factory=lambda: [44100, 48000, 88200, 96000, 176400, 192000]
    )
    default_sample_rate: int = 48000
    alt_settings: List[AltSettingSpec] = field(
        default_factory=lambda: [
            AltSettingSpec(alt_setting=1, channels=2, subframe_size=2, bit_resolution=16, max_packet_size=1024),
            AltSettingSpec(alt_setting=2, channels=2, subframe_size=3, bit_resolution=24, max_packet_size=1024),
            AltSettingSpec(alt_setting=3, channels=2, subframe_size=4, bit_resolution=32, max_packet_size=1024),
        ]
    )
    usbip_host: str = "127.0.0.1"
    usbip_port: int = 3240
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    bus_id: str = "1-1"
    busnum: int = 1
    devnum: int = 1

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["vid_hex"] = f"0x{self.vid:04X}"
        d["pid_hex"] = f"0x{self.pid:04X}"
        return d

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        if "vid" in data:
            val = data["vid"]
            self.vid = int(val, 16) if isinstance(val, str) else int(val)
        if "pid" in data:
            val = data["pid"]
            self.pid = int(val, 16) if isinstance(val, str) else int(val)
        if "manufacturer" in data:
            self.manufacturer = str(data["manufacturer"])
        if "product" in data:
            self.product = str(data["product"])
        if "serial" in data:
            self.serial = str(data["serial"])
        if "uac_version" in data:
            self.uac_version = int(data["uac_version"])
        if "default_sample_rate" in data:
            self.default_sample_rate = int(data["default_sample_rate"])
        if "sample_rates" in data and isinstance(data["sample_rates"], list):
            self.sample_rates = [int(r) for r in data["sample_rates"]]

    def get_alt_spec(self, alt_setting: int) -> AltSettingSpec:
        for spec in self.alt_settings:
            if spec.alt_setting == alt_setting:
                return spec
        return self.alt_settings[0]
