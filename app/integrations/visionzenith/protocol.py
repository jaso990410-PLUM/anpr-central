import base64
from datetime import datetime, timezone
import re
from typing import Any


CONTROLLER_ADDRESS = 1
SERIAL_INTERVAL_MS = 50
DEFAULT_PLATE_PAUSE_MS = 100


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _aa55_frame(body: bytes) -> bytes:
    checksum = crc16(body + b"\x00\x00")
    return b"\xaa\x55" + body + checksum.to_bytes(2, "big") + b"\xaf"


def _controller_address(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("Controller slave ID must be 0 to 255")
    return value


def led_frame(plate: str, *, address: int = CONTROLLER_ADDRESS) -> bytes:
    if not re.fullmatch(r"[A-Z0-9]+", plate):
        raise ValueError("Plate must contain one or more ASCII letters/digits")
    payload = bytes.fromhex("04050100") + plate.encode("ascii")
    if len(payload) > 0xFFFF:
        raise ValueError("Plate exceeds the protocol length field")
    body = bytes([0x50, _controller_address(address), 0, 0x27])
    body += len(payload).to_bytes(2, "big") + payload
    return _aa55_frame(body)


def idle_frame(
    row: int, text: str, sequence: int, *, address: int = CONTROLLER_ADDRESS
) -> bytes:
    if row not in (1, 2, 3, 4):
        raise ValueError("Idle row must be 1 to 4")
    if not 0 <= sequence <= 255:
        raise ValueError("Sequence must be 0 to 255")
    if any(ord(character) < 32 for character in text):
        raise ValueError("Use single-line text without control characters")
    encoded = text.encode("gbk")
    if len(encoded) > 255:
        raise ValueError("Idle text exceeds 255 encoded bytes")
    payload = bytes([row, 1, 0]) + encoded
    body = bytes([sequence, _controller_address(address), 0, 0x25])
    body += len(payload).to_bytes(2, "big") + payload
    return _aa55_frame(body)


def display_settings_frame(
    brightness: int,
    scroll_speed: int,
    *,
    sequence: int = 3,
    address: int = CONTROLLER_ADDRESS,
) -> bytes:
    """Build the proven F1 brightness/scroll-speed controller frame."""
    if type(brightness) is not int or not 0 <= brightness <= 5:
        raise ValueError("LED brightness must be 0 to 5")
    if type(scroll_speed) is not int or not 0 <= scroll_speed <= 5:
        raise ValueError("LED scroll speed must be 0 to 5")
    if type(sequence) is not int or not 0 <= sequence <= 255:
        raise ValueError("Sequence must be 0 to 255")
    payload = bytes([brightness, scroll_speed])
    body = bytes([sequence, _controller_address(address), 0, 0xF1])
    body += len(payload).to_bytes(2, "big") + payload
    return _aa55_frame(body)


def traffic_light_frame(
    *, seconds: int = 5, red: bool = False, address: int = CONTROLLER_ADDRESS
) -> bytes:
    if type(seconds) is not int or not 1 <= seconds <= 255:
        raise ValueError("Timer must be 1 to 255 whole seconds")
    command, value = (0x12, 0) if red else (0x13, seconds)
    body = bytes([0x50, _controller_address(address), 0, command, 0, 1, value])
    return _aa55_frame(body)


def prepare_text(
    text: str = "",
    plate: str | None = None,
    plate_pause: int = DEFAULT_PLATE_PAUSE_MS,
) -> str:
    if type(plate_pause) is not int or plate_pause < 0:
        raise ValueError("Plate pause must be a non-negative integer")
    if plate is None:
        if "{plate}" in text:
            raise ValueError("Text contains {plate}; supply a plate")
        return text
    plate = plate.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+", plate):
        raise ValueError("Plate must contain ASCII letters/digits only")
    if not text:
        text = "{plate}"
    elif "{plate}" not in text:
        raise ValueError("Text must contain {plate}")
    separator = f"[p{plate_pause}]" if plate_pause else ""
    return text.replace("{plate}", separator.join(plate))


def voice_frame(
    text: str = "",
    speed: int | None = None,
    volume: int | None = None,
    voice: int | None = None,
    pitch: int | None = None,
) -> bytes:
    tags = ""
    if voice is not None:
        if type(voice) is not int or voice not in (3, 51, 52, 53, 54, 55):
            raise ValueError("Voice must be one of 3, 51, 52, 53, 54, 55")
        tags += f"[m{voice}]"
    for name, value, tag in (
        ("speed", speed, "s"),
        ("volume", volume, "v"),
        ("pitch", pitch, "t"),
    ):
        if value is not None:
            if type(value) is not int or not 0 <= value <= 10:
                raise ValueError(f"{name} must be an integer from 0 to 10")
            tags += f"[{tag}{value}]"
    if not isinstance(text, str):
        raise ValueError("Text must be a string")
    if not tags and not text.strip():
        raise ValueError("Provide text or a voice setting")
    payload = (tags + text).encode("gbk")
    if len(payload) > 250:
        raise ValueError("Text plus tags exceeds 250 GBK bytes")
    return b"\xfd" + (len(payload) + 2).to_bytes(2, "big") + b"\x01\x01" + payload


def serial_envelope(
    *,
    request_id: str,
    serial_number: str,
    bv: int,
    serial_channel: int,
    raw_frame: bytes,
    timestamp: int | None = None,
) -> dict[str, Any]:
    if serial_channel not in (0, 1):
        raise ValueError("Serial channel must be 0 or 1")
    return {
        "id": request_id,
        "bv": bv,
        "sn": serial_number,
        "name": "serial_data",
        "version": "1.0",
        "timestamp": (
            timestamp
            if timestamp is not None
            else int(datetime.now(timezone.utc).timestamp())
        ),
        "payload": {
            "type": "serial_data",
            "body": {
                "interval": SERIAL_INTERVAL_MS,
                "serialData": [
                    {
                        "serialChannel": serial_channel,
                        "data": base64.b64encode(raw_frame).decode("ascii"),
                        "dataLen": len(raw_frame),
                    }
                ],
            },
        },
    }


def access_list_envelope(
    *,
    request_id: str,
    serial_number: str,
    bv: int,
    body: dict[str, Any],
    timestamp: int | None = None,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "bv": bv,
        "sn": serial_number,
        "name": "white_list_operator",
        "version": "1.0",
        "timestamp": (
            timestamp
            if timestamp is not None
            else int(datetime.now(timezone.utc).timestamp())
        ),
        "payload": {"type": "white_list_operator", "body": body},
    }


def device_set_envelope(
    *,
    request_id: str,
    serial_number: str,
    bv: int,
    setting_type: str,
    body: dict[str, Any],
    timestamp: int | None = None,
) -> dict[str, Any]:
    if setting_type not in {"evs_get_rs485", "evs_set_rs485"}:
        raise ValueError("Unsupported device setting type")
    return {
        "id": request_id,
        "bv": bv,
        "sn": serial_number,
        "name": "device_set",
        "version": "1.0",
        "timestamp": timestamp if timestamp is not None else int(datetime.now(timezone.utc).timestamp()),
        "payload": {"type": setting_type, "module": "", "body": body},
    }


def offline_recovery_envelope(*, request_id: str, serial_number: str, bv: int, max_count: int, min_id: int = 0, timestamp: int | None = None) -> dict[str, Any]:
    if max_count < 1 or min_id < 0:
        raise ValueError("Invalid offline recovery request")
    return {
        "id": request_id,
        "bv": bv,
        "sn": serial_number,
        "name": "check_offline_record",
        "version": "1.0",
        "timestamp": timestamp if timestamp is not None else int(datetime.now(timezone.utc).timestamp()),
        "payload": {"type": "check_offline_record", "body": {"enable": "push", "max_count": max_count, "min_id": min_id}},
    }
