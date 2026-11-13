import struct
from typing import Dict, Tuple, List
from .config import DacConfig, AltSettingSpec

# USB Standard Descriptor Types
USB_DT_DEVICE = 0x01
USB_DT_CONFIG = 0x02
USB_DT_STRING = 0x03
USB_DT_INTERFACE = 0x04
USB_DT_ENDPOINT = 0x05
USB_DT_DEVICE_QUALIFIER = 0x06
USB_DT_OTHER_SPEED_CONFIG = 0x07
USB_DT_INTERFACE_ASSOCIATION = 0x0B
USB_DT_BOS = 0x0F

# USB Audio Class-Specific Descriptor Types
USB_DT_CS_INTERFACE = 0x24
USB_DT_CS_ENDPOINT = 0x25

# Entity IDs inside AudioControl Interface (Interface 0)
UAC_INPUT_TERMINAL_ID = 0x01
UAC_FEATURE_UNIT_ID = 0x02
UAC_OUTPUT_TERMINAL_ID = 0x03
UAC2_CLOCK_SOURCE_ID = 0x10


def build_string_descriptor(text: str) -> bytes:
    encoded = text.encode("utf-16-le")
    length = min(254, len(encoded) + 2)
    return struct.pack("<BB", length, USB_DT_STRING) + encoded[: length - 2]


def build_language_id_descriptor() -> bytes:
    # English (United States) 0x0409
    return struct.pack("<BBH", 4, USB_DT_STRING, 0x0409)


def build_device_descriptor(config: DacConfig) -> bytes:
    if config.uac_version == 2:
        # UAC 2.0 uses Interface Association Descriptor (IAD): Misc / Common / IAD
        dev_class, dev_subclass, dev_proto = 0xEF, 0x02, 0x01
    else:
        # UAC 1.0 uses class defined at interface level
        dev_class, dev_subclass, dev_proto = 0x00, 0x00, 0x00

    return struct.pack(
        "<BBHBBBBHHHBBBB",
        18,                 # bLength
        USB_DT_DEVICE,      # bDescriptorType
        0x0200,             # bcdUSB (USB 2.0)
        dev_class,          # bDeviceClass
        dev_subclass,       # bDeviceSubClass
        dev_proto,          # bDeviceProtocol
        64,                 # bMaxPacketSize0
        config.vid,         # idVendor
        config.pid,         # idProduct
        config.bcd_device,  # bcdDevice
        1,                  # iManufacturer
        2,                  # iProduct
        3,                  # iSerialNumber
        1,                  # bNumConfigurations
    )


def build_device_qualifier_descriptor(config: DacConfig) -> bytes:
    if config.uac_version == 2:
        dev_class, dev_subclass, dev_proto = 0xEF, 0x02, 0x01
    else:
        dev_class, dev_subclass, dev_proto = 0x00, 0x00, 0x00

    return struct.pack(
        "<BBHBBBBBB",
        10,                         # bLength
        USB_DT_DEVICE_QUALIFIER,    # bDescriptorType
        0x0200,                     # bcdUSB
        dev_class,
        dev_subclass,
        dev_proto,
        64,                         # bMaxPacketSize0
        1,                          # bNumConfigurations
        0,                          # bReserved
    )


def build_bos_descriptor() -> bytes:
    # Minimal USB 2.0 Extension BOS descriptor (12 bytes total)
    bos_hdr = struct.pack("<BBHB", 5, USB_DT_BOS, 12, 1)
    usb2_ext = struct.pack("<BBBI", 7, 0x10, 0x02, 0x00000002)
    return bos_hdr + usb2_ext


