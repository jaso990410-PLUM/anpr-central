from __future__ import annotations

import shutil
import socket
import time
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.access_list_camera_sync import AccessListCameraSync
from app.models.barrier import BarrierAction, BarrierConfiguration, BarrierState
from app.models.camera import Camera
from app.models.control_board import ControlBoard
from app.models.media import MediaSource, MediaStorage
from app.models.monitoring import ComponentHealth
from app.models.mqtt_broker_config import MqttBrokerConfig
from app.models.offline_recovery_run import OfflineRecoveryRun
from app.services.alert_service import AlertService
from app.services.operational_event_service import EventProjectionService, EventService


@dataclass(frozen=True)
class TransitionPolicy:
    fault_event: str
    recovery_event: str
    alert_type: str
    title: str
    severity: str = "WARNING"


COMPONENT_POLICIES: dict[str, TransitionPolicy] = {
    "DATABASE": TransitionPolicy("DATABASE_UNAVAILABLE", "DATABASE_RECOVERED", "DATABASE_UNAVAILABLE", "PostgreSQL unavailable", "CRITICAL"),
    "MQTT_BROKER": TransitionPolicy("MQTT_DISCONNECTED", "MQTT_RECONNECTED", "MQTT_DISCONNECTED", "MQTT broker disconnected", "CRITICAL"),
    "CAMERA": TransitionPolicy("CAMERA_OFFLINE", "CAMERA_ONLINE", "CAMERA_OFFLINE", "Camera offline"),
    "RECORDER": TransitionPolicy("RECORDER_STOPPED", "RECORDER_STARTED", "RECORDER_STOPPED", "Recorder stopped", "CRITICAL"),
    "RECORDING_SOURCE": TransitionPolicy("RECORDING_STALE", "RECORDING_RESUMED", "RECORDING_STALE", "Recording is stale"),
    "STORAGE": TransitionPolicy("STORAGE_UNAVAILABLE", "STORAGE_RECOVERED", "STORAGE_UNAVAILABLE", "Recording storage unavailable", "CRITICAL"),
    "ACCESS_SYNC": TransitionPolicy("ACCESS_SYNC_MISMATCH", "ACCESS_SYNC_RESTORED", "ACCESS_SYNC_MISMATCH", "Access-list synchronization mismatch"),
    "OFFLINE_RECOVERY": TransitionPolicy("OFFLINE_BACKLOG", "OFFLINE_BACKLOG_CLEARED", "OFFLINE_BACKLOG", "Offline recognition backlog"),
    "BARRIER_CONTROL": TransitionPolicy("BARRIER_CONTROL_DEGRADED", "BARRIER_CONTROL_RESTORED", "BARRIER_CONTROL_DEGRADED", "Barrier control degraded"),
    "PERIPHERAL": TransitionPolicy("PERIPHERAL_COMMAND_FAILED", "COMPONENT_RECOVERED", "PERIPHERAL_COMMAND_FAILED", "Peripheral command path failed"),
}

DEGRADED_POLICIES: dict[str, TransitionPolicy] = {
    "DATABASE": TransitionPolicy("DATABASE_SLOW", "DATABASE_RECOVERED", "DATABASE_SLOW", "PostgreSQL response is slow"),
    "STORAGE": TransitionPolicy("STORAGE_LOW", "STORAGE_RECOVERED", "STORAGE_LOW", "Recording storage is low"),
}


