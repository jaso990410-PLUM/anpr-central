from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models.media import EventMediaReference, MediaEvidenceHold, MediaExport, MediaSource, MediaStorage, RecordingSegment
from app.services.media_provider import CentralRecorderProvider
from app.services.media_service import MediaPlaybackService, MediaValidationError, storage_health
from app.services.playback_artifact_service import cleanup_expired_playback_artifacts
from app.services.audit_service import create_audit_log

logger = logging.getLogger(__name__)


class RecorderError(RuntimeError): pass


def _recording_segment_start_state(source: MediaSource) -> str:
    if source.last_successful_segment_at is None:
        return "STARTING"
    if source.recorder_state in {"ERROR", "DISCONNECTED", "DEGRADED"}:
        return "DEGRADED"
    return "RECORDING"


def resolve_stream_uri(source: MediaSource, *, live: bool = False) -> str:
    uri = source.live_stream_uri if live and source.live_stream_uri else source.recording_stream_uri
    if not uri: raise RecorderError("RTSP stream is not configured")
    if not source.credential_reference: return uri
    key=f"ANPR_MEDIA_CREDENTIAL_{source.credential_reference}"
    secret=os.getenv(key)
    if not secret: raise RecorderError(f"Credential reference {source.credential_reference} is unavailable")
    try:
        doc=json.loads(secret); username=str(doc["username"]); password=str(doc["password"])
    except (ValueError, KeyError, TypeError) as error: raise RecorderError(f"Credential reference {source.credential_reference} is invalid") from error
    parts=urlsplit(uri); netloc=f"{quote(username,safe='')}:{quote(password,safe='')}@{parts.hostname}"
    if parts.port: netloc += f":{parts.port}"
    return urlunsplit((parts.scheme,netloc,parts.path,parts.query,parts.fragment))


