"""Plain-language activity feed built from the technical log.

The technical log (console tab and log file) is left untouched. This module only
*reads* the same records and turns the handful that matter to an observer into
short English sentences plus a one-line status summary. It has no Qt
dependency so it can be tested on its own.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Entry kinds, used by the GUI for icon and colour.
OK = "ok"
INFO = "info"
ACTION = "action"
WAIT = "wait"
WARN = "warn"
ERROR = "error"
STOP = "stop"
DELETE = "delete"

ICONS = {
    OK: "✔",
    INFO: "ℹ",
    ACTION: "▶",
    WAIT: "⏳",
    WARN: "⚠",
    ERROR: "✖",
    STOP: "■",
    DELETE: "🗑",
}

COLORS = {
    OK: "#2e7d32",
    INFO: "#455a64",
    ACTION: "#1565c0",
    WAIT: "#b26a00",
    WARN: "#e65100",
    ERROR: "#b00020",
    STOP: "#455a64",
    DELETE: "#6a1b9a",
}

MODEL_NAMES = {"dwarf3": "DWARF 3", "dwarf2": "DWARF 2", "dwarfmini": "DWARF mini"}

SESSION_END_REASONS = {
    "settings_changed_or_not_running": "exposure, gain, filter or binning changed",
    "abort": "exposure aborted",
    "camera_disconnect": "NINA disconnected the camera",
    "telescope_slew": "telescope slew",
    "telescope_move_axis": "telescope moved",
    "focuser_move": "focuser moved",
    "filter_change": "filter changed",
    "idle_timeout": "no new exposure was requested",
    "device_stopped": "the DWARF stopped capturing",
    "frame_timeout": "no new frame arrived in time",
    "shutdown": "the app is closing",
}

CLEANUP_SKIP_REASONS = {
    "album_entry_not_found": "the DWARF did not list the session in time",
    "album_entry_incomplete": "the album entry was incomplete",
    "no_dwarf_raw_folder": "the capture folder was not recognised",
}

LOW_BATTERY_PERCENT = 20

_KV_RE = re.compile(r"(\w+)=(\S+)")
_UVICORN_RE = re.compile(r"Uvicorn running on \S+?:(\d+)")


@dataclass
class ActivityEntry:
    timestamp: float
    kind: str
    text: str
    # Consecutive entries with the same group are merged by the GUI (×N).
    group: Optional[str] = None


@dataclass
class ActivityStatus:
    model: Optional[str] = None
    ip: Optional[str] = None
    connection: str = "not connected"
    camera: str = "idle"
    session_frames: Optional[int] = None
    filter_name: Optional[str] = None
    telescope: Optional[str] = None
    battery: Optional[int] = None

    def render(self) -> str:
        device = MODEL_NAMES.get(self.model or "", "DWARF")
        connection = self.connection
        if self.ip and self.connection in {"connected", "not responding"}:
            connection = f"{self.connection} ({self.ip})"
        parts = [f"{device} · {connection}", f"Camera: {self.camera}"]
        if self.session_frames is not None:
            noun = "frame" if self.session_frames == 1 else "frames"
            parts.append(f"Continuous session: {self.session_frames} {noun}")
        if self.filter_name:
            parts.append(f"Filter: {self.filter_name}")
        if self.telescope:
            parts.append(f"Telescope: {self.telescope}")
        if self.battery is not None:
            parts.append(f"Battery {self.battery} %")
        return "  |  ".join(parts)


def _num(value: Any, digits: int = 0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits == 0:
        return f"{number:.0f}" if number >= 10 or number.is_integer() else f"{number:g}"
    return f"{number:.{digits}f}"


def _seconds(value: Any) -> str:
    try:
        return f"{float(value):.0f}"
    except (TypeError, ValueError):
        return str(value)


def _short_filter(name: Any) -> Optional[str]:
    if not isinstance(name, str) or not name.strip():
        return None
    return name.replace(" Filter", "").strip()


def parse_record(message: str) -> tuple[Optional[str], dict[str, Any]]:
    """Return (event, fields) for a structlog JSON line or a ``name k=v`` line."""

    text = message.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return None, {}
        if isinstance(data, dict):
            event = str(data.pop("event", "") or "")
            # Some events are logged printf-style: "name ip=%s mode=%s".
            if " " in event:
                event = event.split(" ", 1)[0]
            return event or None, data
        return None, {}
    head, _, rest = text.partition(" ")
    return head or None, dict(_KV_RE.findall(rest))


class ActivityTranslator:
    """Turn log records into activity entries and keep the status line."""

    def __init__(self) -> None:
        self.status = ActivityStatus()
        self._last_size: Optional[tuple[int, int]] = None
        self._continuous = False
        self._stopping_since: Optional[float] = None
        self._goto_warning_shown = False
        self._low_battery_warned = False
        self._released_at: Optional[float] = None
        self._handlers: dict[str, Callable[[dict[str, Any], float], list[ActivityEntry]]] = {
            "cli.start.profile": self._on_profile,
            "cli.start.connected": self._on_cli_connected,
            "server.starting": self._on_server_starting,
            "server.stopped": self._on_server_stopped,
            "dwarf.system.master_lock_acquired": self._on_lock_acquired,
            "dwarf.system.master_lock_unlocked": self._on_lock_released,
            "dwarf.ws.connect_failed": self._on_connect_failed,
            "dwarf.camera.disconnect.local_only": self._on_camera_disconnect,
            "filterwheel.position_set": self._on_filter,
            "filterwheel.initialize_position": self._on_filter,
            "alpaca.camera.start_exposure_request": self._on_exposure_request,
            "alpaca.camera.gain_rejected": self._on_gain_rejected,
            "dwarf.camera.v3_astro_params_applied": self._on_params_applied,
            "dwarf.camera.start_deferred_until_ready": self._on_ready_wait,
            "dwarf.camera.astro_capture_ready_wait_started": self._on_ready_wait,
            "dwarf.camera.astro_capture_ready_wait_finished": self._on_ready_done,
            "dwarf.camera.astro_capture_ready_wait_timeout": self._on_ready_timeout,
            "dwarf.camera.deferred_start_failed": self._on_start_failed,
            "dwarf.camera.astro_capture_state": self._on_capture_state,
            "dwarf.camera.astro_capture_goto_missing": self._on_goto_missing,
            "dwarf.camera.dark_library_missing_continuing": self._on_no_darks,
            "dwarf.camera.continuous_session_started": self._on_session_started,
            "dwarf.camera.continuous_frame_delivered": self._on_continuous_frame,
            "dwarf.camera.continuous_frame_skipped": self._on_frame_skipped,
            "dwarf.camera.continuous_frame_failed": self._on_frame_failed,
            "dwarf.camera.continuous_session_ended": self._on_session_ended,
            "dwarf.camera.frame_stored": self._on_frame_stored,
            "dwarf.camera.astro_cleanup_deleted": self._on_cleanup_deleted,
            "dwarf.camera.astro_cleanup_skipped": self._on_cleanup_skipped,
            "dwarf.camera.astro_cleanup_failed": self._on_cleanup_failed,
            "dwarf.telescope.goto.one_click.starting": self._on_goto_start,
            "dwarf.telescope.goto.retrying": self._on_goto_retry,
            "dwarf.telescope.goto.wait_result": self._on_goto_result,
            "dwarf.telescope.goto.wait_timeout": self._on_goto_timeout,
            "dwarf.telescope.goto.timeout": self._on_goto_timeout,
            "dwarf.telescope.calibration.starting": self._on_calibration_start,
            "dwarf.telescope.calibration.completed": self._on_calibration_done,
            "dwarf.telescope.calibration.outcome": self._on_calibration_outcome,
            "dwarf.tracking.notification.state": self._on_tracking,
            "dwarf.focus.move.completed": self._on_focus_moved,
            "dwarf.focus.autofocus.before_calibration.starting": self._on_autofocus_start,
            "dwarf.focus.autofocus.before_calibration.completed": self._on_autofocus_done,
            "dwarf.focus.autofocus.before_calibration.failed": self._on_autofocus_failed,
            "dwarf.battery.notification": self._on_battery,
            "http.request.error": self._on_http_error,
        }

    # region entry point
    def configure(self, *, model: Optional[str], ip: Optional[str]) -> None:
        """Seed the status line from the settings used to start the server."""

        if model:
            self.status.model = model
        if ip:
            self.status.ip = ip

    def feed(self, message: str, created: float) -> list[ActivityEntry]:
        uvicorn = _UVICORN_RE.search(message)
        if uvicorn:
            return [
                ActivityEntry(created, OK, f"Alpaca server running on port {uvicorn.group(1)}")
            ]
        event, fields = parse_record(message)
        if not event:
            return []
        handler = self._handlers.get(event)
        if handler is None:
            return []
        try:
            return handler(fields, created)
        except Exception:  # never let a formatting problem break the GUI
            return []

    # endregion

    # region connection and server
    def _on_profile(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.model = str(f.get("model") or self.status.model or "")
        return []

    def _on_cli_connected(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.ip = f.get("ip") or self.status.ip
        return []

    def _on_server_starting(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [ActivityEntry(t, INFO, f"Starting Alpaca server on port {f.get('port')}")]

    def _on_server_stopped(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.connection = "not connected"
        self.status.camera = "idle"
        self.status.session_frames = None
        return [ActivityEntry(t, STOP, "Alpaca server stopped")]

    def _on_lock_acquired(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        args = f.get("positional_args") or []
        ip = f.get("ip") or (args[0] if args else None) or self.status.ip
        self.status.ip = ip
        reconnect = self._released_at is not None and t - self._released_at < 5.0
        already = self.status.connection == "connected"
        self._released_at = None
        self.status.connection = "connected"
        if reconnect or already:
            # Start-up checks and camera reconnects release and re-take control
            # within a second; that is not news for the observer.
            return []
        device = MODEL_NAMES.get(self.status.model or "", "DWARF")
        return [ActivityEntry(t, OK, f"Connected to {device} at {ip}")]

    def _on_lock_released(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.connection = "released"
        self._released_at = t
        return []

    def _on_connect_failed(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.connection = "not responding"
        ip = self.status.ip or "the configured IP"
        return [
            ActivityEntry(
                t,
                ERROR,
                f"DWARF not responding at {ip}. Check the IP and that it is on the "
                "same Wi-Fi network",
                group="connect_failed",
            )
        ]

    def _on_camera_disconnect(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.camera = "disconnected"
        return [ActivityEntry(t, INFO, "Camera disconnected by NINA")]

    # endregion

    # region camera
    def _on_filter(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        name = _short_filter(f.get("filter"))
        if not name or name == self.status.filter_name:
            return []
        self.status.filter_name = name
        return [ActivityEntry(t, INFO, f"Filter: {name}")]

    def _on_exposure_request(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        duration = f.get("resolved_duration")
        light = f.get("resolved_light", True)
        kind = "light" if light in (True, "True", "true", None) else "dark"
        self.status.camera = f"exposing {_num(duration)} s"
        return [ActivityEntry(t, ACTION, f"NINA requested a {_num(duration)} s {kind} exposure")]

    def _on_gain_rejected(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [
            ActivityEntry(
                t,
                ERROR,
                f"NINA requested gain {f.get('requested')}: out of range "
                f"({f.get('minimum')}–{f.get('maximum')}). Check NINA's Default Gain",
                group="gain_rejected",
            )
        ]

    def _on_params_applied(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [
            ActivityEntry(
                t, INFO, f"DWARF set to {_num(f.get('exposure'))} s, gain {f.get('gain')}"
            )
        ]

    def _on_ready_wait(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.camera = "waiting for the DWARF"
        return [
            ActivityEntry(
                t,
                WAIT,
                "Waiting for the DWARF to close the previous session…",
                group="ready_wait",
            )
        ]

    def _on_ready_done(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [ActivityEntry(t, OK, f"DWARF ready after {_seconds(f.get('elapsed_seconds'))} s")]

    def _on_ready_timeout(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.camera = "DWARF busy"
        return [
            ActivityEntry(t, ERROR, f"DWARF still busy after {_seconds(f.get('timeout'))} s")
        ]

    def _on_start_failed(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.camera = "failed"
        return [ActivityEntry(t, ERROR, f"Exposure could not start: {f.get('error')}")]

    def _on_capture_state(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        name = f.get("state_name")
        if name == "stopping":
            if self._stopping_since is None:
                self._stopping_since = t
            return []
        if name == "stopped" and self._stopping_since is not None:
            elapsed = t - self._stopping_since
            self._stopping_since = None
            return [ActivityEntry(t, OK, f"DWARF finished closing the session ({elapsed:.0f} s)")]
        if name in {"running", "idle"}:
            self._stopping_since = None
        return []

    def _on_goto_missing(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        if self._goto_warning_shown:
            return []
        self._goto_warning_shown = True
        return [
            ActivityEntry(
                t,
                INFO,
                "No GOTO from NINA in this session: the DWARF images wherever it points",
            )
        ]

    def _on_no_darks(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [
            ActivityEntry(
                t,
                WARN,
                "No dark library for these settings: continuing without darks",
                group="no_darks",
            )
        ]

    def _on_session_started(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self._continuous = True
        self.status.session_frames = 0
        return [ActivityEntry(t, OK, "Continuous DWARF session started")]

    def _on_frame_stored(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        try:
            self._last_size = (int(f.get("width")), int(f.get("height")))
        except (TypeError, ValueError):
            self._last_size = None
        if self._continuous:
            return []  # reported by continuous_frame_delivered
        self.status.camera = "frame ready"
        size = f" ({self._last_size[0]}×{self._last_size[1]})" if self._last_size else ""
        return [ActivityEntry(t, OK, f"Frame delivered to NINA{size}")]

    def _on_continuous_frame(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        number = int(f.get("frames_delivered") or 0)
        self.status.session_frames = number
        self.status.camera = "frame ready"
        size = ""
        if number == 1 and self._last_size:
            size = f" ({self._last_size[0]}×{self._last_size[1]})"
        delay = ""
        try:
            delay = f" · {max(0.0, t - float(f.get('frame_end'))):.0f} s after the exposure ended"
        except (TypeError, ValueError):
            pass
        if f.get("stacking_failed"):
            return [
                ActivityEntry(
                    t,
                    WARN,
                    f"Frame {number} delivered to NINA{size}{delay} · the DWARF could not "
                    "stack it (few stars, clouds or trailing?)",
                )
            ]
        return [ActivityEntry(t, OK, f"Frame {number} delivered to NINA{size}{delay}")]

    def _on_frame_skipped(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [
            ActivityEntry(
                t,
                WARN,
                "Frame skipped: it finished before NINA asked (Wi-Fi download slower "
                "than the exposures); it stays on the DWARF",
                group="frame_skipped",
            )
        ]

    def _on_frame_failed(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.camera = "failed"
        reason = SESSION_END_REASONS.get(str(f.get("reason")), str(f.get("reason")))
        return [ActivityEntry(t, ERROR, f"No frame from the DWARF: {reason}")]

    def _on_session_ended(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self._continuous = False
        self.status.session_frames = None
        reason = SESSION_END_REASONS.get(str(f.get("reason")), str(f.get("reason")))
        delivered = f.get("frames_delivered", 0)
        noun = "frame" if delivered == 1 else "frames"
        return [
            ActivityEntry(
                t, STOP, f"Continuous session ended: {reason} ({delivered} {noun} delivered)"
            )
        ]

    def _on_cleanup_deleted(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        if f.get("still_present"):
            return [
                ActivityEntry(
                    t, WARN, "The DWARF accepted the deletion but still lists the session"
                )
            ]
        return [ActivityEntry(t, DELETE, f"Session deleted from the DWARF: {f.get('folder')}")]

    def _on_cleanup_skipped(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        reason = CLEANUP_SKIP_REASONS.get(str(f.get("reason")), str(f.get("reason")))
        return [ActivityEntry(t, WARN, f"Session not deleted: {reason}")]

    def _on_cleanup_failed(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [ActivityEntry(t, ERROR, f"Deleting the session failed: {f.get('error')}")]

    # endregion

    # region telescope and focuser
    def _on_goto_start(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self._goto_warning_shown = False
        self.status.telescope = "slewing"
        target = f.get("target_name")
        if not target:
            target = f"RA {_num(f.get('ra_hours'), 3)} h, Dec {_num(f.get('dec_degrees'), 2)}°"
        return [
            ActivityEntry(
                t, ACTION, f"GOTO to {target} (the DWARF may calibrate first; this can take minutes)"
            )
        ]

    def _on_goto_retry(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [ActivityEntry(t, WARN, f"GOTO retrying (DWARF code {f.get('code')})", group="goto_retry")]

    def _on_goto_result(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        if f.get("result") == "success":
            self.status.telescope = "tracking"
            return [ActivityEntry(t, OK, "GOTO completed: tracking the target")]
        self.status.telescope = "GOTO failed"
        reason = f.get("reason") or f.get("result")
        return [ActivityEntry(t, ERROR, f"GOTO failed: {reason}")]

    def _on_goto_timeout(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.telescope = "GOTO timed out"
        return [ActivityEntry(t, ERROR, "GOTO timed out", group="goto_timeout")]

    def _on_calibration_start(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        self.status.telescope = "calibrating"
        return [ActivityEntry(t, WAIT, "Calibrating (plate solving)…")]

    def _on_calibration_done(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [ActivityEntry(t, OK, "Calibration completed")]

    def _on_calibration_outcome(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        outcome = f.get("outcome")
        if outcome == "failed":
            self.status.telescope = "calibration failed"
            return [ActivityEntry(t, ERROR, f"Calibration failed: {f.get('error')}")]
        if outcome == "not_confirmed":
            return [ActivityEntry(t, WARN, "Calibration not confirmed by the DWARF")]
        return []

    def _on_tracking(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        state = str(f.get("state") or "").lower()
        if "track" in state or "running" in state:
            self.status.telescope = "tracking"
        elif "stop" in state or "idle" in state:
            self.status.telescope = "not tracking"
        return []

    def _on_focus_moved(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [ActivityEntry(t, INFO, f"Focuser at {f.get('position')}", group="focus")]

    def _on_autofocus_start(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [ActivityEntry(t, WAIT, "Autofocus running…")]

    def _on_autofocus_done(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [ActivityEntry(t, OK, "Autofocus completed")]

    def _on_autofocus_failed(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [ActivityEntry(t, ERROR, f"Autofocus failed: {f.get('error')}")]

    # endregion

    # region misc
    def _on_battery(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        try:
            percent = int(f.get("battery_percent"))
        except (TypeError, ValueError):
            return []
        self.status.battery = percent
        if percent <= LOW_BATTERY_PERCENT and not self._low_battery_warned:
            self._low_battery_warned = True
            return [ActivityEntry(t, WARN, f"DWARF battery low: {percent} %")]
        if percent > LOW_BATTERY_PERCENT + 5:
            self._low_battery_warned = False
        return []

    def _on_http_error(self, f: dict[str, Any], t: float) -> list[ActivityEntry]:
        return [
            ActivityEntry(
                t,
                ERROR,
                "Internal error while handling a NINA request (details in the Technical tab)",
                group="http_error",
            )
        ]

    # endregion
