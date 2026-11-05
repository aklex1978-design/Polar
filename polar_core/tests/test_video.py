import http.server
import threading
import time

import numpy as np
import pytest

from polar_core import onvif
from polar_core.cameras import CameraRecord
from polar_core.video import VideoSource, capture_target, ffmpeg_options, opencv_opener, redact, with_credentials


def test_credentials_and_redact():
    url = with_credentials("rtsp://10.0.0.5:554/live", "ad min", "p@ss:1")
    assert url == "rtsp://ad%20min:p%40ss%3A1@10.0.0.5:554/live"
    assert "p%40ss" not in redact(url)
    assert with_credentials("http://cam/x", "", "") == "http://cam/x"


def test_capture_target_per_protocol():
    assert capture_target(CameraRecord(protocol="USB", address="1")) == 1
    assert capture_target(CameraRecord(protocol="USB", address="/dev/video3")) == "/dev/video3"
    r = CameraRecord(protocol="ONVIF", address="10.0.0.7:8000", login="u", stream_kind="sub")
    seen = {}

    def lookup(addr, login, pw, kind, transport):
        seen.update(addr=addr, kind=kind, transport=transport)
        return "rtsp://10.0.0.7/sub"
    assert capture_target(r, "pw", onvif_lookup=lookup) == "rtsp://u:pw@10.0.0.7/sub"
    assert seen == {"addr": "10.0.0.7:8000", "kind": "sub", "transport": "TCP"}


def test_ffmpeg_options():
    assert "rtsp_transport;udp" in ffmpeg_options(CameraRecord(protocol="RTSP", transport="UDP"))
    assert "nobuffer" in ffmpeg_options(CameraRecord(protocol="RTSP"))
    assert "max_delay;200000" in ffmpeg_options(CameraRecord(protocol="HTTP", buffer_ms=200))


class FakeCapture:
    def __init__(self, frames, fail_after=None):
        self.n = 0
        self.frames = frames
        self.fail_after = fail_after

    def isOpened(self):  # noqa: N802
        return True

    def read(self):
        time.sleep(0.005)
        self.n += 1
        if self.fail_after is not None and self.n > self.fail_after:
            return False, None
        return True, np.full((4, 6, 3), self.n % 255, np.uint8)

    def release(self):
        pass


def test_video_source_timestamps_and_reconnect():
    opened = []
    events = []

    def opener():
        opened.append(1)
        if len(opened) == 2:
            raise IOError("камера недоступна")
        return FakeCapture(None, fail_after=20 if len(opened) == 1 else None)

    src = VideoSource(opener, "cam", on_event=lambda k, m: events.append(k), reconnect_s=(0.01, 0.05)).start()
    try:
        f1 = src.wait_frame(timeout=2)
        assert f1 is not None and src.frame_size == (6, 4)
        deadline = time.monotonic() + 3
        while len(opened) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        f2 = src.wait_frame(after_index=25, timeout=2)
        assert f2.t > f1.t and f2.index > f1.index
        assert events[:2] == ["stream_ok", "stream_lost"] and events[-1] == "stream_ok"
        assert src.reconnects >= 2 and src.fps > 10
    finally:
        src.stop()


# --- справжнє захоплення через OpenCV: MJPEG по HTTP ----------------------------------
class _Mjpeg(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        import cv2

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            for i in range(200):
                img = np.zeros((120, 160, 3), np.uint8)
                cv2.putText(img, str(i), (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
                jpg = cv2.imencode(".jpg", img)[1].tobytes()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def test_opencv_http_mjpeg():
    cv2 = pytest.importorskip("cv2")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Mjpeg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rec = CameraRecord(protocol="HTTP", address=f"http://127.0.0.1:{srv.server_port}/video.mjpg")
    src = VideoSource(opencv_opener(capture_target(rec), ffmpeg_options(rec)), "http").start()
    try:
        f = src.wait_frame(timeout=10)
        assert f is not None, src.last_error
        assert f.image.shape[:2] == (120, 160)
        f2 = src.wait_frame(after_index=f.index + 5, timeout=5)
        assert f2 is not None and f2.t > f.t
    finally:
        src.stop()
        srv.shutdown()
    assert cv2


# --- ONVIF -------------------------------------------------------------------------------
PROFILES = ('<s:Envelope><s:Body><trt:GetProfilesResponse><trt:Profiles token="main_0" fixed="true"/>'
            '<trt:Profiles token="sub_1"/></trt:GetProfilesResponse></s:Body></s:Envelope>')
URI = ('<s:Envelope><s:Body><trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://10.0.0.7:554/'
       'Streaming/Channels/102?a=1&amp;b=2</tt:Uri></trt:MediaUri></trt:GetStreamUriResponse></s:Body></s:Envelope>')


class _Onvif(http.server.BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        _Onvif.bodies.append(body)
        out = (PROFILES if "GetProfiles" in body else URI).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_onvif_stream_uri():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Onvif)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        uri = onvif.get_stream_uri(f"127.0.0.1:{srv.server_port}", "admin", "secret", "sub", "TCP")
    finally:
        srv.shutdown()
    assert uri == "rtsp://10.0.0.7:554/Streaming/Channels/102?a=1&b=2"
    assert "sub_1" in _Onvif.bodies[1] and "PasswordDigest" in _Onvif.bodies[0]
    assert "secret" not in _Onvif.bodies[0]                 # пароль лише як дайджест


def test_onvif_digest_is_standard():
    # приклад: nonce, created і пароль відомі → дайджест за формулою WS-Security
    import base64
    import hashlib
    x = onvif._security("u", "pw", nonce=b"0123456789abcdef", created="2026-01-01T00:00:00.000Z")
    expect = base64.b64encode(hashlib.sha1(b"0123456789abcdef2026-01-01T00:00:00.000Zpw").digest()).decode()
    assert expect in x
