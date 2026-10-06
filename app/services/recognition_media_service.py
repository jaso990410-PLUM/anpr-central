import os
from pathlib import Path
import tempfile
from uuid import UUID

from app.core.config import settings
from app.integrations.visionzenith.recognition import image_bytes


class RecognitionMediaError(ValueError):
    pass


class RecognitionMediaService:
    def __init__(self, root: Path | None = None) -> None:
        configured = root or settings.recognition_media_dir
        self.root = configured.resolve()

    def store(self, event_id: UUID, kind: str, encoded: str) -> str:
        if kind not in {"scene", "plate"}:
            raise RecognitionMediaError("Unsupported recognition image kind")
        try:
            data, extension = image_bytes(
                encoded,
                max_decoded_bytes=settings.recognition_max_image_bytes,
            )
        except (ValueError, TypeError) as error:
            raise RecognitionMediaError("Recognition image is invalid") from error
        self.root.mkdir(parents=True, exist_ok=True)
        filename = f"{event_id}_{kind}{extension}"
        destination = (self.root / filename).resolve()
        if destination.parent != self.root:
            raise RecognitionMediaError("Unsafe recognition image path")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.root,
                prefix=f".{event_id}_{kind}_",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                temporary_path = Path(stream.name)
            os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
        return filename

    def resolve(self, reference: str) -> Path:
        if not reference or Path(reference).name != reference:
            raise RecognitionMediaError("Unsafe recognition image reference")
        path = (self.root / reference).resolve()
        if path.parent != self.root or not path.is_file():
            raise RecognitionMediaError("Recognition image is unavailable")
        return path
