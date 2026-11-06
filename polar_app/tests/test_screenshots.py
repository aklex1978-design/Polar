import json
import os

import numpy as np

from polar_app.screenshots import GridSnapshots
from polar_core.sync import FrameAngles, StampedFrame


def frame(i, az, el, moving=False):
    return StampedFrame(np.full((4, 6, 3), i, np.uint8), float(i), i, FrameAngles(az, el, moving, float(i), 0.0))


def test_nodes_follow_half_fov(tmp_path):
    # HFOV 45° → крок 22,5°; ряди Навчання 0, 17, 34 …
    g = GridSnapshots(str(tmp_path / "s"), north_offset_deg=15.0, az_step_deg=22.5, elevations=[0, 17, 34])
    g.reset()
    assert g.total == 3 * 16
    assert g.offer(frame(1, 30.2, 17.1, moving=True)) is None      # у русі — не зберігається
    assert g.offer(frame(2, 30.2, 17.1))                          # від півночі 45,2° → вузол 45°, ряд 17°
    assert g.offer(frame(3, 30.0, 17.0)) is None                  # вузол уже є
    assert g.offer(frame(4, 40.0, 17.0)) is None                  # не на вузлі
    assert g.offer(frame(5, 352.5, 34.0)) is None                 # від півночі 7,5° — між вузлами
    assert g.offer(frame(6, 345.0, 34.0))                         # від півночі 0° → вузол 0°
    idx = json.load(open(tmp_path / "s" / "index.json", encoding="utf-8"))
    assert idx["azimuth_step_deg"] == 22.5 and idx["elevations_deg"] == [0.0, 17.0, 34.0]
    assert "17/045" in idx["snapshots"]
    assert os.path.isfile(tmp_path / "s" / "el_17" / "az_045.jpg")


def test_fractional_names(tmp_path):
    g = GridSnapshots(str(tmp_path / "s"), north_offset_deg=0.0, az_step_deg=22.5, elevations=[0])
    g.reset()
    assert g.offer(frame(1, 22.5, 0.0)).endswith(os.path.join("el_00", "az_022.5.jpg"))


def test_manual_save_keeps_index(tmp_path):
    g = GridSnapshots(str(tmp_path / "s"), north_offset_deg=0.0, az_step_deg=45.0, elevations=[0, 30])
    g.reset()
    assert g.offer(frame(1, 45.0, 30.0))
    g2 = GridSnapshots(str(tmp_path / "s"), north_offset_deg=0.0, az_step_deg=45.0, elevations=[0, 30])
    g2.load()                                                     # набір Навчання зберігається
    assert g2.save(frame(2, 45.1, 30.0)).endswith(os.path.join("el_30", "az_045.jpg"))   # вузол — замінено
    off = g2.save(frame(3, 60.0, 12.5))                           # поза вузлом — у manual/
    assert off.endswith(os.path.join("manual", "el_12.5_az_060.jpg")) and os.path.isfile(off)
    idx = json.load(open(tmp_path / "s" / "index.json", encoding="utf-8"))["snapshots"]
    assert idx["30/045"]["manual"] and "manual/12.5/060" in idx and g2.count == 1


def test_step_helpers():
    from polar_core.learning import elevation_rows, next_azimuth, next_row
    rows = elevation_rows(60.0, 0.0, 90.0)
    assert rows == [0.0, 30.0, 60.0]
    assert next_row(rows, 0.2, +1) == 30.0 and next_row(rows, 30.0, -1) == 0.0
    assert next_row(rows, 45.0, -1) == 30.0 and next_row(rows, 60.0, +1) == 60.0   # між рядами / край
    assert next_azimuth(0.0, 45.0, +1) == 45.0 and next_azimuth(0.3, 45.0, -1) == 315.0
    assert next_azimuth(50.0, 45.0, -1) == 45.0 and next_azimuth(44.8, 45.0, +1) == 90.0


def test_nearest_snapshot_follows_position(tmp_path):
    from polar_app.screenshots import nearest_snapshot
    d = str(tmp_path / "s")
    g = GridSnapshots(d, north_offset_deg=0.0, az_step_deg=45.0, elevations=[0, 30, 60])
    g.reset()
    assert nearest_snapshot(d, 0.0, 0.0) is None
    g.offer(frame(1, 0.0, 0.0))
    g.offer(frame(2, 45.0, 30.0))
    path, e = nearest_snapshot(d, 10.0, 5.0)                       # клітинка вузла 0°/0°
    assert path.endswith(os.path.join("el_00", "az_000.jpg"))
    assert nearest_snapshot(d, 350.0, 14.0)[1]["azimuth_node"] == 0.0     # через 0/360
    assert nearest_snapshot(d, 50.0, 31.0)[1]["azimuth_node"] == 45.0     # перейшли — інший знімок
    assert nearest_snapshot(d, 90.0, 60.0) is None                 # для цього положення знімка немає
