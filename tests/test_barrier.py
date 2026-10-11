import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.integrations.visionzenith.command_manager import VisionZenithCommandManager
from app.integrations.visionzenith.protocol import gate_direct_open_envelope, get_io_status_envelope
from app.integrations.visionzenith.topics import BARRIER_GATE_STATUS, BARRIER_OPEN_REPLY, GPIO_INPUT, GPIO_STATUS_REPLY, barrier_open_topic, parse_native_topic
from app.models.barrier import BarrierAction, BarrierConfiguration, BarrierState
from app.models.camera import Camera
from app.models.camera_command import CameraCommand
from app.models.parking import AccessDecision
from app.models.recognition_event import RecognitionEvent
from app.models.site import Site
from app.services.barrier_action_service import BarrierActionCoordinator
from app.services.barrier_configuration_service import BarrierConfigurationError, BarrierConfigurationService
from app.services.barrier_status_service import BarrierFeedbackRejected, BarrierStatusService, normalize_gpio_feedback


def seed_lane(db: Session, *, source="LIVE", decision="ALLOW", intent="OPEN"):
    now = datetime.now(timezone.utc)
    site = Site(code="MAIN", name="Main Site", timezone="Asia/Kuala_Lumpur", is_active=True)
    camera = Camera(site=site, code="MAIN-ENTRY", name="Main Entry", direction="ENTRY", lane_role="ENTRY", serial_number="275688a3-5c6d744b", is_enabled=True, provisioning_state="PROVISIONED", connection_state="ONLINE")
    event = RecognitionEvent(camera=camera, site=site, source=source, plate_text="ABC1234", direction="ENTRY", confidence=99, captured_at=now, received_at=now, event_type="FULL")
    db.add_all((site, camera, event)); db.flush()
    access_decision = AccessDecision(recognition_event_id=event.id, site_id=site.id, camera_id=camera.id, access_status="ALLOWED", decision=decision, reason_code="ACTIVE_ALLOW", policy_type="RESIDENTIAL", policy_version=1, evaluated_at=now, barrier_intent=intent, physical_execution="NOT_IMPLEMENTED", response_status="PENDING")
    db.add(access_decision); db.commit()
    return site, camera, event, access_decision


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_gate_contract_and_topics_are_exact():
    request = gate_direct_open_envelope(request_id="one", serial_number="camera-sn", bv=17932, timestamp=123)
    assert request == {"id": "one", "bv": 17932, "sn": "camera-sn", "name": "gate_direct_open", "version": "1.0", "timestamp": 123, "payload": None}
    assert barrier_open_topic("camera-sn") == "device/camera-sn/message/down/gate_direct_open"
    assert parse_native_topic("device/camera-sn/message/down/gate_direct_open/reply").message_type == BARRIER_OPEN_REPLY
    assert parse_native_topic("device/camera-sn/message/up/barr_gate_status").message_type == BARRIER_GATE_STATUS
    assert parse_native_topic("device/camera-sn/message/up/gpio_in").message_type == GPIO_INPUT
    assert parse_native_topic("device/camera-sn/message/down/get_io_status/reply").message_type == GPIO_STATUS_REPLY
    assert get_io_status_envelope(request_id="io", serial_number="camera-sn", bv=17932, gpio=2, timestamp=1)["payload"]["body"] == {"type": 0, "gpio": 2}


@pytest.mark.parametrize("open_raw,closed_raw,expected", [(1,0,"OPEN"),(0,1,"CLOSED"),(0,0,"INTERMEDIATE"),(1,1,"INVALID_FEEDBACK")])
def test_two_input_gpio_normalization(open_raw, closed_raw, expected):
    assert normalize_gpio_feedback(open_raw=open_raw, open_level="HIGH", closed_raw=closed_raw, closed_level="HIGH", has_open=True, has_closed=True) == expected


def test_gpio_polarity_and_single_input_are_conservative():
    assert normalize_gpio_feedback(open_raw=0, open_level="LOW", closed_raw=None, closed_level="HIGH", has_open=True, has_closed=False) == "OPEN"
    assert normalize_gpio_feedback(open_raw=1, open_level="LOW", closed_raw=None, closed_level="HIGH", has_open=True, has_closed=False) == "UNKNOWN"
    assert normalize_gpio_feedback(open_raw=None, open_level="HIGH", closed_raw=0, closed_level="LOW", has_open=False, has_closed=True) == "CLOSED"


