import base64
from datetime import datetime, timezone
import json
import uuid

import pytest
from sqlalchemy.orm import Session

from app.integrations.visionzenith.access_list import (
    READBACK_MISMATCH,
    READBACK_UNVERIFIED,
    READBACK_VERIFIED,
    merge_record,
    readback_status,
    select_record,
)
from app.integrations.visionzenith.adapter import (
    NativeMessageRejected,
    VisionZenithAdapter,
    parse_native_heartbeat,
    process_native_heartbeat,
)
from app.integrations.visionzenith.protocol import (
    crc16,
    device_set_envelope,
    idle_frame,
    led_frame,
    prepare_text,
    serial_envelope,
    traffic_light_frame,
    voice_frame,
)
from app.integrations.visionzenith.rs485 import desired_matches, get_rs485_body, parse_rs485_reply, set_rs485_body
from app.integrations.visionzenith.recognition import (
    RetainedRecognitionIgnored,
    image_bytes,
    image_candidates,
    metadata_only,
    parse_recognition,
)
from app.integrations.visionzenith.topics import (
    DEVICE_SET_REPLY,
    KEEP_ALIVE,
    SERIAL_REPLY,
    parse_native_topic,
)
from app.models.camera import Camera
from app.models.site import Site
from app.services.camera_service import CREDENTIALS_REVOKED, ONLINE, PROVISIONED


SERIAL = "275688a3-5c6d744b"


def create_camera(
    db: Session, *, serial: str = SERIAL, enabled: bool = True
) -> Camera:
    site = Site(
        code="MAIN",
        name="Main Site",
        timezone="Asia/Kuala_Lumpur",
        is_active=True,
    )
    camera = Camera(
        site=site,
        code="MAIN-ENTRY-01",
        name="Main Entry",
        direction="ENTRY",
        serial_number=serial,
        is_enabled=enabled,
    )
    db.add(camera)
    db.commit()
    return camera


def recognition_payload(
    plate: str = "PFQ5217", *, event_id: str = "event-1"
) -> bytes:
    return json.dumps(
        {
            "id": "envelope-1",
            "payload": {
                "AlarmInfoPlate": {
                    "result": {
                        "PlateResult": {
                            "license": base64.b64encode(
                                plate.encode("gbk")
                            ).decode("ascii"),
                            "unique_id": event_id,
                        }
                    }
                }
            },
        }
    ).encode()


def test_native_topic_parsing() -> None:
    heartbeat = parse_native_topic(
        f"device/{SERIAL}/message/up/keep_alive"
    )
    reply = parse_native_topic(
        f"device/{SERIAL}/message/down/serial_data/reply"
    )

    assert heartbeat is not None
    assert heartbeat.serial_number == SERIAL
    assert heartbeat.message_type == KEEP_ALIVE
    assert reply is not None and reply.message_type == SERIAL_REPLY
    assert parse_native_topic("device/serial/message/up/unknown") is None
    assert parse_native_topic("device/serial/message/#") is None


def test_documented_rs485_topics_envelopes_and_mapping() -> None:
    reply = parse_native_topic(f"device/{SERIAL}/message/down/device_set/reply")
    assert reply is not None and reply.message_type == DEVICE_SET_REPLY
    body = set_rs485_body(source=1, baud_rate=19200, data_bits=8, parity="EVEN", stop_bits=2)
    assert body == {"source": 1, "baud_rate": 19200, "data_bits": 8, "parity_id": 2, "stop_bits": 2}
    envelope = device_set_envelope(request_id="rs485-1", serial_number=SERIAL, bv=17932, setting_type="evs_set_rs485", body=body, timestamp=123)
    assert envelope["payload"]["type"] == "evs_set_rs485"
    assert envelope["payload"]["body"] == body
    assert "slave_id" not in envelope["payload"]["body"]
    observed = parse_rs485_reply({"payload": {"type": "evs_get_rs485", "body": body}})
    assert desired_matches(observed, source=1, baud_rate=19200, data_bits=8, parity="EVEN", stop_bits=2)
    assert get_rs485_body(0) == {"source": 0}


def test_native_heartbeat_maps_serial_and_marks_camera_online(db: Session) -> None:
    camera = create_camera(db)
    received_at = datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc)

    processed = process_native_heartbeat(
        db,
        f"device/{SERIAL}/message/up/keep_alive",
        json.dumps({"sn": SERIAL, "anything": "vendor-owned"}).encode(),
        now=received_at,
    )

    assert processed.id == camera.id
    assert camera.connection_state == ONLINE
    assert camera.provisioning_state == PROVISIONED
    assert camera.last_seen_at == received_at
    assert camera.last_connected_at == received_at


def test_native_heartbeat_payload_is_not_overconstrained() -> None:
    heartbeat = parse_native_heartbeat(
        f"device/{SERIAL}/message/up/keep_alive", b"vendor-payload-not-json"
    )
    assert heartbeat.serial_number == SERIAL


def test_native_heartbeat_rejects_topic_payload_serial_mismatch() -> None:
    with pytest.raises(NativeMessageRejected, match="do not match"):
        parse_native_heartbeat(
            f"device/{SERIAL}/message/up/keep_alive",
            b'{"sn":"different"}',
        )


@pytest.mark.parametrize("condition", ["unknown", "disabled", "revoked"])
def test_native_heartbeat_rejects_unusable_camera(
    db: Session, condition: str
) -> None:
    serial = "unknown"
    if condition != "unknown":
        camera = create_camera(db, enabled=condition != "disabled")
        serial = SERIAL
        if condition == "revoked":
            camera.provisioning_state = CREDENTIALS_REVOKED
            db.commit()

    with pytest.raises(NativeMessageRejected):
        process_native_heartbeat(
            db,
            f"device/{serial}/message/up/keep_alive",
            b"{}",
        )


