import os

from polar_core import usb_cameras as u
from polar_core.cameras import address_error

V4L2 = """ioctl: VIDIOC_ENUM_FMT
	Type: Video Capture

	[0]: 'MJPG' (Motion-JPEG, compressed)
		Size: Discrete 1920x1080
			Interval: Discrete 0.033s (30.000 fps)
			Interval: Discrete 0.067s (15.000 fps)
		Size: Discrete 1280x720
			Interval: Discrete 0.017s (60.000 fps)
	[1]: 'YUYV' (YUYV 4:2:2)
		Size: Discrete 1920x1080
			Interval: Discrete 0.200s (5.000 fps)
		Size: Discrete 640x480
			Interval: Discrete 0.033s (30.000 fps)
"""


def test_parse_v4l2_formats():
    modes = u.parse_v4l2_formats(V4L2)
    assert u.VideoMode(1920, 1080, 30.0, "MJPG") in modes
    assert u.VideoMode(1280, 720, 60.0, "MJPG") in modes
    assert u.VideoMode(640, 480, 30.0, "YUYV") in modes
    assert len(modes) == 5
    cam = u.UsbCamera("/dev/video0", "x", modes=modes)
    best = cam.best_mode()
    assert (best.width, best.height, best.fps, best.codec) == (1920, 1080, 30.0, "MJPEG")
    assert str(best) == "1920×1080, 30 кадрів/с, MJPG"


def test_list_from_sysfs(tmp_path):
    for n, name, index in ((0, "HD Pro Webcam C920", "0"), (1, "HD Pro Webcam C920", "1"),
                           (2, "Thermal Cam", "0")):
        d = tmp_path / f"video{n}"
        d.mkdir()
        (d / "name").write_text(name + "\n")
        (d / "index").write_text(index + "\n")
    cams = u.list_usb_cameras(sysfs=str(tmp_path), modes_for=lambda dev: u.parse_v4l2_formats(V4L2)
                              if dev == "/dev/video0" else [])
    assert [(c.device, c.name) for c in cams] == [("/dev/video0", "HD Pro Webcam C920"),
                                                  ("/dev/video2", "Thermal Cam")]
    assert cams[0].best_mode().fps == 30.0 and cams[1].best_mode() is None
    assert cams[0].address == "/dev/video0"


def test_stable_address_is_valid():
    assert address_error("USB", "/dev/v4l/by-id/usb-046d_HD_Pro_Webcam_C920_ABC-video-index0") is None
    assert address_error("USB", "/dev/v4l/by-id/") is not None
    assert u.UsbCamera("/dev/video0", "c", "/dev/v4l/by-id/usb-x-video-index0").address.endswith("index0")


def test_real_system_does_not_crash():
    assert isinstance(u.list_usb_cameras(), list)
    assert os.path  # список на цій машині може бути порожнім
