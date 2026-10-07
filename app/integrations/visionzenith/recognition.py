import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any


IMAGE_FIELDS = {
    "full_image_content",
    "small_image_content",
    "content",
    "binimg_content",
}


class RecognitionRejected(ValueError):
    pass


class RetainedRecognitionIgnored(RecognitionRejected):
    pass


@dataclass(frozen=True)
class RecognitionMessage:
    plate: str
    event_id: str | None
    record_id: str | None
    document: dict[str, Any]
    plate_result: dict[str, Any]
    captured_at: datetime | None
    confidence: float | None
    direction: str | None
    metadata: dict[str, Any]


def parse_recognition(payload: bytes, *, retained: bool = False) -> RecognitionMessage:
    if retained:
        raise RetainedRecognitionIgnored("Retained recognition ignored")
    try:
        document = json.loads(payload.decode("utf-8"))
        result = document["payload"]["AlarmInfoPlate"]["result"]["PlateResult"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RecognitionRejected("Malformed recognition payload") from error
    if not isinstance(document, dict) or not isinstance(result, dict):
        raise RecognitionRejected("Malformed recognition payload")
    encoded_plate = result.get("license")
    if not isinstance(encoded_plate, str):
        raise RecognitionRejected("Recognition plate is missing")
    try:
        plate = (
            base64.b64decode(encoded_plate, validate=True)
            .decode("gbk")
            .strip()
            .upper()
        )
    except (ValueError, UnicodeError) as error:
        raise RecognitionRejected("Recognition plate cannot be decoded") from error
    if not re.fullmatch(r"[A-Z0-9]{1,32}", plate):
        raise RecognitionRejected("Recognition plate format is unsupported")
    event_id = result.get("unique_id") or document.get("id")
    if event_id is not None and not isinstance(event_id, (str, int)):
        raise RecognitionRejected("Recognition event ID is invalid")
    return RecognitionMessage(
        plate=plate,
        event_id=str(event_id) if event_id is not None else None,
        record_id=_safe_text(result.get("plateid"), 80),
        document=document,
        plate_result=result,
        captured_at=_capture_time(result, document),
        confidence=_confidence(result),
        direction=_motion_direction(result.get("direction")),
        metadata=_allowlisted_metadata(result),
    )


def image_bytes(
    value: str, *, max_decoded_bytes: int | None = None
) -> tuple[bytes, str]:
    if not isinstance(value, str):
        raise ValueError("Image field must be a Base64 string")
    if value.startswith("data:"):
        value = value.split(",", 1)[1]
    compact = "".join(value.split())
    if max_decoded_bytes is not None and len(compact) > (max_decoded_bytes * 4 // 3) + 8:
        raise ValueError("Image exceeds the configured size limit")
    data = base64.b64decode(compact, validate=True)
    if max_decoded_bytes is not None and len(data) > max_decoded_bytes:
        raise ValueError("Image exceeds the configured size limit")
    if data.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
    elif data.startswith(b"BM"):
        extension = ".bmp"
    else:
        raise ValueError("Image is not a recognized JPEG, PNG or BMP")
    return data, extension


def metadata_only(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                f"<image omitted: {len(item)} Base64 characters>"
                if key in IMAGE_FIELDS and isinstance(item, str) and item
                else metadata_only(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [metadata_only(item) for item in value]
    return value


def image_candidates(plate_result: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    candidates = [
        ("scene", plate_result.get("full_image_content")),
        ("plate", plate_result.get("small_image_content")),
    ]
    for index, item in enumerate(plate_result.get("plates") or []):
        if isinstance(item, dict):
            candidates.append((f"plate_{index + 1}", item.get("content")))
    return tuple(
        (name, value)
        for name, value in candidates
        if isinstance(value, str) and value
    )


def _capture_time(
    result: dict[str, Any], document: dict[str, Any]
) -> datetime | None:
    timestamp = result.get("timeStamp")
    if isinstance(timestamp, dict):
        timeval = timestamp.get("Timeval")
        if isinstance(timeval, dict):
            seconds = timeval.get("sec")
            microseconds = timeval.get("usec", 0)
            if (
                isinstance(seconds, (int, float))
                and not isinstance(seconds, bool)
                and isinstance(microseconds, (int, float))
                and not isinstance(microseconds, bool)
                and 0 <= microseconds < 1_000_000
            ):
                try:
                    return datetime.fromtimestamp(
                        float(seconds) + (float(microseconds) / 1_000_000),
                        timezone.utc,
                    )
                except (ValueError, OSError, OverflowError):
                    pass

    value = next(
        (
            candidate
            for candidate in (
                timestamp if not isinstance(timestamp, dict) else None,
                result.get("timestamp"),
                result.get("capture_time"),
                document.get("timestamp"),
            )
            if candidate not in (None, "")
        ),
        None,
    )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    return None


def _confidence(result: dict[str, Any]) -> float | None:
    value = next(
        (
            candidate
            for candidate in (
                result.get("confidence"),
                result.get("plate_confidence"),
                result.get("confidence_level"),
            )
            if candidate is not None
        ),
        None,
    )
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 100 else None


def _safe_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    clean = str(value).strip()
    return clean[:maximum] if clean else None


def _allowlisted_metadata(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {}
    for source, target, maximum in (
        ("plate_color", "plate_color", 40),
        ("vehicle_color", "vehicle_color", 40),
        ("vehicle_type", "vehicle_type", 40),
        ("country", "country", 40),
        ("trigger_type", "trigger_type", 40),
    ):
        value = _safe_text(result.get(source), maximum)
        if value is not None:
            allowed[target] = value
    motion_direction = _motion_direction(result.get("direction"))
    if motion_direction is not None:
        allowed["motion_direction"] = motion_direction
    return allowed


def _motion_direction(value: Any) -> str | None:
    """Normalize the vendor's physical motion direction, not lane entry/exit."""
    if isinstance(value, str):
        clean = value.strip().upper()
        if clean in {"UNKNOWN", "LEFT", "RIGHT", "UP", "DOWN"}:
            return clean
        try:
            value = int(clean)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if int(value) != value:
        return None
    return {
        0: "UNKNOWN",
        1: "LEFT",
        2: "RIGHT",
        3: "UP",
        4: "DOWN",
    }.get(int(value))
