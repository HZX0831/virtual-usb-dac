import argparse
import asyncio
import logging
import os
import subprocess
import sys
from usb_dac.config import DacConfig
from usb_dac.audio_analyzer import AudioStreamAnalyzer
from usb_dac.usbip_server import VirtualUsbDacServer
from usb_dac.api_server import VerificationApiServer, find_usbip_exe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Virtual Hi-Fi USB DAC & Verification Server")
    parser.add_argument("--usbip-port", type=int, default=3240, help="USB/IP TCP port (default: 3240)")
    parser.add_argument("--api-port", type=int, default=8765, help="HTTP/WS API port (default: 8765)")
    parser.add_argument("--uac", type=int, choices=[1, 2], default=2, help="USB Audio Class version (1 or 2)")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=0x1209, help="USB Vendor ID (e.g. 0x1209)")
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=0xF001, help="USB Product ID (e.g. 0xF001)")
    parser.add_argument("--product", type=str, default="Virtual Hi-Fi USB DAC", help="USB Product Name")
    parser.add_argument("--auto-attach", action="store_true", help="Automatically attach via usbip.exe on startup")
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = DacConfig(
        vid=args.vid,
        pid=args.pid,
        product=args.product,
        uac_version=args.uac,
        usbip_port=args.usbip_port,
        api_port=args.api_port,
    )
    captures_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
    analyzer = AudioStreamAnalyzer(captures_dir=captures_dir)
    dac_server = VirtualUsbDacServer(config=config, analyzer=analyzer)
    api_server = VerificationApiServer(config=config, dac_server=dac_server, analyzer=analyzer)

    await dac_server.start()
    await api_server.start()

    if args.auto_attach:
        usbip_exe = find_usbip_exe()
        if usbip_exe:
            logging.info("Auto-attaching virtual USB DAC using %s ...", usbip_exe)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [usbip_exe, "attach", "-r", config.usbip_host, "-b", config.bus_id],
                    capture_output=True,
                    text=True,
                ),
            )
        else:
            logging.warning("usbip.exe not found; skipping --auto-attach.")

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await api_server.stop()
        await dac_server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main_async(parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