def _transition_policy(component_type: str, state: str) -> TransitionPolicy | None:
    if state == "DEGRADED" and component_type in DEGRADED_POLICIES:
        return DEGRADED_POLICIES[component_type]
    return COMPONENT_POLICIES.get(component_type)


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class ComponentHealthService:
    def __init__(self, db: Session):
        self.db = db
        self.events = EventService(db)
        self.alerts = AlertService(db)

    def observe(
        self, *, component_type: str, component_id: str, display_name: str,
        state: str, summary: str, observed_at: datetime | None = None,
        site_id: UUID | None = None, capabilities: dict | None = None,
    ) -> ComponentHealth:
        now = _utc(observed_at)
        item = self.db.scalar(select(ComponentHealth).where(
            ComponentHealth.component_type == component_type,
            ComponentHealth.component_id == component_id,
        ))
        previous = item.health_state if item else None
        previous_changed_at = item.state_changed_at if item else None
        if item is None:
            item = ComponentHealth(component_type=component_type, component_id=component_id,
                site_id=site_id, display_name=display_name[:180], health_state=state,
                observed_at=now, state_changed_at=now, summary=summary[:500], capabilities=capabilities)
            self.db.add(item)
        else:
            item.site_id = site_id
            item.display_name = display_name[:180]
            item.observed_at = now
            item.summary = summary[:500]
            item.capabilities = capabilities
            if previous != state:
                item.health_state = state
                item.state_changed_at = now
        if state == "HEALTHY":
            item.last_success_at = now
        self.db.flush()

        policy = _transition_policy(component_type, state)
        is_fault = state in {"FAULT", "DEGRADED"}
        was_fault = previous in {"FAULT", "DEGRADED"}
        if policy and is_fault:
            previous_policy = _transition_policy(component_type, previous or "")
            changed_condition = bool(was_fault and previous_policy and previous_policy.alert_type != policy.alert_type)
            if changed_condition and previous_policy:
                self.alerts.resolve_condition(alert_type=previous_policy.alert_type, site_id=site_id,
                    component_type=component_type, component_id=component_id, resolved_at=now,
                    reason=f"Condition changed to {policy.alert_type}")
            transition_event = None
            if not was_fault or changed_condition:
                transition_event = self.events.record(event_type=policy.fault_event, occurred_at=now,
                    source_type="HealthTransition", source_id=f"{component_type}:{component_id}:{now.isoformat()}:FAULT",
                    display_subject=display_name, summary=summary, site_id=site_id)
            self.alerts.observe_condition(alert_type=policy.alert_type, severity=policy.severity,
                site_id=site_id, component_type=component_type, component_id=component_id,
                title=policy.title, summary=summary, observed_at=now,
                trigger_event_id=transition_event.id if transition_event else None)
        elif was_fault and not is_fault:
            previous_policy = _transition_policy(component_type, previous or "")
            if previous_policy is None:
                return item
            recovery = self.events.record(event_type=previous_policy.recovery_event, occurred_at=now,
                source_type="HealthTransition", source_id=f"{component_type}:{component_id}:{now.isoformat()}:RECOVERY",
                display_subject=display_name, summary=summary, site_id=site_id)
            if component_type == "RECORDING_SOURCE" and previous_changed_at is not None:
                self.events.record(event_type="RECORDING_GAP", occurred_at=now,
                    source_type="HealthTransition", source_id=f"{component_type}:{component_id}:{now.isoformat()}:GAP",
                    display_subject=display_name,
                    summary=f"Recording gap from {_utc(previous_changed_at).isoformat()} to {now.isoformat()}", site_id=site_id)
            self.alerts.resolve_condition(alert_type=previous_policy.alert_type, site_id=site_id,
                component_type=component_type, component_id=component_id, resolved_at=now,
                reason=f"Monitoring observed {state.lower()}: {summary}", resolution_event_id=recovery.id)
        return item


