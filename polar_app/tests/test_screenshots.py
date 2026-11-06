import json
import os

import numpy as np

from polar_app.screenshots import GridSnapshots
from polar_core.sync import FrameAngles, StampedFrame


def frame(i, az, el, moving=False):
    return StampedFrame(np.full((4, 6, 3), i, np.uint8), float(i), i, FrameAngles(az, el, moving, float(i), 0.0))


def test_nodes_and_replacement(tmp_path):
    g = GridSnapshots(str(tmp_path / "s"), north_offset_deg=15.0)
    g.reset()
    assert g.total == 8 * 36
    assert g.offer(frame(1, 35.2, 20.3, moving=True))           # азимут від півночі 50,2°, кут місця 20,3°
    assert g.offer(frame(2, 35.0, 20.0, moving=True)) is None   # вузол уже є, і знову з руху
    assert g.offer(frame(3, 35.1, 19.9))                        # нерухомий замінює знімок з руху
    assert g.offer(frame(4, 35.0, 20.0)) is None
    assert g.offer(frame(5, 40.0, 25.0)) is None                # не на вузлі
    assert g.offer(frame(6, 345.0, 90.0)) is None               # 90° — поза 10…80°
    assert g.offer(frame(7, 345.2, 10.0))                       # азимут 0,2° від півночі → вузол 0°
    assert g.count == 2
    idx = json.load(open(tmp_path / "s" / "index.json", encoding="utf-8"))
    assert idx["20/50"]["moving"] is False and idx["20/50"]["file"] == os.path.join("el_20", "az_050.jpg")
    assert idx["10/0"]["azimuth_deg"] == 0.2
    assert os.path.isfile(tmp_path / "s" / "el_10" / "az_000.jpg")
    g.reset()
    assert g.count == 0 and not os.path.exists(tmp_path / "s" / "el_10")
