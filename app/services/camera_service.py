from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import re
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models.camera import Camera
from app.models.site import Site


UNPROVISIONED = "UNPROVISIONED"
CONNECTING = "CONNECTING"
CREDENTIALS_REVOKED = "CREDENTIALS_REVOKED"
DISABLED = "DISABLED"
ONLINE = "ONLINE"
OFFLINE = "OFFLINE"

_DIRECTION_PATTERN = re.compile(r"^[A-Z][A-Z0-9_-]*$")


class CameraValidationError(ValueError):
    """A safe validation error suitable for the camera-management UI."""


@dataclass(frozen=True)
class CameraListItem:
    id: UUID
    code: str
    name: str
    site_code: str
    site_name: str
    direction: str
    manufacturer_model: str
    ip_address: str | None
    provisioning_state: str
    connection_state: str
    is_enabled: bool
    last_seen_display: str | None


@dataclass(frozen=True)
class CameraDetail:
    id: UUID
    code: str
    name: str
    site_code: str
    site_name: str
    site_timezone: str
    direction: str
    manufacturer: str | None
    model: str | None
    serial_number: str | None
    ip_address: str | None
    is_enabled: bool
    provisioning_state: str
    connection_state: str
    last_seen_display: str | None
    status_topic: str
    event_topic: str
    command_topic: str
    whitelist_topic: str