class MonitoringService:
    """Observational monitor. It never calls access, barrier, or peripheral command services."""

    def __init__(self, db: Session):
        self.db = db
        self.health = ComponentHealthService(db)

    def run_once(self, *, include_network_probe: bool = True, now: datetime | None = None) -> dict[str, int]:
        observed = _utc(now)
        projected = EventProjectionService(self.db).project_recent(limit_per_source=settings.monitor_projection_batch_size)
        count = 0
        count += self._core(observed, include_network_probe)
        count += self._cameras(observed)
        count += self._media(observed)
        count += self._access_sync(observed)
        count += self._offline_recovery(observed)
        count += self._lane_equipment(observed)
        count += self._host(observed)
        self.db.commit()
        return {"events_projected": projected, "components_observed": count}

    def _core(self, now: datetime, network: bool) -> int:
        self.health.observe(component_type="APPLICATION", component_id="anpr-central", display_name="ANPR Central",
            state="HEALTHY", summary="Application monitor cycle completed", observed_at=now,
            capabilities={"claim": "VERIFIED", "uptime_scope": "monitor process"})
        samples: list[float] = []
        for _ in range(3):
            started = time.perf_counter()
            self.db.execute(text("SELECT 1"))
            samples.append((time.perf_counter() - started) * 1000)
        elapsed_ms = sum(samples) / len(samples)
        slow_samples = sum(sample >= settings.monitor_database_slow_ms for sample in samples)
        db_state = "DEGRADED" if slow_samples >= 2 else "HEALTHY"
        self.health.observe(component_type="DATABASE", component_id="postgresql", display_name="PostgreSQL",
            state=db_state, summary=f"Three basic queries averaged {elapsed_ms:.1f} ms", observed_at=now,
            capabilities={"claim": "VERIFIED", "average_query_ms": round(elapsed_ms, 1), "slow_samples": slow_samples})
        config = self.db.get(MqttBrokerConfig, 1)
        enabled = config.is_enabled if config else settings.mqtt_enabled
        host = config.host if config else settings.mqtt_host
        port = config.port if config else settings.mqtt_port
        if not enabled:
            mqtt_state, mqtt_summary = "DISABLED", "Central MQTT client is intentionally disabled"
        elif not network:
            mqtt_state, mqtt_summary = "UNKNOWN", "Broker network probe was not requested"
        else:
            try:
                with socket.create_connection((host, port), timeout=settings.monitor_network_timeout_seconds):
                    pass
                mqtt_state, mqtt_summary = "HEALTHY", f"Broker TCP endpoint {host}:{port} is reachable"
            except OSError:
                mqtt_state, mqtt_summary = "FAULT", f"Broker TCP endpoint {host}:{port} is unreachable"
        self.health.observe(component_type="MQTT_BROKER", component_id="central", display_name="MQTT Broker",
            state=mqtt_state, summary=mqtt_summary, observed_at=now,
            capabilities={"claim": "VERIFIED" if network else "UNKNOWN", "host": host, "port": port})
        client_state = "DISABLED" if not enabled else "FAULT" if mqtt_state == "FAULT" else "UNKNOWN"
        client_summary = "Central MQTT client is intentionally disabled" if not enabled else (
            "Central client cannot reach the broker endpoint" if mqtt_state == "FAULT" else
            "Central MQTT client runtime is process-local and not directly verified by the independent monitor")
        self.health.observe(component_type="MQTT_CLIENT", component_id="anpr-central", display_name="Central MQTT client",
            state=client_state, summary=client_summary, observed_at=now,
            capabilities={"claim": "NOT_CONFIGURED" if not enabled else "UNKNOWN", "broker_probe": mqtt_state})
        return 4

    def _cameras(self, now: datetime) -> int:
        cameras = self.db.scalars(select(Camera).order_by(Camera.code)).all()
        for camera in cameras:
            if not camera.is_enabled:
                state, summary = "DISABLED", "Camera is administratively disabled"
            elif camera.connection_state == "ONLINE" and camera.last_seen_at and _utc(camera.last_seen_at) > now - timedelta(seconds=settings.mqtt_heartbeat_timeout_seconds):
                state, summary = "HEALTHY", f"Heartbeat last seen {_utc(camera.last_seen_at).isoformat()}"
            elif camera.provisioning_state in {"UNPROVISIONED", "CONNECTING"}:
                state, summary = "UNKNOWN", f"Provisioning state is {camera.provisioning_state}"
            elif camera.provisioning_state == "CREDENTIALS_REVOKED":
                state, summary = "DISABLED", "Camera credentials are revoked"
            else:
                state, summary = "FAULT", "Camera heartbeat is stale or connection is offline"
            self.health.observe(component_type="CAMERA", component_id=str(camera.id), display_name=camera.code,
                state=state, summary=summary, observed_at=now, site_id=camera.site_id,
                capabilities={"claim": "VERIFIED" if state in {"HEALTHY", "FAULT"} else state, "last_seen_at": camera.last_seen_at.isoformat() if camera.last_seen_at else None})
        return len(cameras)

    def _media(self, now: datetime) -> int:
        count = 0
        sources = self.db.scalars(select(MediaSource).order_by(MediaSource.camera_id)).all()
        configured_states: list[str] = []
        for source in sources:
            if not source.enabled or source.provider_type == "NONE":
                state, summary = "DISABLED", "Media source is not commissioned"
            elif source.recorder_state == "RECORDING" and source.last_successful_segment_at and _utc(source.last_successful_segment_at) > now - timedelta(seconds=settings.media_recorder_stale_seconds):
                state, summary = "HEALTHY", "Recording segments are current"
            elif source.recorder_state in {"STARTING"}:
                state, summary = "UNKNOWN", "Recorder is starting"
            elif source.recorder_state == "DEGRADED":
                state, summary = "DEGRADED", "Recorder reports degraded operation"
            elif source.recorder_state == "RECORDING":
                state = "FAULT"
                if source.last_successful_segment_at:
                    age_seconds = max(0, int((now - _utc(source.last_successful_segment_at)).total_seconds()))
                    summary = f"Recording is stale; last completed segment was {age_seconds} seconds ago while recorder reports RECORDING"
                else:
                    summary = "Recording is stale; no completed segment is available while recorder reports RECORDING"
            else:
                state, summary = "FAULT", f"Recorder state is {source.recorder_state}"
            self.health.observe(component_type="RECORDING_SOURCE", component_id=str(source.id),
                display_name=f"{source.camera.code} recording", state=state, summary=summary,
                observed_at=now, site_id=source.camera.site_id,
                capabilities={"claim": "VERIFIED", "recorder_state": source.recorder_state,
                              "last_segment_at": source.last_successful_segment_at.isoformat() if source.last_successful_segment_at else None})
            media_state = "DISABLED" if not source.enabled or source.provider_type == "NONE" else (
                "HEALTHY" if source.last_stream_activity_at and _utc(source.last_stream_activity_at) > now - timedelta(seconds=settings.media_recorder_stale_seconds) else
                "UNKNOWN" if source.recorder_state == "STARTING" else "DEGRADED")
            self.health.observe(component_type="MEDIA", component_id=str(source.id), display_name=f"{source.camera.code} media stream",
                state=media_state, summary="Recent media stream activity observed" if media_state == "HEALTHY" else
                "Media source is not commissioned" if media_state == "DISABLED" else
                "Live stream has not been verified by the monitor; recording health is tracked separately",
                observed_at=now, site_id=source.camera.site_id,
                capabilities={"claim": "DERIVED" if media_state != "DISABLED" else "NOT_CONFIGURED", "provider": source.provider_type})
            if source.enabled and source.recording_enabled:
                configured_states.append(state)
            count += 2
        recorder_state = "DISABLED" if not configured_states else "FAULT" if "FAULT" in configured_states else "DEGRADED" if "DEGRADED" in configured_states else "UNKNOWN" if "UNKNOWN" in configured_states else "HEALTHY"
        self.health.observe(component_type="RECORDER", component_id="central-recorder", display_name="Central Recorder",
            state=recorder_state, summary="No recording sources are commissioned" if recorder_state == "DISABLED" else
            "All configured recording sources are current" if recorder_state == "HEALTHY" else "One or more recording sources require attention",
            observed_at=now, capabilities={"claim": "DERIVED", "configured_sources": len(configured_states)})
        count += 1
        for storage in self.db.scalars(select(MediaStorage).order_by(MediaStorage.name)).all():
            if not storage.enabled:
                state, summary, details = "DISABLED", "Storage target is intentionally disabled", {"claim": "NOT_CONFIGURED"}
            else:
                root = Path(storage.root_path)
                try:
                    if not root.is_dir():
                        raise OSError("Configured storage directory is unavailable")
                    usage = shutil.disk_usage(root)
                    writable = os.access(root, os.W_OK)
                    free_gb = usage.free / (1024 ** 3)
                    free_percent = usage.free * 100 / usage.total if usage.total else 0
                    healthy = writable and free_gb >= storage.minimum_free_space_gb and free_percent >= storage.minimum_free_space_percent
                    state = "HEALTHY" if healthy else "DEGRADED"
                    summary = "Storage available" if healthy else "Storage below its configured free-space or writability threshold"
                    details = {"claim": "VERIFIED", "writable": writable, "total_bytes": usage.total,
                               "free_bytes": usage.free, "free_percent": round(free_percent, 1)}
                except OSError:
                    state, summary, details = "FAULT", "Configured storage directory is unavailable", {"claim": "VERIFIED", "writable": False}
            self.health.observe(component_type="STORAGE", component_id=str(storage.id), display_name=storage.name,
                state=state, summary=summary, observed_at=now, capabilities=details)
            count += 1
        return count

    def _access_sync(self, now: datetime) -> int:
        count = 0
        for camera in self.db.scalars(select(Camera).where(Camera.access_list_sync_enabled.is_(True))).all():
            statuses = self.db.execute(select(AccessListCameraSync.status, func.count()).where(
                AccessListCameraSync.camera_id == camera.id).group_by(AccessListCameraSync.status)).all()
            counts = {status: total for status, total in statuses}
            mismatch = sum(counts.get(value, 0) for value in ("MISMATCH", "ERROR", "TIMEOUT_UNCERTAIN", "OUT_OF_SYNC"))
            state = "DEGRADED" if mismatch else "HEALTHY"
            summary = f"{mismatch} mismatched/failed records" if mismatch else "No synchronization mismatch is currently reported"
            self.health.observe(component_type="ACCESS_SYNC", component_id=str(camera.id),
                display_name=f"{camera.code} access synchronization", state=state, summary=summary,
                observed_at=now, site_id=camera.site_id, capabilities={"claim": "DERIVED", "status_counts": counts})
            count += 1
        return count

    def _offline_recovery(self, now: datetime) -> int:
        count = 0
        for camera in self.db.scalars(select(Camera).where(Camera.is_enabled.is_(True))).all():
            latest = self.db.scalar(select(OfflineRecoveryRun).where(OfflineRecoveryRun.camera_id == camera.id).order_by(OfflineRecoveryRun.started_at.desc()).limit(1))
            if latest is None:
                state, summary, backlog = "UNKNOWN", "No offline-recovery observation is available", None
            else:
                backlog = latest.last_reported_count
                state = "DEGRADED" if backlog > 0 or latest.status in {"FAILED", "TIMED_OUT"} else "HEALTHY"
                summary = f"Camera reports {backlog} pending offline records; run state {latest.status}"
            self.health.observe(component_type="OFFLINE_RECOVERY", component_id=str(camera.id),
                display_name=f"{camera.code} offline recovery", state=state, summary=summary,
                observed_at=now, site_id=camera.site_id, capabilities={"claim": "DERIVED", "reported_backlog": backlog})
            count += 1
        return count

    def _lane_equipment(self, now: datetime) -> int:
        count = 0
        for config in self.db.scalars(select(BarrierConfiguration)).all():
            camera = config.camera
            if not config.enabled or config.control_mode == "DISABLED":
                state, summary = "DISABLED", "Barrier control is intentionally disabled"
            else:
                last = self.db.scalar(select(BarrierAction).where(BarrierAction.camera_id == camera.id).order_by(BarrierAction.requested_at.desc()).limit(1))
                if last and last.command_status in {"CAMERA_REJECTED", "PUBLISH_FAILED", "TIMEOUT_UNCERTAIN", "EXPIRED_NOT_SENT"}:
                    state, summary = "DEGRADED", f"Last command ended as {last.command_status}"
                else:
                    state, summary = "HEALTHY", "Barrier command path is configured; this does not verify physical position"
            self.health.observe(component_type="BARRIER_CONTROL", component_id=str(camera.id), display_name=f"{camera.code} barrier control",
                state=state, summary=summary, observed_at=now, site_id=camera.site_id,
                capabilities={"claim": "DERIVED", "feedback_mode": config.feedback_mode})
            barrier_state = self.db.scalar(select(BarrierState).where(BarrierState.camera_id == camera.id))
            feedback_state = "DISABLED" if config.feedback_mode == "NONE" else ("HEALTHY" if barrier_state and barrier_state.observed_at else "UNKNOWN")
            feedback_summary = "Physical feedback is not configured" if config.feedback_mode == "NONE" else "Physical feedback observation available" if feedback_state == "HEALTHY" else "Physical feedback has not been observed"
            self.health.observe(component_type="BARRIER_FEEDBACK", component_id=str(camera.id), display_name=f"{camera.code} barrier feedback",
                state=feedback_state, summary=feedback_summary, observed_at=now, site_id=camera.site_id,
                capabilities={"claim": "NOT_CONFIGURED" if config.feedback_mode == "NONE" else "VERIFIED" if feedback_state == "HEALTHY" else "UNKNOWN",
                              "feedback_mode": config.feedback_mode, "physical_state": barrier_state.normalized_state if barrier_state else None})
            count += 2
        for board in self.db.scalars(select(ControlBoard).where(ControlBoard.enabled.is_(True))).all():
            camera = board.camera
            failed = board.runtime_status in {"ERROR", "FAILED", "TIMEOUT_UNCERTAIN"}
            state = "FAULT" if failed else "UNVERIFIED"
            summary = f"Last command path status is {board.runtime_status}" if failed else "Camera command acknowledgement does not verify downstream RS-485 devices"
            for kind, label in (("LED", "LED display"), ("SPEAKER", "Speaker / TTS"), ("TRAFFIC_LIGHT", "Traffic light")):
                self.health.observe(component_type="PERIPHERAL", component_id=f"{board.id}:{kind}",
                    display_name=f"{camera.code} {label}", state=state, summary=summary, observed_at=now,
                    site_id=camera.site_id, capabilities={"claim": "UNVERIFIED", "camera_ack_only": True, "peripheral_type": kind})
                count += 1
        return count

    def _host(self, now: datetime) -> int:
        root = Path(settings.media_default_storage_root).resolve().anchor or str(Path.cwd().anchor)
        usage = shutil.disk_usage(root)
        free_percent = usage.free * 100 / usage.total if usage.total else 0
        state = "DEGRADED" if free_percent < 5 else "HEALTHY"
        self.health.observe(component_type="HOST", component_id="system-disk", display_name="System disk",
            state=state, summary=f"{free_percent:.1f}% free", observed_at=now,
            capabilities={"claim": "VERIFIED", "free_bytes": usage.free, "total_bytes": usage.total})
        self.health.observe(component_type="HOST", component_id="process", display_name="Monitor process",
            state="HEALTHY", summary="Monitor process completed this observation cycle", observed_at=now,
            capabilities={"claim": "VERIFIED", "cpu_memory_metrics": "not commissioned"})
        return 2