def probe_stream(source: MediaSource, *, timeout: int = 10) -> dict[str, object]:
    uri=resolve_stream_uri(source)
    command=[settings.media_ffprobe_path,"-v","error","-rtsp_transport","tcp","-select_streams","v:0","-show_entries","stream=codec_name,width,height,bit_rate,r_frame_rate","-of","json",uri]
    try: result=subprocess.run(command,capture_output=True,text=True,timeout=timeout,check=False)
    except FileNotFoundError as error: raise RecorderError("ffprobe is not installed or configured") from error
    except subprocess.TimeoutExpired as error: raise RecorderError("RTSP stream test timed out") from error
    if result.returncode != 0: raise RecorderError("RTSP stream could not be read")
    try: stream=json.loads(result.stdout)["streams"][0]
    except (ValueError,KeyError,IndexError,TypeError) as error: raise RecorderError("RTSP stream returned no video metadata") from error
    return {"codec":stream.get("codec_name"),"width":stream.get("width"),"height":stream.get("height"),"bitrate_kbps":int(stream["bit_rate"])//1000 if str(stream.get("bit_rate","")).isdigit() else None,"frame_rate":stream.get("r_frame_rate")}


class RecordingWorker:
    BACKOFF=(1,2,5,10,30)
    def __init__(self, source_id: UUID, session_factory: sessionmaker[Session], stop: Event):
        self.source_id=source_id; self.session_factory=session_factory; self.stop=stop; self.thread: Thread|None=None; self.process=None
    def start(self):
        self.thread=Thread(target=self.run,name=f"anpr-recorder-{self.source_id}",daemon=True); self.thread.start()
    def join(self):
        if self.process is not None: self.process.terminate()
        if self.thread: self.thread.join(timeout=10)
    def run(self):
        failures=0
        while not self.stop.is_set():
            try: self._record_segment(); failures=0
            except Exception as error:
                failures+=1; self._mark_error(error, failures)
                self.stop.wait(self.BACKOFF[min(failures-1,len(self.BACKOFF)-1)])
    def _record_segment(self):
        with self.session_factory() as db:
            source=db.get(MediaSource,self.source_id)
            if source is None or not source.enabled or not source.recording_enabled or source.provider_type!="CENTRAL_RECORDER": self.stop.wait(2); return
            storage=db.get(MediaStorage,source.storage_id)
            if storage is None or storage_health(storage)["status"]!="HEALTHY": raise RecorderError("Recording storage is unavailable or below free-space threshold")
            uri=resolve_stream_uri(source)
            executable=shutil.which(settings.media_ffmpeg_path)
            if executable is None: raise RecorderError("ffmpeg is not installed or configured")
            start=datetime.now(timezone.utc); folder=Path(str(source.camera_id))/start.strftime("%Y/%m/%d/%H")
            name=f"segment_{start.strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}.mp4"; relative=(folder/name).as_posix()
            root=Path(storage.root_path).resolve(); final=(root/relative).resolve()
            if root != final and root not in final.parents: raise RecorderError("Recording path escaped the configured storage root")
            final.parent.mkdir(parents=True,exist_ok=True); temporary=final.with_suffix(".partial")
            segment=RecordingSegment(media_source_id=source.id,camera_id=source.camera_id,started_at=start,relative_path=relative,state="WRITING")
            source.recorder_state=_recording_segment_start_state(source); db.add(segment); db.commit(); segment_id=segment.id
            duration=source.segment_duration_seconds
        command=[executable,"-hide_banner","-loglevel","error","-rtsp_transport","tcp","-i",uri,"-map","0:v:0","-an","-c","copy","-t",str(duration),"-movflags","+faststart","-f","mp4",str(temporary)]
        try: self.process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        except FileNotFoundError as error: raise RecorderError("ffmpeg is not installed or configured") from error
        while self.process.poll() is None and not self.stop.wait(0.5): pass
        if self.stop.is_set() and self.process.poll() is None: self.process.terminate()
        returncode=self.process.wait(timeout=10); self.process=None; ended=datetime.now(timezone.utc)
        with self.session_factory() as db:
            segment=db.get(RecordingSegment,segment_id); source=db.get(MediaSource,self.source_id); storage=db.get(MediaStorage,source.storage_id) if source else None
            if segment is None or source is None: return
            if returncode==0 and temporary.is_file() and temporary.stat().st_size>0:
                os.replace(temporary,final); segment.state="COMPLETE"; segment.ended_at=ended; segment.finalized_at=ended
                segment.file_size_bytes=final.stat().st_size; segment.duration_ms=max(1,int((ended-_utc(segment.started_at)).total_seconds()*1000))
                segment.codec=source.detected_codec or source.expected_codec; source.recorder_state="RECORDING"; source.last_error=None; source.last_stream_activity_at=ended
                source.last_successful_segment_at=ended; source.consecutive_failures=0; storage.last_successful_write_at=ended if storage else None
            else:
                segment.state="PARTIAL" if temporary.exists() and temporary.stat().st_size else "FAILED"; segment.ended_at=ended; segment.finalized_at=ended
                source.recorder_state="DISCONNECTED"; source.consecutive_failures+=1; source.last_error="RTSP recording segment failed"
            db.commit()
    def _mark_error(self,error,failures):
        logger.warning("Recorder worker degraded for source %s: %s",self.source_id,_safe_error(error))
        with self.session_factory() as db:
            source=db.get(MediaSource,self.source_id)
            if source: source.recorder_state="ERROR" if "storage" in str(error).lower() or "ffmpeg" in str(error).lower() else "DISCONNECTED"; source.consecutive_failures=failures; source.last_error=_safe_error(error); db.commit()


class RecorderService:
    def __init__(self,session_factory): self.session_factory=session_factory; self.stop_event=Event(); self.workers={}; self.lock=Lock()
    def start(self):
        self.stop_event.clear(); self.reconcile_unfinished(); self.sync_workers()
    def stop(self):
        self.stop_event.set()
        for worker in tuple(self.workers.values()): worker.join()
        self.workers.clear()
    def sync_workers(self):
        with self.session_factory() as db:
            ids=set(db.scalars(select(MediaSource.id).where(MediaSource.provider_type=="CENTRAL_RECORDER",MediaSource.enabled.is_(True),MediaSource.recording_enabled.is_(True))).all())
        for source_id in ids-set(self.workers):
            worker=RecordingWorker(source_id,self.session_factory,self.stop_event); self.workers[source_id]=worker; worker.start()
    def reconcile_unfinished(self):
        with self.session_factory() as db:
            for segment in db.scalars(select(RecordingSegment).where(RecordingSegment.state=="WRITING")).all():
                source=db.get(MediaSource,segment.media_source_id); storage=db.get(MediaStorage,source.storage_id) if source else None
                path=(Path(storage.root_path)/segment.relative_path) if storage else None
                segment.state="PARTIAL" if path and path.exists() and path.stat().st_size else "FAILED"; segment.finalized_at=datetime.now(timezone.utc)
            db.commit()
    def maintenance_once(self):
        with self.session_factory() as db:
            resolve_pending_media(db); process_exports(db); apply_retention(db); cleanup_expired_playback_artifacts(db); db.commit()


def resolve_pending_media(db: Session, now=None):
    from app.services.media_service import EventMediaService
    now=_utc(now or datetime.now(timezone.utc)); count=0
    for ref in db.scalars(select(EventMediaReference).where(EventMediaReference.status=="PENDING",EventMediaReference.clip_end_at<=now)).all():
        EventMediaService(db).resolve(ref,now=now); count+=1
    return count


def apply_retention(db: Session, now=None) -> int:
    now=_utc(now or datetime.now(timezone.utc)); removed=0
    for storage in db.scalars(select(MediaStorage).where(MediaStorage.enabled.is_(True))).all():
        health=storage_health(storage); cutoff=now-timedelta(days=storage.retention_days) if storage.retention_days else None
        pressure=health["status"]!="HEALTHY"
        query=select(RecordingSegment).join(MediaSource).where(MediaSource.storage_id==storage.id,RecordingSegment.state=="COMPLETE").order_by(RecordingSegment.started_at)
        for segment in db.scalars(query).all():
            old=cutoff is not None and _utc(segment.started_at)<cutoff
            if not old and not pressure: continue
            held=db.scalar(select(MediaEvidenceHold.id).where(MediaEvidenceHold.camera_id==segment.camera_id,MediaEvidenceHold.released_at.is_(None),MediaEvidenceHold.start_at < segment.ended_at,MediaEvidenceHold.end_at > segment.started_at))
            exporting=db.scalar(select(MediaExport.id).where(MediaExport.camera_id==segment.camera_id,MediaExport.state.in_(("QUEUED","PROCESSING")),MediaExport.start_at < segment.ended_at,MediaExport.end_at > segment.started_at))
            if held or exporting: continue
            root=Path(storage.root_path).resolve(); path=(root/segment.relative_path).resolve()
            if root != path and root not in path.parents: continue
            segment.state="DELETING"; db.flush()
            try:
                if path.exists(): path.unlink()
            except OSError: segment.state="COMPLETE"; continue
            affected=tuple(db.scalars(select(EventMediaReference).where(EventMediaReference.camera_id==segment.camera_id,EventMediaReference.clip_start_at < segment.ended_at,EventMediaReference.clip_end_at > segment.started_at)).all())
            db.delete(segment); db.flush(); removed+=1
            for ref in affected:
                remaining=CentralRecorderProvider(db).get_recording_availability(ref.camera_id,ref.clip_start_at,ref.clip_end_at)
                ref.status="EXPIRED" if remaining.status=="UNAVAILABLE" else remaining.status
            if pressure and storage_health(storage)["status"]=="HEALTHY": pressure=False
    return removed


def process_exports(db: Session) -> int:
    processed=0
    jobs=db.scalars(select(MediaExport).where(MediaExport.state=="QUEUED").order_by(MediaExport.created_at).limit(settings.media_max_export_jobs).with_for_update(skip_locked=True)).all()
    for job in jobs:
        job.state="PROCESSING"; db.flush()
        try:
            source=db.scalar(select(MediaSource).where(MediaSource.camera_id==job.camera_id)); storage=db.get(MediaStorage,source.storage_id) if source else None
            if source is None or storage is None: raise RecorderError("Recording source is unavailable")
            availability=CentralRecorderProvider(db).get_recording_availability(job.camera_id,job.start_at,job.end_at)
            if availability.status=="UNAVAILABLE": raise RecorderError("No recording covers this export window")
            export_dir=Path(storage.root_path).resolve()/"exports"; export_dir.mkdir(parents=True,exist_ok=True)
            output=export_dir/f"evidence_{job.id}.mp4"; temporary=export_dir/f"evidence_{job.id}.partial"; inputs=[MediaPlaybackService.segment_path(s,storage) for s in availability.segments]
            offset=max(0.0,(_utc(job.start_at)-_utc(availability.segments[0].started_at)).total_seconds())
            duration=max(0.001,(_utc(job.end_at)-_utc(job.start_at)).total_seconds()); manifest=None
            if len(inputs)==1:
                command=[settings.media_ffmpeg_path,"-hide_banner","-loglevel","error","-ss",str(offset),"-i",str(inputs[0]),"-t",str(duration),"-map","0:v:0","-an","-c","copy","-f","mp4",str(temporary)]
            else:
                manifest=export_dir/f"evidence_{job.id}.txt"; manifest.write_text("".join(f"file '{str(p).replace(chr(39), chr(39)*2)}'\n" for p in inputs),encoding="utf-8")
                command=[settings.media_ffmpeg_path,"-hide_banner","-loglevel","error","-ss",str(offset),"-f","concat","-safe","0","-i",str(manifest),"-t",str(duration),"-map","0:v:0","-an","-c","copy","-f","mp4",str(temporary)]
            result=subprocess.run(command,capture_output=True,timeout=300,check=False)
            if manifest: manifest.unlink(missing_ok=True)
            if result.returncode or not temporary.is_file() or not temporary.stat().st_size: raise RecorderError("Evidence export remux failed")
            os.replace(temporary,output)
            digest=hashlib.sha256(output.read_bytes()).hexdigest(); job.state="COMPLETED"; job.output_relative_path=output.relative_to(Path(storage.root_path).resolve()).as_posix()
            job.file_size_bytes=output.stat().st_size; job.checksum_sha256=digest; job.completed_at=datetime.now(timezone.utc); job.error=None; processed+=1
            create_audit_log(db,action="media.export_completed",actor_user_id=job.requested_by_user_id,entity_type="media_export",entity_id=str(job.id),details={"checksum_sha256":digest,"file_size_bytes":job.file_size_bytes})
        except Exception as error: job.state="FAILED"; job.error=_safe_error(error)
    return processed


def _safe_error(error):
    text=str(error); return text[:500] if "@" not in text else "Media operation failed; credentials were redacted"
def _utc(value): return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