def test_configuration_requires_explicit_safe_commissioning(db: Session):
    _site, camera, _event, _decision = seed_lane(db)
    service = BarrierConfigurationService(db)
    config, created = service.save(camera, enabled=True, control_mode="CENTRAL_MANAGED", feedback_mode="GPIO_INPUTS", confirmation_timeout="10", open_input="1", open_level="HIGH", closed_input="2", closed_level="LOW", notes="dry contacts verified")
    assert created and config.commissioned_at is None
    with pytest.raises(BarrierConfigurationError):
        service.commission(config, camera.id, local_auto_open_verified=False)
    service.commission(config, camera.id, local_auto_open_verified=True)
    assert config.commissioned_at and config.local_auto_open_verified
    service.save(camera, enabled=True, control_mode="CAMERA_LOCAL", feedback_mode="NONE", confirmation_timeout=10, open_input="", open_level="HIGH", closed_input="", closed_level="HIGH", notes="")
    assert config.commissioned_at is None


def test_native_feedback_normalizes_and_offline_preserves_last_known(db: Session):
    _site, camera, _event, _decision = seed_lane(db)
    db.add(BarrierConfiguration(camera_id=camera.id, enabled=True, control_mode="CAMERA_LOCAL", feedback_mode="NATIVE_GATE_STATUS")); db.commit()
    service = BarrierStatusService(db)
    for vendor_state, expected in ((0,"CLOSED"),(1,"OPEN"),(2,"INTERMEDIATE")):
        state = service.handle_native_status(camera.serial_number, json.dumps({"sn": camera.serial_number, "payload": {"body": {"gate_status": vendor_state, "connect_status": 1, "enable": 1, "gate_ctrl_id": "gate-1"}}}).encode())
        assert state.normalized_state == expected
    db.commit(); camera.connection_state = "OFFLINE"; service.mark_offline_states_unknown(); db.commit()
    assert state.normalized_state == "UNKNOWN" and state.last_known_state == "INTERMEDIATE"
    with pytest.raises(BarrierFeedbackRejected):
        service.handle_native_status(camera.serial_number, b'{"payload":{"body":{"gate_status":9}}}')


def test_live_allow_creates_and_dispatches_exactly_one_action(db: Session):
    _site, camera, _event, decision = seed_lane(db)
    now = datetime.now(timezone.utc)
    db.add(BarrierConfiguration(camera_id=camera.id, enabled=True, control_mode="CENTRAL_MANAGED", feedback_mode="NONE", commissioned_at=now, local_auto_open_verified=True, local_auto_open_verified_at=now)); db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    calls = []
    class Commands:
        def enqueue_gate_open(self, **kwargs):
            calls.append(kwargs)
            with factory() as session:
                action = session.get(BarrierAction, kwargs["barrier_action_id"])
                action.request_id = "BARRIER_OPEN-test"
                action.command_status = "PUBLISHED"
                action.published_at = datetime.now(timezone.utc)
                session.commit()
    manager = Commands()
    coordinator = BarrierActionCoordinator(session_factory=factory, command_manager=manager, is_connected=lambda: True)
    action_id = coordinator.create_automatic(decision.id)
    assert coordinator.create_automatic(decision.id) == action_id
    assert calls == [{"barrier_action_id": action_id, "camera_id": camera.id, "serial_number": camera.serial_number}]
    db.expire_all(); assert db.get(BarrierAction, action_id).command_status == "PUBLISHED"


@pytest.mark.parametrize("code,expected", [(200, "CAMERA_ACKNOWLEDGED"), (500, "CAMERA_REJECTED")])
def test_gate_reply_correlates_to_action_without_claiming_gate_open(db: Session, code, expected):
    site, camera, event, _decision = seed_lane(db)
    now = datetime.now(timezone.utc)
    action = BarrierAction(camera_id=camera.id, site_id=site.id, recognition_event_id=event.id, trigger_source="COMMISSIONING_TEST", command_status="PUBLISHED", request_id="BARRIER_OPEN-test", requested_at=now, expires_at=now + timedelta(seconds=8), published_at=now, physical_confirmation_status="NOT_AVAILABLE")
    command = CameraCommand(camera_id=camera.id, request_id="BARRIER_OPEN-test", command_type="BARRIER_OPEN", status="PUBLISHED", published_at=now, request_metadata={"barrier_action_id": "pending"})
    db.add_all((BarrierConfiguration(camera_id=camera.id, enabled=True, control_mode="CENTRAL_MANAGED", feedback_mode="NONE"), action, command)); db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = VisionZenithCommandManager(session_factory=factory, publisher=lambda *_: True)
    try:
        assert manager.handle_reply(camera.serial_number, json.dumps({"id":"BARRIER_OPEN-test", "code":code, "payload":None}).encode())
        db.expire_all(); stored = db.get(BarrierAction, action.id)
        assert stored.command_status == expected
        assert stored.physical_confirmation_status == "NOT_AVAILABLE"
    finally:
        manager.stop()


