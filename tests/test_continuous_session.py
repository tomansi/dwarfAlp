import asyncio

import pytest

from dwarf_alpaca.config.settings import Settings
from dwarf_alpaca.dwarf.ftp_client import FtpPhotoEntry
from dwarf_alpaca.dwarf.session import DwarfSession, _ContinuousAstroSession, _OperationState

FOLDER = "/Astronomy/DWARF_RAW_TELE_EXP_5_GAIN_40_2026-11-13-01-41-46-346"


def _entry(name: str, end: float) -> FtpPhotoEntry:
    return FtpPhotoEntry(directory=FOLDER, name=name, timestamp=end, path=f"{FOLDER}/{name}")


def test_select_picks_oldest_fresh_frame_and_skips_stale_ones() -> None:
    entries = [_entry("a.fits", 100.0), _entry("b.fits", 106.0), _entry("c.fits", 112.0)]
    chosen, skipped = DwarfSession._select_continuous_frame(
        entries,
        delivered=set(),
        duration=5.0,
        valid_after=90.0,
        requested_at=105.0,
    )
    assert chosen is not None and chosen.name == "b.fits"
    assert [entry.name for entry in skipped] == ["a.fits"]


def test_select_ignores_delivered_and_pre_session_frames() -> None:
    entries = [_entry("old.fits", 60.0), _entry("a.fits", 100.0), _entry("b.fits", 106.0)]
    chosen, skipped = DwarfSession._select_continuous_frame(
        entries,
        delivered={f"{FOLDER}/a.fits"},
        duration=5.0,
        valid_after=90.0,
        requested_at=90.0,
    )
    assert chosen is not None and chosen.name == "b.fits"
    assert skipped == []


def test_select_waits_when_no_frame_finished_after_request() -> None:
    chosen, skipped = DwarfSession._select_continuous_frame(
        [_entry("a.fits", 100.0)],
        delivered=set(),
        duration=5.0,
        valid_after=90.0,
        requested_at=110.0,
    )
    assert chosen is None
    assert [entry.name for entry in skipped] == ["a.fits"]


def _session(**overrides) -> DwarfSession:
    settings = Settings(
        force_simulation=True,
        dwarf_device_model="dwarf3",
        continuous_astro_session=True,
        **overrides,
    )
    session = DwarfSession(settings)
    state = session.camera_state
    state.requested_gain = 40
    state.filter_index = 2
    state.requested_bin = (1, 1)
    return session


def test_continuous_disabled_by_default() -> None:
    assert Settings().continuous_astro_session is False


def test_reuse_requires_same_settings_running_device_and_known_folder() -> None:
    session = _session()
    session._continuous = _ContinuousAstroSession(
        key=session._continuous_key(5.0, True), started_at=0.0, first_capture_id="c0"
    )
    session._astro_capture_operation_state = _OperationState.RUNNING
    assert not session._continuous_can_reuse(5.0, True)  # first frame not delivered yet

    session._continuous.folder = FOLDER
    assert session._continuous_can_reuse(5.0, True)
    assert not session._continuous_can_reuse(10.0, True)

    session.camera_state.requested_gain = 80
    assert not session._continuous_can_reuse(5.0, True)
    session.camera_state.requested_gain = 40

    session._astro_capture_operation_state = _OperationState.STOPPING
    assert not session._continuous_can_reuse(5.0, True)


@pytest.mark.asyncio
async def test_end_session_stops_device_and_queues_folder_cleanup() -> None:
    session = _session(delete_astro_session_after_download=True)
    session._continuous = _ContinuousAstroSession(
        key=session._continuous_key(5.0, True),
        started_at=0.0,
        first_capture_id="c0",
        folder=FOLDER,
    )
    session._astro_capture_operation_state = _OperationState.RUNNING
    stops: list[bool] = []
    cleanups: list[str] = []

    async def fake_stop(*, strict: bool = False) -> None:
        stops.append(strict)

    session._stop_astro_capture = fake_stop  # type: ignore[method-assign]
    session._schedule_astro_folder_cleanup = (  # type: ignore[method-assign]
        lambda folder, capture_id=None: cleanups.append(folder)
    )

    await session._end_continuous_session(reason="test")

    assert session._continuous is None
    assert stops == [False]
    assert cleanups == [FOLDER.rsplit("/", 1)[-1]]
    assert session._last_continuous_folder == FOLDER
    await asyncio.sleep(0)
