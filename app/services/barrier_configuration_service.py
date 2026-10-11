from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.barrier import BarrierConfiguration, BarrierState
from app.models.camera import Camera


CONTROL_MODES = {"DISABLED", "CAMERA_LOCAL", "CENTRAL_MANAGED"}
FEEDBACK_MODES = {"NONE", "NATIVE_GATE_STATUS", "GPIO_INPUTS"}
ACTIVE_LEVELS = {"HIGH", "LOW"}


class BarrierConfigurationError(ValueError):
    pass


class BarrierConfigurationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, camera_id: UUID) -> BarrierConfiguration | None:
        return self.db.scalar(select(BarrierConfiguration).where(BarrierConfiguration.camera_id == camera_id))

    def save(
        self,
        camera: Camera,
        *,
        enabled: bool,
        control_mode: str,
        feedback_mode: str,
        confirmation_timeout: str | int,
        open_input: str | int | None,
        open_level: str,
        closed_input: str | int | None,
        closed_level: str,
        notes: str,
    ) -> tuple[BarrierConfiguration, bool]:
        if control_mode not in CONTROL_MODES or feedback_mode not in FEEDBACK_MODES:
            raise BarrierConfigurationError("Select supported barrier control and feedback modes.")
        if open_level not in ACTIVE_LEVELS or closed_level not in ACTIVE_LEVELS:
            raise BarrierConfigurationError("GPIO active level must be High or Low.")
        try:
            timeout = int(confirmation_timeout)
        except (TypeError, ValueError) as error:
            raise BarrierConfigurationError("Confirmation timeout must be a whole number.") from error
        if not 1 <= timeout <= 60:
            raise BarrierConfigurationError("Confirmation timeout must be between 1 and 60 seconds.")
        open_gpio = _gpio(open_input)
        closed_gpio = _gpio(closed_input)
        if feedback_mode == "GPIO_INPUTS" and open_gpio is None and closed_gpio is None:
            raise BarrierConfigurationError("Configure at least one GPIO feedback input.")
        if open_gpio is not None and open_gpio == closed_gpio:
            raise BarrierConfigurationError("Open and Closed feedback must use different GPIO inputs.")
        config = self.get(camera.id)
        created = config is None
        if config is None:
            config = BarrierConfiguration(camera_id=camera.id)
            self.db.add(config)
        ownership_changed = config.control_mode != control_mode
        feedback_changed = config.feedback_mode != feedback_mode or config.open_status_input != open_gpio or config.closed_status_input != closed_gpio or config.open_active_level != open_level or config.closed_active_level != closed_level
        config.enabled = enabled
        config.control_mode = control_mode
        config.feedback_mode = feedback_mode
        config.open_confirmation_timeout_seconds = timeout
        config.open_status_input = open_gpio if feedback_mode == "GPIO_INPUTS" else None
        config.closed_status_input = closed_gpio if feedback_mode == "GPIO_INPUTS" else None
        config.open_active_level = open_level
        config.closed_active_level = closed_level
        config.commissioning_notes = notes.strip()[:2000] or None
        if not enabled or control_mode != "CENTRAL_MANAGED" or ownership_changed:
            self._clear_commissioning(config)
        state = self.db.scalar(select(BarrierState).where(BarrierState.camera_id == camera.id))
        if state is None:
            state = BarrierState(camera_id=camera.id)
            self.db.add(state)
        if feedback_changed:
            state.normalized_state = "UNKNOWN"
            state.feedback_source = feedback_mode
            state.observed_at = None
            state.open_input_raw = None
            state.closed_input_raw = None
        self.db.flush()
        return config, created

    def commission(self, config: BarrierConfiguration, user_id: UUID, *, local_auto_open_verified: bool, notes: str = "") -> None:
        if not config.enabled or config.control_mode != "CENTRAL_MANAGED":
            raise BarrierConfigurationError("Enable Central Managed mode before commissioning.")
        if not local_auto_open_verified:
            raise BarrierConfigurationError("Verify that camera-local automatic opening is disabled or non-conflicting.")
        now = datetime.now(timezone.utc)
        config.local_auto_open_verified = True
        config.local_auto_open_verified_at = now
        config.local_auto_open_verified_by_user_id = user_id
        config.commissioned_at = now
        config.commissioned_by_user_id = user_id
        if notes.strip():
            config.commissioning_notes = notes.strip()[:2000]
        self.db.flush()

    def decommission(self, config: BarrierConfiguration) -> None:
        self._clear_commissioning(config)
        self.db.flush()

    @staticmethod
    def _clear_commissioning(config: BarrierConfiguration) -> None:
        config.commissioned_at = None
        config.commissioned_by_user_id = None
        config.local_auto_open_verified = False
        config.local_auto_open_verified_at = None
        config.local_auto_open_verified_by_user_id = None


def _gpio(value: str | int | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise BarrierConfigurationError("GPIO input must be a whole number.") from error
    if not 0 <= parsed <= 255:
        raise BarrierConfigurationError("GPIO input must be between 0 and 255.")
    return parsed
