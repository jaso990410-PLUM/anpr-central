from __future__ import annotations

from app.integrations.visionzenith.command_manager import VisionZenithCommandManager
from app.models.barrier import BarrierAction
from app.models.camera import Camera


class VisionZenithBarrierAdapter:
    """Translate an approved durable barrier action into the RA5 command."""

    def __init__(self, command_manager: VisionZenithCommandManager) -> None:
        self._commands = command_manager

    def open(self, action: BarrierAction, camera: Camera):
        if not camera.serial_number:
            raise RuntimeError("Camera serial number is required")
        return self._commands.enqueue_gate_open(
            barrier_action_id=action.id,
            camera_id=camera.id,
            serial_number=camera.serial_number,
        )
