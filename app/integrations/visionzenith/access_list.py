from datetime import datetime
import json
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FIELDS = {
    "plate",
    "index",
    "create_time",
    "enable_time",
    "overdue_time",
    "enable",
    "need_alarm",
    "time_seg_enable",
    "seg_time_start",
    "seg_time_end",
    "vehicle_comment",
    "customer_id",
    "vehicle_code",
}


def delete_camera_access_record(plate: object) -> dict[str, str]:
    clean = str(plate or "").strip().upper()
    if not clean:
        raise ValueError("An explicit plate is required for single-record deletion")
    return {"operator_type": "delete", "plate": clean}


def delete_all_camera_access_records() -> dict[str, str]:
    # Official documentation defines an omitted plate as delete-all. This is
    # deliberately separate from single deletion so empty input cannot escalate.
    return {"operator_type": "delete"}

READBACK_VERIFIED = "READBACK_VERIFIED"
READBACK_MISMATCH = "READBACK_MISMATCH"
READBACK_UNVERIFIED = "READBACK_UNVERIFIED"


def merge_record(
    plate: str,
    existing: dict[str, Any] | None,
    changes: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    record = {
        "plate": plate,
        "create_time": now or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "enable": 1,
        "need_alarm": 0,
        "time_seg_enable": 0,
        "seg_time_start": "00:00:00",
        "seg_time_end": "00:00:00",
    }
    if existing:
        record.update(
            {key: value for key, value in existing.items() if key in FIELDS}
        )
    record.update(changes)
    record["plate"] = plate
    return record


def select_record(reply: dict[str, Any], plate: str) -> dict[str, Any] | None:
    body = (reply.get("payload") or {}).get("body")
    if not isinstance(body, dict):
        raise ValueError("Unexpected access-list query reply")
    if body.get("state_code", 200) not in (200, "200"):
        raise ValueError("Access-list query was rejected")
    if (
        "dldb_rec" in body
        and body["dldb_rec"] is None
        and body.get("state_code") in (200, "200")
    ):
        return None
    records = body.get("dldb_rec")
    if not isinstance(records, list):
        raise ValueError("Access-list query did not return a record array")
    matching = [
        record
        for record in records
        if isinstance(record, dict) and record.get("plate") == plate
    ]
    if len(matching) > 1:
        raise ValueError("Access-list query returned duplicate records")
    if records and not matching:
        raise ValueError(
            "Access-list query returned a different plate: "
            + json.dumps(records, ensure_ascii=True)
        )
    return matching[0] if matching else None


def verify_changes(
    record: dict[str, Any], changes: dict[str, Any]
) -> tuple[list[str], list[str]]:
    missing = [key for key in changes if key not in record]
    mismatches = [
        key
        for key, value in changes.items()
        if key in record and record[key] != value
    ]
    return missing, mismatches


def readback_status(
    record: dict[str, Any], changes: dict[str, Any]
) -> str:
    missing, mismatches = verify_changes(record, changes)
    if mismatches:
        return "READBACK_MISMATCH"
    if missing:
        return "READBACK_UNVERIFIED"
    return "READBACK_VERIFIED"


def desired_changes(
    *,
    list_type: str,
    enabled: bool,
    valid_from: datetime | None,
    valid_until: datetime | None,
    daily_window_enabled: bool,
    daily_start: Any,
    daily_end: Any,
    camera_comment: str | None,
    timezone_name: str,
) -> dict[str, Any]:
    changes: dict[str, Any] = {
        "need_alarm": 0 if list_type == "ALLOW" else 1,
        "enable": 1 if enabled else 0,
        "vehicle_comment": camera_comment or "",
    }
    if valid_from is not None:
        changes["enable_time"] = _camera_datetime(valid_from, timezone_name)
    if valid_until is not None:
        changes["overdue_time"] = _camera_datetime(valid_until, timezone_name)
    if daily_window_enabled:
        changes.update(
            time_seg_enable=1,
            seg_time_start=daily_start.strftime("%H:%M:%S"),
            seg_time_end=daily_end.strftime("%H:%M:%S"),
        )
    else:
        changes.update(
            time_seg_enable=0,
            seg_time_start="00:00:00",
            seg_time_end="00:00:00",
        )
    return changes


def acknowledgement_succeeded(reply: dict[str, Any]) -> bool:
    body = (reply.get("payload") or {}).get("body")
    nested = body.get("state_code") if isinstance(body, dict) else None
    return reply.get("code") in (200, "200") and nested in (None, 200, "200")


def _camera_datetime(value: datetime, timezone_name: str) -> str:
    try:
        display_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        display_zone = ZoneInfo("UTC")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(display_zone).strftime("%Y-%m-%d %H:%M:%S")