def build_uac2_config_descriptor(config: DacConfig) -> bytes:
    """Build full USB Audio Class 2.0 Configuration Descriptor tree for Windows usbaudio2.sys."""
    # 1. Interface Association Descriptor (8 bytes)
    iad = struct.pack(
        "<BBBBBBBB",
        8,                              # bLength
        USB_DT_INTERFACE_ASSOCIATION,   # bDescriptorType
        0,                              # bFirstInterface
        2,                              # bInterfaceCount (AC=0, AS=1)
        0x01,                           # bFunctionClass (AUDIO)
        0x00,                           # bFunctionSubClass (UNDEFINED)
        0x20,                           # bFunctionProtocol (AF_VERSION_02_00)
        2,                              # iFunction (Product string)
    )

    # 2. Standard AudioControl Interface 0 Descriptor (9 bytes)
    ac_if = struct.pack(
        "<BBBBBBBBB",
        9,                  # bLength
        USB_DT_INTERFACE,   # bDescriptorType
        0,                  # bInterfaceNumber
        0,                  # bAlternateSetting
        0,                  # bNumEndpoints
        0x01,               # bInterfaceClass (AUDIO)
        0x01,               # bInterfaceSubClass (AUDIOCONTROL)
        0x20,               # bInterfaceProtocol (IP_VERSION_02_00)
        2,                  # iInterface
    )

    # 3. Class-Specific AC Interface Descriptors
    # Clock Source Descriptor (8 bytes)
    clock_src = struct.pack(
        "<BBBBBBBB",
        8,                      # bLength
        USB_DT_CS_INTERFACE,    # bDescriptorType
        0x0A,                   # bDescriptorSubtype (CLOCK_SOURCE)
        UAC2_CLOCK_SOURCE_ID,   # bClockID (0x10)
        0x03,                   # bmAttributes (Internal Programmable Clock)
        0x07,                   # bmControls (SamFreq R/W = 0b11, Validity R = 0b0100 -> 0x07)
        0x00,                   # bAssocTerminal
        0x00,                   # iClockSource
    )

    # Input Terminal Descriptor (17 bytes)
    input_term = struct.pack(
        "<BBBBHBBBIBHB",
        17,                     # bLength
        USB_DT_CS_INTERFACE,    # bDescriptorType
        0x02,                   # bDescriptorSubtype (INPUT_TERMINAL)
        UAC_INPUT_TERMINAL_ID,  # bTerminalID (1)
        0x0101,                 # wTerminalType (USB Streaming)
        0x00,                   # bAssocTerminal
        UAC2_CLOCK_SOURCE_ID,   # bCSourceID (0x10)
        2,                      # bNrChannels (2)
        0x00000003,             # bmChannelConfig (FL | FR)
        0,                      # iChannelNames
        0x0000,                 # bmControls
        0,                      # iTerminal
    )

    # Feature Unit Descriptor (18 bytes for 2 channels)
    feature_unit = struct.pack(
        "<BBBBBIIIB",
        18,                     # bLength
        USB_DT_CS_INTERFACE,    # bDescriptorType
        0x06,                   # bDescriptorSubtype (FEATURE_UNIT)
        UAC_FEATURE_UNIT_ID,    # bUnitID (2)
        UAC_INPUT_TERMINAL_ID,  # bSourceID (1)
        0x0000000F,             # bmaControls(0) Master: Mute R/W (0x03) | Volume R/W (0x0C)
        0x00000000,             # bmaControls(1) Ch1
        0x00000000,             # bmaControls(2) Ch2
        0,                      # iFeature
    )

    # Output Terminal Descriptor (12 bytes)
    output_term = struct.pack(
        "<BBBBHBBGHB".replace("G", "B"),
        12,                     # bLength
        USB_DT_CS_INTERFACE,    # bDescriptorType
        0x03,                   # bDescriptorSubtype (OUTPUT_TERMINAL)
        UAC_OUTPUT_TERMINAL_ID, # bTerminalID (3)
        0x0301,                 # wTerminalType (Speaker)
        0x00,                   # bAssocTerminal
        UAC_FEATURE_UNIT_ID,    # bSourceID (2)
        UAC2_CLOCK_SOURCE_ID,   # bCSourceID (0x10)
        0x0000,                 # bmControls
        0,                      # iTerminal
    )

    ac_cs_body = clock_src + input_term + feature_unit + output_term
    ac_total_len = 9 + len(ac_cs_body)
    ac_hdr = struct.pack(
        "<BBBHBHB",
        9,                      # bLength
        USB_DT_CS_INTERFACE,    # bDescriptorType
        0x01,                   # bDescriptorSubtype (HEADER)
        0x0200,                 # bcdADC (2.00)
        0x01,                   # bCategory (DESKTOP_SPEAKER)
        ac_total_len,           # wTotalLength
        0x00,                   # bmControls
    )

    # 4. AudioStreaming Interface 1, Alt 0 (Zero Bandwidth, 9 bytes)
    as_alt0 = struct.pack(
        "<BBBBBBBBB",
        9,                  # bLength
        USB_DT_INTERFACE,   # bDescriptorType
        1,                  # bInterfaceNumber
        0,                  # bAlternateSetting
        0,                  # bNumEndpoints
        0x01,               # bInterfaceClass (AUDIO)
        0x02,               # bInterfaceSubClass (AUDIOSTREAMING)
        0x20,               # bInterfaceProtocol (IP_VERSION_02_00)
        0,                  # iInterface
    )

    # 5. AudioStreaming Interface 1, Operational Alt Settings (Alt 1..N)
    as_alts = []
    for spec in config.alt_settings:
        std_if = struct.pack(
            "<BBBBBBBBB",
            9,                  # bLength
            USB_DT_INTERFACE,   # bDescriptorType
            1,                  # bInterfaceNumber
            spec.alt_setting,   # bAlternateSetting
            1,                  # bNumEndpoints
            0x01,               # bInterfaceClass (AUDIO)
            0x02,               # bInterfaceSubClass (AUDIOSTREAMING)
            0x20,               # bInterfaceProtocol (IP_VERSION_02_00)
            0,                  # iInterface
        )
        cs_as_gen = struct.pack(
            "<BBBBBBIBIB",
            16,                     # bLength
            USB_DT_CS_INTERFACE,    # bDescriptorType
            0x01,                   # bDescriptorSubtype (AS_GENERAL)
            UAC_INPUT_TERMINAL_ID,  # bTerminalLink (1)
            0x00,                   # bmControls
            0x01,                   # bFormatType (FORMAT_TYPE_I)
            0x00000001,             # bmFormats (PCM)
            spec.channels,          # bNrChannels (2)
            0x00000003,             # bmChannelConfig (FL | FR)
            0,                      # iChannelNames
        )
        cs_fmt = struct.pack(
            "<BBBBBB",
            6,                      # bLength
            USB_DT_CS_INTERFACE,    # bDescriptorType
            0x02,                   # bDescriptorSubtype (FORMAT_TYPE)
            0x01,                   # bFormatType (FORMAT_TYPE_I)
            spec.subframe_size,     # bSubslotSize (2, 3, or 4)
            spec.bit_resolution,    # bBitResolution (16, 24, or 32)
        )
        std_ep = struct.pack(
            "<BBBBHB",
            7,                      # bLength (7 in UAC 2.0)
            USB_DT_ENDPOINT,        # bDescriptorType
            0x01,                   # bEndpointAddress (EP 1 OUT)
            0x0D,                   # bmAttributes (Isochronous | Synchronous | Data)
            spec.max_packet_size,   # wMaxPacketSize (1024)
            1,                      # bInterval (1 = every microframe 125us)
        )
        cs_ep = struct.pack(
            "<BBBBBBH",
            8,                      # bLength
            USB_DT_CS_ENDPOINT,     # bDescriptorType
            0x01,                   # bDescriptorSubtype (EP_GENERAL)
            0x00,                   # bmAttributes
            0x00,                   # bmControls
            0x00,                   # bLockDelayUnits
            0x0000,                 # wLockDelay
        )
        as_alts.append(std_if + cs_as_gen + cs_fmt + std_ep + cs_ep)

    body = iad + ac_if + ac_hdr + ac_cs_body + as_alt0 + b"".join(as_alts)
    total_length = 9 + len(body)

    cfg_hdr = struct.pack(
        "<BBHBBBBB",
        9,              # bLength
        USB_DT_CONFIG,  # bDescriptorType
        total_length,   # wTotalLength
        2,              # bNumInterfaces
        1,              # bConfigurationValue
        0,              # iConfiguration
        0x80,           # bmAttributes (Bus Powered)
        50,             # bMaxPower (100mA)
    )
    return cfg_hdr + body