class CameraService:
    def __init__(self, db: Session):
        self.db = db

    def list_cameras(self) -> tuple[CameraListItem, ...]:
        cameras = self.db.scalars(
            select(Camera)
            .options(joinedload(Camera.site))
            .order_by(Camera.code)
        ).all()
        return tuple(
            CameraListItem(
                id=camera.id,
                code=camera.code,
                name=camera.name,
                site_code=camera.site.code,
                site_name=camera.site.name,
                direction=camera.direction,
                manufacturer_model=_manufacturer_model(camera),
                ip_address=camera.ip_address,
                provisioning_state=camera.provisioning_state,
                connection_state=connection_state(camera),
                is_enabled=camera.is_enabled,
                last_seen_display=_display_time(
                    camera.last_seen_at,
                    camera.site.timezone,
                ),
            )
            for camera in cameras
        )

    def list_sites(self) -> tuple[Site, ...]:
        return tuple(
            self.db.scalars(select(Site).order_by(Site.code)).all()
        )

    def get_camera(self, camera_id: UUID) -> Camera | None:
        return self.db.scalar(
            select(Camera)
            .options(joinedload(Camera.site))
            .where(Camera.id == camera_id)
        )

    def camera_detail(self, camera: Camera) -> CameraDetail:
        topic_root = f"anpr/{camera.id}"
        return CameraDetail(
            id=camera.id,
            code=camera.code,
            name=camera.name,
            site_code=camera.site.code,
            site_name=camera.site.name,
            site_timezone=camera.site.timezone,
            direction=camera.direction,
            manufacturer=camera.manufacturer,
            model=camera.model,
            serial_number=camera.serial_number,
            ip_address=camera.ip_address,
            is_enabled=camera.is_enabled,
            provisioning_state=camera.provisioning_state,
            connection_state=connection_state(camera),
            last_seen_display=_display_time(
                camera.last_seen_at,
                camera.site.timezone,
            ),
            status_topic=f"{topic_root}/status",
            event_topic=f"{topic_root}/event",
            command_topic=f"{topic_root}/command",
            whitelist_topic=f"{topic_root}/whitelist",
        )

    def create_camera(
        self,
        *,
        site_id: str,
        code: str,
        name: str,
        direction: str,
        manufacturer: str | None,
        model: str | None,
        serial_number: str | None,
        ip_address: str | None,
        is_enabled: bool,
    ) -> Camera:
        clean_code = code.strip().upper()
        clean_name = name.strip()
        clean_direction = direction.strip().upper()
        clean_manufacturer = _clean_optional(manufacturer)
        clean_model = _clean_optional(model)
        clean_serial = _clean_optional(serial_number)
        clean_ip = _clean_optional(ip_address)

        self._validate_text_fields(
            code=clean_code,
            name=clean_name,
            direction=clean_direction,
            manufacturer=clean_manufacturer,
            model=clean_model,
            serial_number=clean_serial,
        )
        if self._code_exists(clean_code):
            raise CameraValidationError("That camera code is already in use.")
        if clean_serial is not None and self._serial_exists(clean_serial):
            raise CameraValidationError("That serial number is already in use.")
        if clean_ip is not None:
            try:
                clean_ip = str(ipaddress.ip_address(clean_ip))
            except ValueError as error:
                raise CameraValidationError("Enter a valid IP address.") from error

        try:
            parsed_site_id = UUID(site_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise CameraValidationError("Select a valid site.") from error
        site = self.db.get(Site, parsed_site_id)
        if site is None:
            raise CameraValidationError("Select a valid site.")

        try:
            with self.db.begin_nested():
                camera = Camera(
                    site=site,
                    code=clean_code,
                    name=clean_name,
                    direction=clean_direction,
                    manufacturer=clean_manufacturer,
                    model=clean_model,
                    serial_number=clean_serial,
                    ip_address=clean_ip,
                    is_enabled=is_enabled,
                    provisioning_state=UNPROVISIONED,
                )
                self.db.add(camera)
                self.db.flush()
        except IntegrityError as error:
            raise CameraValidationError(
                "The camera code or serial number is already in use."
            ) from error
        return camera

    def set_enabled(self, camera: Camera, *, enabled: bool) -> bool:
        changed = camera.is_enabled != enabled
        camera.is_enabled = enabled
        self.db.flush()
        return changed

    def revoke_provisioning(self, camera: Camera) -> bool:
        changed = camera.provisioning_state != CREDENTIALS_REVOKED
        camera.provisioning_state = CREDENTIALS_REVOKED
        self.db.flush()
        return changed

    def _code_exists(self, code: str) -> bool:
        return self.db.scalar(
            select(Camera.id).where(func.lower(Camera.code) == code.lower())
        ) is not None

    def _serial_exists(self, serial_number: str) -> bool:
        return self.db.scalar(
            select(Camera.id).where(
                func.lower(Camera.serial_number) == serial_number.lower()
            )
        ) is not None

    @staticmethod
    def _validate_text_fields(
        *,
        code: str,
        name: str,
        direction: str,
        manufacturer: str | None,
        model: str | None,
        serial_number: str | None,
    ) -> None:
        if not code:
            raise CameraValidationError("Camera code is required.")
        if len(code) > 80:
            raise CameraValidationError("Camera code must contain at most 80 characters.")
        if not name:
            raise CameraValidationError("Camera name is required.")
        if len(name) > 150:
            raise CameraValidationError("Camera name must contain at most 150 characters.")
        if not direction or not _DIRECTION_PATTERN.fullmatch(direction):
            raise CameraValidationError(
                "Direction must begin with a letter and use letters, numbers, hyphens, or underscores."
            )
        if len(direction) > 20:
            raise CameraValidationError("Direction must contain at most 20 characters.")
        for value, label, maximum in (
            (manufacturer, "Manufacturer", 100),
            (model, "Model", 100),
            (serial_number, "Serial number", 100),
        ):
            if value is not None and len(value) > maximum:
                raise CameraValidationError(
                    f"{label} must contain at most {maximum} characters."
                )


def connection_state(camera: Camera) -> str:
    if not camera.is_enabled:
        return DISABLED
    if camera.provisioning_state == CREDENTIALS_REVOKED:
        return CREDENTIALS_REVOKED
    if camera.provisioning_state == CONNECTING:
        return CONNECTING
    return UNPROVISIONED


def _manufacturer_model(camera: Camera) -> str:
    values = [value for value in (camera.manufacturer, camera.model) if value]
    return " / ".join(values) if values else "Not provided"


def _clean_optional(value: str | None) -> str | None:
    return (value or "").strip() or None


def _display_time(value: datetime | None, timezone_name: str) -> str | None:
    if value is None:
        return None
    utc_value = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    try:
        display_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        display_timezone = timezone.utc
    return utc_value.astimezone(display_timezone).strftime("%Y-%m-%d %H:%M:%S")
