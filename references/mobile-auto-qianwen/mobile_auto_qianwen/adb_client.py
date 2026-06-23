import base64
import subprocess
import time
from pathlib import Path

from .constants import DEFAULT_ADB, DEFAULT_SERIAL


class AdbError(RuntimeError):
    pass


class AdbClient:
    def __init__(self, adb: str = DEFAULT_ADB, serial: str | None = DEFAULT_SERIAL):
        self.adb = adb
        self.serial = serial

    def command(self, args: list[str], check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
        command = [self.adb]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(args)
        if text:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        else:
            result = subprocess.run(command, capture_output=True, text=False)
        if check and result.returncode != 0:
            stderr = result.stderr if isinstance(result.stderr, str) else ""
            stdout = result.stdout if isinstance(result.stdout, str) else ""
            raise AdbError(stderr.strip() or stdout.strip() or f"adb failed: {command}")
        return result

    def devices(self) -> list[str]:
        result = subprocess.run([self.adb, "devices"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        devices = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices

    def resolve_serial(self) -> str:
        if self.serial:
            return self.serial
        devices = self.devices()
        if not devices:
            raise AdbError("No connected adb device found.")
        self.serial = devices[0]
        return self.serial

    def tap(self, x: int, y: int) -> None:
        self.command(["shell", "input", "tap", str(x), str(y)])

    def keyevent(self, code: int) -> None:
        self.command(["shell", "input", "keyevent", str(code)])

    def text(self, value: str) -> None:
        escaped = value.replace("%", "%s").replace(" ", "%s")
        self.command(["shell", "input", "text", escaped])

    def broadcast_text(self, value: str) -> None:
        self.command(["shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", value])

    def broadcast_base64_text(self, value: str) -> None:
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        self.command(["shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", encoded])

    def broadcast_clear_text(self) -> None:
        self.command(["shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"])

    def list_imes(self) -> list[str]:
        result = self.command(["shell", "ime", "list", "-s"])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def current_ime(self) -> str:
        return self.command(["shell", "settings", "get", "secure", "default_input_method"], check=False).stdout.strip()

    def set_ime(self, ime: str) -> None:
        self.command(["shell", "ime", "set", ime])

    def dump_xml(self) -> str:
        remote = "/sdcard/mobile-auto-qianwen-window.xml"
        last_error: Exception | None = None
        for _ in range(3):
            try:
                self.command(["shell", "uiautomator", "dump", remote])
                xml = self.command(["shell", "cat", remote]).stdout
                if xml and "<hierarchy" in xml:
                    return xml
                last_error = AdbError("uiautomator dump did not produce valid hierarchy xml")
            except Exception as exc:
                last_error = exc
                cat_result = self.command(["shell", "cat", remote], check=False)
                xml = cat_result.stdout or ""
                if "<hierarchy" in xml:
                    return xml
            time.sleep(0.8)
        raise AdbError(str(last_error) if last_error else "uiautomator dump failed")

    def screenshot(self, path: str | Path) -> bool:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        remote = "/sdcard/mobile-auto-qianwen-screen.png"
        shot = self.command(["shell", "screencap", "-p", remote], check=False)
        if shot.returncode != 0:
            return False
        pull = self.command(["pull", remote, str(target)], check=False)
        return pull.returncode == 0 and target.exists() and target.stat().st_size > 0

    def current_focus(self) -> str:
        result = self.command(["shell", "dumpsys", "window"], check=False)
        lines = [line.strip() for line in result.stdout.splitlines() if "mCurrentFocus" in line or "mFocusedApp" in line]
        return "\n".join(lines)

    def start_app(self, package: str) -> None:
        self.command(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])
