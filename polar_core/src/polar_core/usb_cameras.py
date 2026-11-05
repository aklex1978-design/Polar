"""Список USB-камер, підключених до сервера (кнопка «Отримати список камер» у формі камери).

* Linux: пристрої з /sys/class/video4linux; режими — з `v4l2-ctl --list-formats-ext` (пакет v4l-utils),
  інакше поточний режим через OpenCV.
* Windows: назви пристроїв DirectShow (pygrabber; без нього — PowerShell), номер у списку = номер
  пристрою для OpenCV (CAP_DSHOW); режими — пробою стандартних роздільних здатностей.
* macOS: назви з system_profiler, пристрої — AVFoundation.

Камера, яку вже показує Polar, на Windows і macOS повторно не відкривається (пристрій зайнятий),
тому її режим береться з робочого потоку (параметр in_use) — інакше вона «зникала» б зі списку.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

FOURCC_CODEC = {"MJPG": "MJPEG", "H264": "H.264", "HEVC": "H.265", "H265": "H.265"}


@dataclass(frozen=True)
class VideoMode:
    width: int
    height: int
    fps: float
    fourcc: str = ""

    @property
    def codec(self) -> Optional[str]:
        return FOURCC_CODEC.get(self.fourcc)

    def __str__(self) -> str:
        fmt = f", {self.fourcc}" if self.fourcc else ""
        return f"{self.width}×{self.height}, {self.fps:g} кадрів/с{fmt}"


@dataclass
class UsbCamera:
    device: str                     # /dev/video0 або номер пристрою
    name: str
    stable_path: str = ""           # /dev/v4l/by-id/… — не змінюється після перезавантаження
    modes: List[VideoMode] = field(default_factory=list)

    @property
    def address(self) -> str:
        """Що записати в поле «Адреса»."""
        return self.stable_path or self.device

    def best_mode(self) -> Optional[VideoMode]:
        """Найбільша роздільна здатність, з неї — найбільше кадрів/с; стиснені формати в пріоритеті."""
        if not self.modes:
            return None
        return max(self.modes, key=lambda m: (m.width * m.height, m.fps, m.codec is not None))


def parse_v4l2_formats(text: str) -> List[VideoMode]:
    modes, fourcc, size = [], "", None
    for line in text.splitlines():
        m = re.search(r"\[\d+\]:\s*'(\w+)'", line)
        if m:
            fourcc, size = m.group(1), None
            continue
        m = re.search(r"Size:\s*\w+\s+(\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            continue
        m = re.search(r"Interval:.*\(([\d.]+)\s*fps\)", line)
        if m and size:
            mode = VideoMode(size[0], size[1], round(float(m.group(1)), 3), fourcc)
            if mode not in modes:
                modes.append(mode)
    return modes


def _stable_paths() -> dict:
    out = {}
    for link in sorted(glob.glob("/dev/v4l/by-id/*")):
        target = os.path.realpath(link)
        if link.endswith("-index0") or target not in out:
            out[target] = link
    return out


def _is_capture_node(sys_dir: str) -> bool:
    """У одного USB-пристрою кілька вузлів /dev/videoN; кадри дає той, у кого index 0."""
    try:
        with open(os.path.join(sys_dir, "index")) as fh:
            return fh.read().strip() == "0"
    except OSError:
        return True


def _v4l2_modes(device: str, run=subprocess.run) -> List[VideoMode]:
    if not shutil.which("v4l2-ctl"):
        return []
    try:
        r = run(["v4l2-ctl", "-d", device, "--list-formats-ext"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_v4l2_formats(r.stdout)


STANDARD_SIZES = ((3840, 2160), (2560, 1440), (1920, 1080), (1280, 960), (1280, 720), (1024, 768),
                  (800, 600), (640, 480), (320, 240))


def _opencv_probe(device, try_sizes: bool = False) -> Tuple[bool, List[VideoMode]]:
    """Відкрити пристрій і прочитати режим(и). (False, []) — не відкрився (немає або зайнятий)."""
    try:
        import cv2
    except ImportError:
        return False, []
    from .video import usb_backends

    cap = None
    try:
        for backend in usb_backends(cv2):
            cap = cv2.VideoCapture(device, backend)
            if cap.isOpened():
                break
            cap.release()
            cap = None
        if cap is None:
            return False, []
        modes = []

        def current():
            w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = round(float(cap.get(cv2.CAP_PROP_FPS) or 0), 3)
            if w and h and VideoMode(w, h, fps) not in modes:
                modes.append(VideoMode(w, h, fps))
        current()
        if try_sizes:                      # драйвер повертає найближчий підтримуваний розмір
            for w, h in STANDARD_SIZES:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                current()
        return True, modes
    finally:
        if cap is not None:
            cap.release()


def _run(cmd: List[str], timeout: float = 10.0) -> str:
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def windows_camera_names() -> Tuple[List[str], bool]:
    """(назви, порядок збігається з номерами DirectShow)."""
    try:
        from pygrabber.dshow_graph import FilterGraph

        return list(FilterGraph().get_input_devices()), True
    except Exception:  # noqa: BLE001 — pygrabber не встановлено або COM недоступний
        pass
    out = _run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -in 'Camera','Image' } "
                "| Select-Object -ExpandProperty Name"])
    return [line.strip() for line in out.splitlines() if line.strip()], False


def mac_camera_names() -> List[str]:
    import json

    try:
        data = json.loads(_run(["system_profiler", "SPCameraDataType", "-json"]) or "{}")
        return [c.get("_name", "") for c in data.get("SPCameraDataType", [])]
    except ValueError:
        return []


def list_usb_cameras(sysfs: str = "/sys/class/video4linux",
                     modes_for: Optional[Callable[[str], List[VideoMode]]] = None,
                     in_use: Optional[Dict[str, VideoMode]] = None,
                     platform: str = sys.platform,
                     names_for: Optional[Callable[[], Tuple[List[str], bool]]] = None,
                     probe: Callable = _opencv_probe) -> List[UsbCamera]:
    """in_use: адреса пристрою, який уже показує Polar → режим його потоку."""
    in_use = in_use or {}
    if platform.startswith("linux") and os.path.isdir(sysfs):
        stable = _stable_paths()
        cams = []
        for d in sorted(glob.glob(os.path.join(sysfs, "video*")),
                        key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0)):
            if not _is_capture_node(d):
                continue
            dev = "/dev/" + os.path.basename(d)
            try:
                with open(os.path.join(d, "name")) as fh:
                    name = fh.read().strip()
            except OSError:
                name = os.path.basename(d)
            modes = (modes_for or _v4l2_modes)(dev)
            if not modes and modes_for is None:
                _ok, modes = probe(dev)
            path = stable.get(os.path.realpath(dev), "")
            running = in_use.get(path) or in_use.get(dev) or in_use.get(re.sub(r"\D", "", dev))
            if not modes and running:
                modes = [running]
            cams.append(UsbCamera(dev, name, path, modes))
        return cams

    # Windows, macOS
    if names_for is not None:
        names, exact = names_for()
    elif platform.startswith("win"):
        names, exact = windows_camera_names()
    elif platform == "darwin":
        names, exact = mac_camera_names(), False
    else:
        names, exact = [], False
    count = len(names) if exact else max(len(names), 10)
    cams = []
    for i in range(count):
        running = in_use.get(str(i))
        if running is not None:
            ok, modes = True, [running]                 # пристрій зайнятий потоком Polar
        else:
            ok, modes = probe(i, try_sizes=True)
        if not ok and not exact:
            continue
        if i < len(names):
            name = names[i] + ("" if exact else " (назва орієнтовна)")
        else:
            name = f"USB-камера {i}"
        cams.append(UsbCamera(str(i), name, "", modes))
    return cams
