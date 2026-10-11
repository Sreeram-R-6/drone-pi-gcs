"""Checked pixel references. Update only after deliberate visual review."""

import os
from pathlib import Path
import pytest
from PIL import Image
from drone_oled.app import SCREENS
from drone_oled.fsm import FSM
from drone_oled.missions import MissionStore
from drone_oled.model import TelemetryStore
from drone_oled.render import render

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    (name, page, live)
    for name in SCREENS
    for page in (range(5) if name == "status" else (0,))
    for live in (False, True)
]


@pytest.mark.parametrize("screen,page,live", CASES)
def test_golden_pixels(config, profile, ready_store, screen, page, live):
    fsm = FSM(config, profile, MissionStore(ROOT / "missions").load_all())
    fsm.state.id, fsm.state.page = screen, page
    fsm.state.deadline = 110
    fsm.state.confirm_kind = "arm"
    fsm.state.message = "NO FC LINK; CONTROLS DISABLED"
    store = ready_store if live else TelemetryStore()
    frame = render(fsm, store.snapshot(100), 100)
    path = ROOT / "tests/goldens" / f"{screen}-{page}-{int(live)}.png"
    if os.environ.get("DRONE_OLED_UPDATE_GOLDENS") == "1":
        path.parent.mkdir(exist_ok=True)
        frame.save(path)
    assert path.exists(), "Missing reviewed golden " + str(path)
    with Image.open(path) as reference:
        assert reference.mode == "1" and reference.size == (128, 64)
        assert frame.tobytes() == reference.tobytes()
