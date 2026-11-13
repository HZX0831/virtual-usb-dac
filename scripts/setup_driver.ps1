param(
    [switch]$Attach,
    [switch]$Detach,
    [string]$RemoteHost = "127.0.0.1",
    [string]$BusId = "1-1"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallerPath = Join-Path $ScriptDir "USBip-0.9.8.1-x64.exe"
$UsbipExe = "C:\Program Files\USBip\usbip.exe"

function Ensure-DriverInstalled {
    if (Test-Path $UsbipExe) {
        Write-Host "[OK] usbip-win2 is already installed at: $UsbipExe" -ForegroundColor Green
        return
    }

    if (-not (Test-Path $InstallerPath)) {
        Write-Host "[INFO] Downloading usbip-win2 v0.9.8.1 signed installer..." -ForegroundColor Cyan
        Invoke-WebRequest -Uri "https://github.com/vadimgrn/usbip-win2/releases/download/v.0.9.8.1/USBip-0.9.8.1-x64.exe" -OutFile $InstallerPath
    }

    Write-Host "[INFO] Launching usbip-win2 installer with Administrator privileges (please approve UAC prompt if shown)..." -ForegroundColor Yellow
    $proc = Start-Process -FilePath $InstallerPath -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Verb RunAs -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        throw "usbip-win2 installer exited with code $($proc.ExitCode)"
    }

    if (Test-Path $UsbipExe) {
        Write-Host "[OK] Successfully installed usbip-win2 driver and CLI at: $UsbipExe" -ForegroundColor Green
    } else {
        throw "Installer completed, but $UsbipExe was not found."
    }
}

Ensure-DriverInstalled

if ($Attach) {
    Write-Host "[INFO] Attaching virtual USB DAC ($RemoteHost bus $BusId)..." -ForegroundColor Cyan
    & $UsbipExe attach -r $RemoteHost -b $BusId
    & $UsbipExe port
}

if ($Detach) {
    Write-Host "[INFO] Detaching virtual USB devices..." -ForegroundColor Cyan
    & $UsbipExe detach -a
}
