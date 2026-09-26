from eirven_ai.companion import DesktopCompanion


def test_companion_anchor_stays_inside_work_area() -> None:
    anchor = {"x": 9999, "y": 9999}
    DesktopCompanion._clamp_anchor(anchor, 430, 214, 1920, 1080)
    assert anchor == {"x": 1490, "y": 818}

    anchor = {"x": -50, "y": -20}
    DesktopCompanion._clamp_anchor(anchor, 430, 214, 1920, 1080)
    assert anchor == {"x": 0, "y": 0}