def build_uac1_config_descriptor(config: DacConfig) -> bytes:
    """Build USB Audio Class 1.0 Configuration Descriptor tree for Windows usbaudio.sys."""
    # UAC1 sample rates <= 96kHz that fit Full-Speed/High-Speed Isoch
    uac1_rates = [r for r in config.sample_rates if r <= 96000] or [44100, 48000]

    ac_if = struct.pack(
        "<BBBBBBBBB",
        9, USB_DT_INTERFACE, 0, 0, 0, 0x01, 0x01, 0x00, 2
    )
    input_term = struct.pack(
        "<BBBBHBBHBB",
        12, USB_DT_CS_INTERFACE, 0x02, UAC_INPUT_TERMINAL_ID,
        0x0101, 0x00, 2, 0x0003, 0, 0
    )
    feature_unit = struct.pack(
        "<BBBBBBBBBB",
        10, USB_DT_CS_INTERFACE, 0x06, UAC_FEATURE_UNIT_ID,
        UAC_INPUT_TERMINAL_ID, 1, 0x03, 0x00, 0x00, 0
    )
    output_term = struct.pack(
        "<BBBBHBBB",
        9, USB_DT_CS_INTERFACE, 0x03, UAC_OUTPUT_TERMINAL_ID,
        0x0301, 0x00, UAC_FEATURE_UNIT_ID, 0
    )
    ac_cs_body = input_term + feature_unit + output_term
    ac_hdr = struct.pack(
        "<BBBHHBB",
        9, USB_DT_CS_INTERFACE, 0x01, 0x0100,
        9 + len(ac_cs_body), 1, 1
    )

    as_alt0 = struct.pack(
        "<BBBBBBBBB",
        9, USB_DT_INTERFACE, 1, 0, 0, 0x01, 0x02, 0x00, 0
    )

    freq_bytes = b"".join(
        struct.pack("<I", r)[:3] for r in uac1_rates
    )

    as_alts = []
    for spec in config.alt_settings:
        if spec.bit_resolution > 24:
            continue
        std_if = struct.pack(
            "<BBBBBBBBB",
            9, USB_DT_INTERFACE, 1, spec.alt_setting, 1, 0x01, 0x02, 0x00, 0
        )
        cs_as_gen = struct.pack(
            "<BBBBBH",
            7, USB_DT_CS_INTERFACE, 0x01, UAC_INPUT_TERMINAL_ID, 1, 0x0001
        )
        fmt_len = 8 + len(freq_bytes)
        cs_fmt = (
            struct.pack(
                "<BBBBBBBB",
                fmt_len, USB_DT_CS_INTERFACE, 0x02, 0x01,
                spec.channels, spec.subframe_size, spec.bit_resolution, len(uac1_rates)
            )
            + freq_bytes
        )
        std_ep = struct.pack(
            "<BBBBHBBB",
            9, USB_DT_ENDPOINT, 0x01, 0x09, 576, 1, 0, 0
        )
        cs_ep = struct.pack(
            "<BBBBBH",
            7, USB_DT_CS_ENDPOINT, 0x01, 0x01, 0, 0
        )
        as_alts.append(std_if + cs_as_gen + cs_fmt + std_ep + cs_ep)

    body = ac_if + ac_hdr + ac_cs_body + as_alt0 + b"".join(as_alts)
    cfg_hdr = struct.pack(
        "<BBHBBBBB",
        9, USB_DT_CONFIG, 9 + len(body), 2, 1, 0, 0x80, 50
    )
    return cfg_hdr + body


def build_config_descriptor(config: DacConfig) -> bytes:
    if config.uac_version == 2:
        return build_uac2_config_descriptor(config)
    return build_uac1_config_descriptor(config)


def build_uac2_clock_range_ParamBlock(sample_rates: List[int]) -> bytes:
    """
    Build UAC 2.0 Layout 3 Parameter Block for Clock Source CS_SAM_FREQ_CONTROL RANGE request.
    wNumSubRanges (2B) + N * [dMIN (4B), dMAX (4B), dRES (4B)].
    """
    subranges = [struct.pack("<III", r, r, 0) for r in sorted(sample_rates)]
    return struct.pack("<H", len(subranges)) + b"".join(subranges)


def build_uac2_volume_range_param_block() -> bytes:
    """
    Build UAC 2.0 Layout 2 Parameter Block for Feature Unit Volume RANGE request.
    wNumSubRanges = 1, wMIN = -96.0dB (0xA000), wMAX = 0.0dB (0x0000), wRES = 0.5dB (0x0080).
    """
    return struct.pack("<Hhhh", 1, -0x6000, 0x0000, 0x0080)
