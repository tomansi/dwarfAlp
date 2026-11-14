import json

import pytest

from dwarf_alpaca.gui.activity import (
    DELETE,
    ERROR,
    OK,
    STOP,
    WARN,
    ActivityTranslator,
    parse_record,
)


def _js(event: str, **fields) -> str:
    return json.dumps({"event": event, **fields})


def _texts(entries):
    return [entry.text for entry in entries]


def test_parse_structlog_json_and_key_value_lines() -> None:
    assert parse_record(_js("dwarf.battery.notification", battery_percent=90)) == (
        "dwarf.battery.notification",
        {"battery_percent": 90},
    )
    assert parse_record("cli.start.profile model=dwarf3 ws_client_id=X") == (
        "cli.start.profile",
        {"model": "dwarf3", "ws_client_id": "X"},
    )
    event, fields = parse_record(
        _js(
            "dwarf.system.master_lock_acquired ip=%s mode=%s lock=%s",
            positional_args=["192.168.7.81", 0, True],
        )
    )
    assert event == "dwarf.system.master_lock_acquired"


def test_continuous_sequence_reads_like_a_story() -> None:
    tr = ActivityTranslator()
    tr.configure(model="dwarf3", ip="192.168.7.81")
    feed = []
    feed += tr.feed(_js("dwarf.system.master_lock_acquired", positional_args=["192.168.7.81"]), 0)
    feed += tr.feed(_js("dwarf.system.master_lock_unlocked"), 0.1)
    feed += tr.feed(_js("dwarf.system.master_lock_acquired", positional_args=["192.168.7.81"]), 0.2)
    feed += tr.feed(_js("filterwheel.position_set", filter="Astro Filter"), 1)
    feed += tr.feed(_js("alpaca.camera.start_exposure_request", resolved_duration=30.0, resolved_light=True), 2)
    feed += tr.feed(_js("dwarf.camera.continuous_session_started", device_frames=999), 3)
    feed += tr.feed(_js("dwarf.camera.frame_stored", width=3856, height=2180), 49)
    feed += tr.feed(_js("dwarf.camera.continuous_frame_delivered", frames_delivered=1, frame_end=38.0), 49)
    feed += tr.feed(_js("dwarf.camera.continuous_frame_skipped", path="x"), 60)
    feed += tr.feed(_js("dwarf.camera.continuous_session_ended", reason="camera_disconnect", frames_delivered=1), 100)
    feed += tr.feed(_js("dwarf.camera.astro_capture_state", state_name="stopping"), 100)
    feed += tr.feed(_js("dwarf.camera.astro_capture_state", state_name="stopped"), 140)
    feed += tr.feed(_js("dwarf.camera.astro_cleanup_deleted", folder="DWARF_RAW_X", still_present=False), 142)

    assert _texts(feed) == [
        "Connected to DWARF 3 at 192.168.7.81",
        "Filter: Astro",
        "NINA requested a 30 s light exposure",
        "Continuous DWARF session started",
        "Frame 1 delivered to NINA (3856×2180) · 11 s after the exposure ended",
        "Frame skipped: it finished before NINA asked (Wi-Fi download slower than the "
        "exposures); it stays on the DWARF",
        "Continuous session ended: NINA disconnected the camera (1 frame delivered)",
        "DWARF finished closing the session (40 s)",
        "Session deleted from the DWARF: DWARF_RAW_X",
    ]
    kinds = [entry.kind for entry in feed]
    assert kinds[-1] == DELETE and STOP in kinds and WARN in kinds and kinds[0] == OK


def test_status_line_tracks_connection_camera_and_session() -> None:
    tr = ActivityTranslator()
    tr.configure(model="dwarf3", ip="192.168.7.81")
    tr.feed(_js("dwarf.system.master_lock_acquired", positional_args=["192.168.7.81"]), 0)
    tr.feed(_js("dwarf.battery.notification", battery_percent=100), 0)
    tr.feed(_js("filterwheel.position_set", filter="Duo-Band Filter"), 0)
    tr.feed(_js("alpaca.camera.start_exposure_request", resolved_duration=60.0), 0)
    tr.feed(_js("dwarf.camera.continuous_session_started"), 0)
    tr.feed(_js("dwarf.camera.continuous_frame_delivered", frames_delivered=2, frame_end=0), 0)
    assert tr.status.render() == (
        "DWARF 3 · connected (192.168.7.81)  |  Camera: frame ready  |  "
        "Continuous session: 2 frames  |  Filter: Duo-Band  |  Battery 100 %"
    )


def test_errors_are_explained() -> None:
    tr = ActivityTranslator()
    tr.configure(model="dwarf3", ip="192.168.7.81")
    entries = tr.feed(_js("dwarf.ws.connect_failed", error="timed out"), 0)
    entries += tr.feed(_js("alpaca.camera.gain_rejected", requested=800, minimum=0, maximum=200), 1)
    entries += tr.feed(_js("dwarf.camera.astro_cleanup_skipped", reason="album_entry_not_found"), 2)
    assert [entry.kind for entry in entries] == [ERROR, ERROR, WARN]
    assert "192.168.7.81" in entries[0].text and entries[0].group == "connect_failed"
    assert "800" in entries[1].text and "Default Gain" in entries[1].text
    assert "did not list the session" in entries[2].text
    assert "not responding (192.168.7.81)" in tr.status.render()


@pytest.mark.parametrize(
    "message",
    [
        _js("dwarf.ws.command.send", module_id=1),
        _js("dwarf.camera.capture.trace.packet", command_id=15264),
        "not json at all",
        "{broken json",
    ],
)
def test_technical_noise_is_ignored(message) -> None:
    assert ActivityTranslator().feed(message, 0) == []


def test_low_battery_warns_once() -> None:
    tr = ActivityTranslator()
    first = tr.feed(_js("dwarf.battery.notification", battery_percent=18), 0)
    second = tr.feed(_js("dwarf.battery.notification", battery_percent=17), 1)
    assert len(first) == 1 and first[0].kind == WARN and second == []
