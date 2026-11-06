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
