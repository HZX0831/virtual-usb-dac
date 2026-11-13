import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Optional, Set
from aiohttp import web
from .config import DacConfig
from .usbip_server import VirtualUsbDacServer
from .audio_analyzer import AudioStreamAnalyzer

logger = logging.getLogger("api_server")


def find_usbip_exe() -> Optional[str]:
    found = shutil.which("usbip") or shutil.which("usbip.exe")
    if found:
        return found
    candidates = [
        r"C:\Program Files\USBip\usbip.exe",
        r"C:\Program Files\usbip-win2\usbip.exe",
        r"C:\Program Files (x86)\USBip\usbip.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


class VerificationApiServer:
    """HTTP REST + WebSocket verification and control server."""

    def __init__(
        self,
        config: DacConfig,
        dac_server: VirtualUsbDacServer,
        analyzer: AudioStreamAnalyzer,
    ) -> None:
        self.config = config
        self.dac_server = dac_server
        self.analyzer = analyzer
        self.app = web.Application()
        self._runner: Optional[web.AppRunner] = None
        self._ws_clients: Set[web.WebSocketResponse] = set()
        self._telemetry_task: Optional[asyncio.Task] = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/status", self.handle_get_status)
        self.app.router.add_get("/api/usb/logs", self.handle_get_logs)
        self.app.router.add_post("/api/device/plug", self.handle_plug)
        self.app.router.add_post("/api/device/unplug", self.handle_unplug)
        self.app.router.add_post("/api/device/inject-error", self.handle_inject_error)
        self.app.router.add_post("/api/device/config", self.handle_update_config)
        self.app.router.add_post("/api/capture/start", self.handle_capture_start)
        self.app.router.add_post("/api/capture/stop", self.handle_capture_stop)
        self.app.router.add_post("/api/stats/reset", self.handle_reset_stats)
        self.app.router.add_get("/ws/telemetry", self.handle_ws)

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.api_host, self.config.api_port)
        await site.start()
        self._telemetry_task = asyncio.create_task(self._broadcast_telemetry_loop())
        logger.info("HTTP/WebSocket Verification API listening on http://%s:%d", self.config.api_host, self.config.api_port)

    async def stop(self) -> None:
        if self._telemetry_task:
            self._telemetry_task.cancel()
            try:
                await self._telemetry_task
            except asyncio.CancelledError:
                pass
        for ws in list(self._ws_clients):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    def _build_full_status(self) -> dict:
        usbip_exe = find_usbip_exe()
        return {
            "ok": True,
            "driver": {
                "usbip_exe": usbip_exe,
                "installed": usbip_exe is not None,
            },
            "hardware": self.dac_server.get_hardware_state(),
            "audio": self.analyzer.get_snapshot(),
        }

    async def handle_index(self, request: web.Request) -> web.Response:
        static_file = os.path.join(os.path.dirname(__file__), "static", "index.html")
        if os.path.isfile(static_file):
            with open(static_file, "r", encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/html")
        return web.Response(text="<h1>Virtual USB DAC Running</h1>", content_type="text/html")

    async def handle_get_status(self, request: web.Request) -> web.Response:
        return web.json_response(self._build_full_status())

    async def handle_get_logs(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "100"))
        logs = list(self.dac_server.usb_logs)[-limit:]
        return web.json_response({"ok": True, "count": len(logs), "logs": logs})

    async def handle_plug(self, request: web.Request) -> web.Response:
        usbip_exe = find_usbip_exe()
        if not usbip_exe:
            return web.json_response(
                {
                    "ok": False,
                    "error": "usbip.exe not found. Please run scripts/setup_driver.ps1 to install usbip-win2 driver.",
                },
                status=400,
            )

        if self.dac_server.attached:
            return web.json_response(
                {"ok": True, "message": "Device is already attached.", "hardware": self.dac_server.get_hardware_state()}
            )

        cmd = [usbip_exe, "attach", "-r", self.config.usbip_host, "-b", self.config.bus_id]
        loop = asyncio.get_event_loop()
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=10),
        )
        # Wait up to 2.0s for attach handshake
        for _ in range(20):
            if self.dac_server.attached:
                break
            await asyncio.sleep(0.1)

        return web.json_response(
            {
                "ok": proc.returncode == 0 or self.dac_server.attached,
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "hardware": self.dac_server.get_hardware_state(),
            }
        )

    async def handle_unplug(self, request: web.Request) -> web.Response:
        usbip_exe = find_usbip_exe()
        stdout_str, stderr_str = "", ""
        if usbip_exe:
            loop = asyncio.get_event_loop()
            try:
                proc = await loop.run_in_executor(
                    None,
                    lambda: subprocess.run([usbip_exe, "detach", "-a"], capture_output=True, text=True, timeout=8),
                )
                stdout_str = proc.stdout.strip()
                stderr_str = proc.stderr.strip()
            except Exception as exc:
                stderr_str = str(exc)

        closed = await self.dac_server.disconnect_active_clients("API unplug request")
        return web.json_response(
            {
                "ok": True,
                "closed_connections": closed,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "hardware": self.dac_server.get_hardware_state(),
            }
        )

    async def handle_inject_error(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        count = int(body.get("count", 15))
        status_code = int(body.get("status", -71))
        self.dac_server.inject_errors(count=count, errno_status=status_code)
        return web.json_response(
            {
                "ok": True,
                "inject_error_remaining": self.dac_server.inject_error_remaining,
                "status_code": self.dac_server.inject_error_status,
            }
        )

    async def handle_update_config(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

        was_attached = self.dac_server.attached
        replug = bool(body.get("replug", was_attached))

        if was_attached and replug:
            await self.dac_server.disconnect_active_clients("Reconfiguring USB device descriptors")
            await asyncio.sleep(0.3)

        self.config.update_from_dict(body)
        self.dac_server.current_sample_rate = self.config.default_sample_rate
        self.dac_server.log_usb_event(
            "CONFIG_UPDATE",
            f"Updated config: VID=0x{self.config.vid:04X}, PID=0x{self.config.pid:04X}, "
            f"Product='{self.config.product}', UAC={self.config.uac_version}",
        )

        if replug and find_usbip_exe():
            await self.handle_plug(request)

        return web.json_response({"ok": True, "config": self.config.to_dict(), "hardware": self.dac_server.get_hardware_state()})

    async def handle_capture_start(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        filename = body.get("filename")
        path = self.analyzer.start_recording(
            sample_rate=self.dac_server.current_sample_rate,
            bit_resolution=self.dac_server.current_bit_depth,
            channels=self.dac_server.current_channels,
            filename=filename,
        )
        self.dac_server.log_usb_event("CAPTURE_START", f"Started USB PCM capture -> {os.path.basename(path)}")
        return web.json_response({"ok": True, "recording_path": path})

    async def handle_capture_stop(self, request: web.Request) -> web.Response:
        info = self.analyzer.stop_recording()
        self.dac_server.log_usb_event(
            "CAPTURE_STOP",
            f"Stopped USB PCM capture ({info['frames_captured']} frames, {info['duration_sec']}s)",
        )
        return web.json_response({"ok": True, "recording": info})

    async def handle_reset_stats(self, request: web.Request) -> web.Response:
        self.analyzer.reset_stats()
        self.dac_server.log_usb_event("STATS_RESET", "Audio stream statistics reset")
        return web.json_response({"ok": True, "audio": self.analyzer.get_snapshot()})

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        try:
            await ws.send_json({"type": "snapshot", **self._build_full_status(), "logs": list(self.dac_server.usb_logs)[-30:]})
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    pass
        finally:
            self._ws_clients.discard(ws)
        return ws

    async def _broadcast_telemetry_loop(self) -> None:
        while True:
            await asyncio.sleep(0.08)
            if not self._ws_clients:
                continue
            payload = json.dumps(
                {
                    "type": "telemetry",
                    **self._build_full_status(),
                    "logs": list(self.dac_server.usb_logs)[-30:],
                }
            )
            dead = []
            for ws in list(self._ws_clients):
                try:
                    await ws.send_str(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._ws_clients.discard(ws)
