# Virtual Hi-Fi USB DAC — Windows 虚拟 USB 音频播放器与验证平台

本项目在无物理音频输出设备的 Windows 电脑上，基于微软 WHQL 签名的 `usbip-win2` 虚拟 USB 总线驱动与本地 `asyncio` USB/IP 服务端，完整模拟了一个 **USB Audio Class 2.0 (UAC2) / 1.0 (UAC1) 高解析度 USB 声卡**。

Windows 会为其加载原生 `usbaudio2.sys` 驱动，在设备管理器中出现真实的 USB Audio 硬件节点，并注册标准系统扬声器端点 `扬声器 (Virtual Hi-Fi USB DAC)`，同时支持：
- **WASAPI 独占模式（Exclusive Mode）**：支持 `44.1kHz / 48kHz / 88.2kHz / 96kHz / 176.4kHz / 192kHz` × `16-bit / 24-bit / 32-bit` 共 18 种无损格式直通并动态切换虚拟硬件时钟与位深。
- **WASAPI 共享模式 / DirectSound / MME / WDM-KS**：兼容所有常规播放器输出。
- **底层 USB 枚举**：支持通过 `SetupDi` / `CfgMgr32` / `libusb` 枚举 USB VID/PID 与描述符。
- **实时音频内容验证**：通过 HTTP REST API、WebSocket 或内置 Web 监控台实时检验是否有声音输出、RMS/Peak 分贝、FFT 主频率，以及录制原始 USB PCM 为 `.wav` 文件进行比特完美（Bit-Perfect）对比。

---

## 快速启动

### 1. 首次安装虚拟 USB 总线驱动（仅需一次）
```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_driver.ps1
```

### 2. 启动虚拟 USB 声卡并自动挂载到 Windows
```powershell
C:\Users\HP\AppData\Local\Programs\Python\Python38\python.exe main.py --auto-attach
```
- **Web 实时监控面板**：浏览器访问 `http://127.0.0.1:8765`
- **USB/IP 协议端口**：`127.0.0.1:3240`

### 3. 运行自动化测试与 WASAPI 独占/共享闭环验证
```powershell
# 单元测试
C:\Users\HP\AppData\Local\Programs\Python\Python38\python.exe -m unittest discover -s tests -p "test_*.py" -v

# 端到端 WASAPI 共享 + 独占模式闭环验证
C:\Users\HP\AppData\Local\Programs\Python\Python38\python.exe tests/verify_wasapi.py
```

---

## HTTP / WebSocket 验证接口参考

| 接口 | 方法 | 说明 |
| :--- | :--- | :--- |
| `/api/status` | `GET` | 查询 USB 挂载状态、当前硬件采样率/位深/AltSetting、硬件音量/静音，以及实时音频特征（`is_playing`, `is_silent`, `rms_db`, `peak_db`, `dominant_freq_hz`, `total_frames`） |
| `/api/usb/logs` | `GET` | 查询最近收到的 USB Control 传输事件（如 `CLOCK_RATE_SET`, `SET_INTERFACE`, `VOLUME_SET`），用于断言独占模式是否真正切换了硬件参数 |
| `/api/device/plug` | `POST` | 动态插入（Attach）虚拟 USB 声卡，触发 Windows 设备到达事件 |
| `/api/device/unplug` | `POST` | 动态拔出（Detach）虚拟 USB 声卡，测试播放器断连处理与设备切换 |
| `/api/device/inject-error` | `POST` | 注入指定次数的 USB 同步传输错误（如 `{"count": 20}`），测试播放器容错恢复 |
| `/api/device/config` | `POST` | 动态修改 `vid`, `pid`, `manufacturer`, `product`, `serial`, `uac_version` 并重连 |
| `/api/capture/start` | `POST` | 开始将 USB Isochronous OUT 接收到的原始 PCM 字节流录制为 `.wav` |
| `/api/capture/stop` | `POST` | 停止录制并返回文件路径、帧数及原始 PCM 的 `sha256` 校验值 |
| `/ws/telemetry` | `WS` | 实时推送硬件协商状态、电平表、时域波形、32段 FFT 频谱与 USB 日志 |
