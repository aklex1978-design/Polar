"""Список USB-камер, підключених до сервера (кнопка «Отримати список камер» у формі камери).

Linux: пристрої з /sys/class/video4linux; режими (роздільна здатність, кадри/с) — з `v4l2-ctl
--list-formats-ext`, якщо він встановлений (пакет v4l-utils), інакше поточний режим через OpenCV.
Інші ОС: перебір номерів пристроїв 0–9 через OpenCV.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

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


def _opencv_probe(device) -> Tuple[bool, List[VideoMode]]:
    try:
        import cv2
    except ImportError:
        return False, []
    cap = cv2.VideoCapture(device)
    try:
        if not cap.isOpened():
            return False, []
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        return True, ([VideoMode(w, h, round(fps, 3))] if w and h else [])
    finally:
        cap.release()


def list_usb_cameras(sysfs: str = "/sys/class/video4linux",
                     modes_for: Optional[Callable[[str], List[VideoMode]]] = None) -> List[UsbCamera]:
    if sys.platform.startswith("linux") and os.path.isdir(sysfs):
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
                _ok, modes = _opencv_probe(dev)
            cams.append(UsbCamera(dev, name, stable.get(os.path.realpath(dev), ""), modes))
        return cams
    cams = []
    for i in range(10):                                   # Windows, macOS
        ok, modes = _opencv_probe(i)
        if ok:
            cams.append(UsbCamera(str(i), f"Камера {i}", "", modes))
    return cams