@pytest.mark.parametrize("source,decision,intent", [("OFFLINE_RECOVERY","ALLOW","OPEN"),("LIVE","DENY","HOLD"),("LIVE","REVIEW","OPERATOR_REVIEW"),("LIVE","NO_ACTION","NONE")])
def test_ineligible_decisions_never_create_automatic_action(db: Session, source, decision, intent):
    _site, camera, _event, access_decision = seed_lane(db, source=source, decision=decision, intent=intent)
    now = datetime.now(timezone.utc)
    db.add(BarrierConfiguration(camera_id=camera.id, enabled=True, control_mode="CENTRAL_MANAGED", feedback_mode="NONE", commissioned_at=now, local_auto_open_verified=True)); db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    manager = VisionZenithCommandManager(session_factory=factory, publisher=lambda *_: True)
    try:
        assert BarrierActionCoordinator(session_factory=factory, command_manager=manager, is_connected=lambda: True).create_automatic(access_decision.id) is None
        assert db.scalar(select(BarrierAction)) is None
    finally:
        manager.stop()


def test_stale_action_expires_without_publish_and_restart_never_replays(db: Session):
    site, camera, event, decision = seed_lane(db)
    old = datetime.now(timezone.utc) - timedelta(minutes=1)
    decision.evaluated_at = old
    db.add(BarrierConfiguration(camera_id=camera.id, enabled=True, control_mode="CENTRAL_MANAGED", feedback_mode="NONE", commissioned_at=old, local_auto_open_verified=True)); db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False); published=[]
    manager = VisionZenithCommandManager(session_factory=factory, publisher=lambda *args: published.append(args) or True)
    coordinator = BarrierActionCoordinator(session_factory=factory, command_manager=manager, is_connected=lambda: True)
    try:
        action_id = coordinator.create_automatic(decision.id); db.expire_all()
        assert db.get(BarrierAction, action_id).command_status == "EXPIRED_NOT_SENT" and not published
        queued = BarrierAction(camera_id=camera.id, site_id=site.id, recognition_event_id=event.id, trigger_source="MANUAL_RELEASE", command_status="QUEUED", requested_at=old, expires_at=old, physical_confirmation_status="NOT_AVAILABLE")
        uncertain = BarrierAction(camera_id=camera.id, site_id=site.id, recognition_event_id=event.id, trigger_source="MANUAL_RELEASE", command_status="PUBLISHED", requested_at=old, expires_at=old, physical_confirmation_status="NOT_AVAILABLE")
        db.add_all((queued, uncertain)); db.commit()
        coordinator._reconcile_restart(); db.expire_all()
        assert db.get(BarrierAction, queued.id).command_status == "EXPIRED_NOT_SENT"
        assert db.get(BarrierAction, uncertain.id).command_status == "TIMEOUT_UNCERTAIN"
        assert not published
    finally:
        manager.stop()


def test_already_open_suppresses_only_automatic_action(db: Session):
    _site, camera, _event, decision = seed_lane(db)
    now = datetime.now(timezone.utc)
    db.add_all((BarrierConfiguration(camera_id=camera.id, enabled=True, control_mode="CENTRAL_MANAGED", feedback_mode="GPIO_INPUTS", commissioned_at=now, local_auto_open_verified=True, open_status_input=1), BarrierState(camera_id=camera.id, normalized_state="OPEN", feedback_source="GPIO_INPUTS", observed_at=now, last_known_state="OPEN", last_known_at=now))); db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False); published=[]
    manager = VisionZenithCommandManager(session_factory=factory, publisher=lambda *args: published.append(args) or True)
    try:
        coordinator = BarrierActionCoordinator(session_factory=factory, command_manager=manager, is_connected=lambda: True)
        # This state represents a fresh observation received after startup reconciliation.
        coordinator._restart_reconciled = True
        action_id = coordinator.create_automatic(decision.id)
        db.expire_all(); action = db.get(BarrierAction, action_id)
        assert action.command_status == "SUPPRESSED_ALREADY_OPEN" and action.physical_confirmation_status == "CONFIRMED_OPEN" and not published
    finally:
        manager.stop()
