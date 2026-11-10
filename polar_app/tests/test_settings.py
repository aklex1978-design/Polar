"""Етап 8: налаштування (загальні, виявлення, ROS 2), журнал подій, повідомлення про помилки."""
import os
import sqlite3
import sys

import pytest

from polar_app import app_config
from polar_app import events as ev
from polar_app.__main__ import build_context
from polar_app.errors import ErrorReporter, setup_log_file
from polar_core import db
from polar_core.detection import DetectionSettings, load_settings

import fakes
from demo_data import seed_demo


def no_ros():
    raise ImportError("No module named 'rclpy'")


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("POLAR_CONFIG_DIR", str(tmp_path / "config"))
    path = str(tmp_path / "polar.sqlite")
    seed_demo(db.connect(path))
    c = fakes.bind(build_context(path, video_options=fakes.video_options(), ros_factory=no_ros))
    yield c
    c.shutdown()


def test_panorama_dir(ctx, tmp_path):
    st = ctx.settings
    assert st.panorama_dir_is_default() and st.panorama_dir() == str(tmp_path / "panoramas")
    new = st.set_panorama_dir(str(tmp_path / "Панорами"))                    # кирилиця — без проблем
    assert new == str(tmp_path / "Панорами") and os.path.isdir(new) and not st.panorama_dir_is_default()
    assert ctx.learning.panorama_dir() == new
    assert ctx.maps.tiles_dir() == str(tmp_path / "maps")                     # карти — поруч
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(ValueError, match="не можна записувати"):
        st.set_panorama_dir(str(blocker / "sub"))
    st.set_panorama_dir("")                                                   # назад до замовчування
    assert st.panorama_dir_is_default()


def test_database_backup_and_switch(ctx, tmp_path):
    st = ctx.settings
    assert st.db_path() == str(tmp_path / "polar.sqlite")
    written = st.backup(str(tmp_path / "копії" / "backup.sqlite"))
    assert written[0].endswith("backup.sqlite") and os.path.isfile(written[0])
    assert len(written) == 2 and written[1].endswith(".key")                 # ключ паролів поруч
    conn = sqlite3.connect(written[0])
    assert conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 3
    conn.close()
    # інша база: перевірка і запамʼятовування для наступного запуску
    with pytest.raises(ValueError, match="не знайдено"):
        st.switch_database(str(tmp_path / "missing.sqlite"))
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"not a database at all" * 100)
    with pytest.raises(ValueError, match="не база Polar"):
        st.switch_database(str(junk))
    new = st.switch_database(str(tmp_path / "нова.sqlite"), create=True)
    assert os.path.isfile(new) and app_config.db_path() == new
    with pytest.raises(ValueError, match="уже існує"):
        st.switch_database(new, create=True)
    assert st.switch_database(written[0]) == written[0] and app_config.db_path() == written[0]
    assert app_config.db_path(str(tmp_path / "cli.sqlite")) == str(tmp_path / "cli.sqlite")   # --db важливіший


def test_detection_settings_saved_and_applied(ctx):
    st = ctx.settings
    s = st.detection()
    s.k_tolerance, s.confirm_frames, s.hold_s = 3.5, 5, 2.0
    assert "наступним" in st.save_detection(s)
    with ctx.control._db_lock:
        back = load_settings(ctx.conn)
    assert back.k_tolerance == 3.5 and back.confirm_frames == 5
    bad = DetectionSettings(min_ray_angle_deg=95.0, confirm_frames=0)
    with pytest.raises(ValueError, match="кут між променями.*підтвердження|підтвердження.*кут"):
        st.save_detection(bad)
    # Слідкування увімкнене — перезапускається з новими значеннями
    ctx.detection.start = lambda: ["Камера 1"]
    calls = []
    ctx.detection.stop = lambda: calls.append("stop")
    ctx.detection.mode.start_tracking()
    s.hold_s = 7.0
    assert "перезапущено" in st.save_detection(s) and calls == ["stop"]


def test_ros_settings_apply_and_restart_fields(ctx):
    st = ctx.settings
    s = st.ros()
    s.state_rate_hz, s.link_timeout_s = 10.0, 3.0
    text, pending = st.save_ros(s)
    assert pending == [] and ctx.control.ros.state_rate_hz == 10.0
    s.domain_id, s.discovery_server = 42, "192.168.1.10:11811"
    text, pending = st.save_ros(s)
    assert set(pending) == {"domain_id", "discovery_server"} and "перезапуску" in text
    s.domain_id = 500
    with pytest.raises(ValueError, match="ROS_DOMAIN_ID"):
        st.save_ros(s)
    assert isinstance(st.interfaces(), list)


def test_events_query_and_export(ctx, tmp_path):
    cid = ctx.records()[0].id
    with ctx.control._db_lock:
        with ctx.conn:
            ctx.conn.executemany("INSERT INTO events (camera_id, kind, message) VALUES (?, ?, ?)", [
                (cid, "motion", "Камера 1: новий обʼєкт"), (None, "mode", "Слідкування → Виявлення"),
                (cid, "stream_lost", "Камера 1: потік втрачено"), (None, "error", "ZeroDivisionError")])
        all_ = ev.query(ctx.conn)
        assert [e.kind for e in all_[:4]] == ["error", "stream_lost", "mode", "motion"]
        assert {e.kind for e in ev.query(ctx.conn, camera_id=cid)} == {"motion", "stream_lost"}
        assert {e.kind for e in ev.query(ctx.conn, no_camera=True)} >= {"mode", "error"}
        assert [e.kind for e in ev.query(ctx.conn, kinds=list(ev.ALERT_KINDS))][:2] == ["error", "stream_lost"]
        assert len(ev.query(ctx.conn, text="потік")) == 1
        assert len(ev.query(ctx.conn, since_s=3600)) >= 4
        newer = ev.query(ctx.conn, after_id=all_[1].id)
        assert [e.kind for e in newer] == ["error"]
    e = all_[1]
    assert e.camera == "Камера 1" and e.kind_text == "Потік втрачено" and len(e.local_time) == 19
    out = tmp_path / "журнал.csv"
    ev.export_csv(all_, str(out))
    text = out.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0] == "Час;Камера;Подія;Повідомлення" and "Потік втрачено" in text


def test_error_reporter(ctx, tmp_path):
    log_path = setup_log_file(ctx.settings.db_path())
    assert log_path == str(tmp_path / "polar.log")
    shown = []
    rep = ErrorReporter(ctx)
    rep.on_error = lambda short, details: shown.append((short, details))
    try:
        1 / 0
    except ZeroDivisionError:
        short = rep.report(*sys.exc_info(), where="detect-Камера 1")
    assert short.startswith("[detect-Камера 1] ZeroDivisionError")
    assert len(shown) == 1 and "Traceback" in shown[0][1]
    try:
        1 / 0
    except ZeroDivisionError:
        rep.report(*sys.exc_info(), where="detect-Камера 1")
    assert len(shown) == 1                                                    # повтор — без нового вікна
    with ctx.control._db_lock:
        assert ctx.conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'error'").fetchone()[0] == 2
    for h in __import__("logging").getLogger("polar").handlers:
        h.flush()
    assert "ZeroDivisionError" in open(log_path, encoding="utf-8").read()
    # необроблений виняток у потоці теж потрапляє сюди
    import threading
    rep.QUIET_S = 0
    rep.install()
    try:
        t = threading.Thread(target=lambda: [][1], name="worker")
        t.start()
        t.join()
    finally:
        rep.uninstall()
    assert any("IndexError" in s for s, _d in shown)