def test_crc_and_led_known_vectors_and_variable_plate_length() -> None:
    assert (
        crc16(bytes.fromhex("01640011000B01B5DAD2BBD0D0B9E3B8E60000"))
        == 0xE560
    )
    assert (
        led_frame("BLV7833").hex().upper()
        == "AA5550010027000B04050100424C56373833338BB3AF"
    )
    assert led_frame("QAB4838E")[12:-3] == b"QAB4838E"


@pytest.mark.parametrize(
    ("row", "text", "sequence", "expected"),
    [
        (1, "Welcome", 5, "AA5505010025000A01010057656C636F6D659C55AF"),
        (2, "Amseco", 6, "AA55060100250009020100416D7365636FAD20AF"),
        (3, "ANP", 7, "AA55070100250006030100414E500845AF"),
        (4, "Test", 8, "AA55080100250007040100546573745F9CAF"),
    ],
)
def test_idle_frame_vectors(
    row: int, text: str, sequence: int, expected: str
) -> None:
    assert idle_frame(row, text, sequence).hex().upper() == expected


def test_traffic_light_and_voice_vectors() -> None:
    green = traffic_light_frame(seconds=5)
    red = traffic_light_frame(red=True)
    assert green[:9] == bytes([0xAA, 0x55, 0x50, 1, 0, 0x13, 0, 1, 5])
    assert red[5:9] == bytes([0x12, 0, 1, 0])
    assert voice_frame(volume=8).hex().upper() == "FD000601015B76385D"
    assert voice_frame(speed=3).hex().upper() == "FD000601015B73335D"
    assert prepare_text(plate="QAB4838E", plate_pause=0) == "QAB4838E"


def test_serial_envelope_base64_and_raw_length() -> None:
    raw = led_frame("BLV7833")
    envelope = serial_envelope(
        request_id="request-1",
        serial_number=SERIAL,
        bv=17932,
        serial_channel=0,
        raw_frame=raw,
        timestamp=123,
    )
    serial_data = envelope["payload"]["body"]["serialData"][0]
    assert base64.b64decode(serial_data["data"]) == raw
    assert serial_data["dataLen"] == len(raw)
    assert serial_data["dataLen"] != len(serial_data["data"])
    assert envelope["timestamp"] == 123


def test_access_list_merge_and_readback_distinctions() -> None:
    original = {
        "plate": "PFQ5217",
        "enable": 1,
        "need_alarm": 0,
        "vehicle_comment": "Staff",
    }
    merged = merge_record(
        "PFQ5217", original, {"need_alarm": 1}, now="2026-10-04 00:00:00"
    )
    assert merged["enable"] == 1
    assert merged["need_alarm"] == 1
    assert select_record(
        {"payload": {"body": {"state_code": 200, "dldb_rec": None}}},
        "PFQ5217",
    ) is None
    assert readback_status({"enable": 1}, {"enable": 1}) == READBACK_VERIFIED
    assert (
        readback_status({"enable": 0}, {"enable": 1})
        == READBACK_MISMATCH
    )
    assert (
        readback_status({"enable": 1}, {"customer_id": 1001})
        == READBACK_UNVERIFIED
    )


def test_recognition_gbk_variable_plate_and_snapshot_foundation() -> None:
    parsed = parse_recognition(recognition_payload("QAB4838E"))
    assert parsed.plate == "QAB4838E"
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aI1sAAAAASUVORK5CYII="
    )
    encoded = base64.b64encode(png).decode()
    parsed.plate_result["full_image_content"] = encoded
    assert image_candidates(parsed.plate_result) == (("scene", encoded),)
    assert image_bytes(encoded) == (png, ".png")
    assert encoded not in json.dumps(metadata_only(parsed.document))


def test_retained_recognition_is_ignored() -> None:
    with pytest.raises(RetainedRecognitionIgnored):
        parse_recognition(recognition_payload(), retained=True)


class RecordingCommandManager:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []

    def enqueue_serial(self, **kwargs):
        self.commands.append(kwargs)


def test_adapter_does_not_act_on_retained_recognition(db: Session) -> None:
    create_camera(db)
    manager = RecordingCommandManager()
    adapter = VisionZenithAdapter(auto_led_enabled=True)

    with pytest.raises(NativeMessageRejected, match="Retained"):
        adapter.dispatch(
            db,
            f"device/{SERIAL}/message/up/quick_ivs_result",
            recognition_payload(),
            retained=True,
            command_manager=manager,  # type: ignore[arg-type]
        )

    assert manager.commands == []


def test_adapter_routes_fresh_recognition_to_led_once(db: Session) -> None:
    camera = create_camera(db)
    manager = RecordingCommandManager()
    adapter = VisionZenithAdapter(auto_led_enabled=True)
    topic = f"device/{SERIAL}/message/up/quick_ivs_result"
    payload = recognition_payload("QAB4838E")

    adapter.dispatch(
        db,
        topic,
        payload,
        retained=False,
        command_manager=manager,  # type: ignore[arg-type]
    )
    adapter.dispatch(
        db,
        topic,
        payload,
        retained=False,
        command_manager=manager,  # type: ignore[arg-type]
    )

    assert len(manager.commands) == 1
    assert manager.commands[0]["camera_id"] == camera.id
    assert manager.commands[0]["raw_frame"] == led_frame("QAB4838E")
