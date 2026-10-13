from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import string
import re
from typing import Annotated
from uuid import UUID
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.csrf import generate_csrf_token
from app.db.dependencies import get_db
from app.dependencies.auth import require_page_permission, require_permission, user_has_permission
from app.dependencies.csrf import require_session_csrf
from app.models.camera import Camera
from app.models.media import EventMediaReference, MediaEvidenceHold, MediaExport, MediaSource, MediaStorage, RecordingSegment
from app.models.recognition_event import RecognitionEvent
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.dashboard_service import get_dashboard_summary
from app.services.media_service import EventMediaService, MediaConfigurationService, MediaEvidenceService, MediaPlaybackService, MediaValidationError, safe_stream_label, storage_health
from app.services.recorder_service import RecorderError, probe_stream, resolve_stream_uri
from app.services.shell_service import build_navigation, role_label
from app.web import templates

router=APIRouter(prefix="/media")


def _context(request,db,user,active="media"):
    return {"request":request,"user":user,"csrf_token":generate_csrf_token(request.cookies[settings.session_cookie_name]),
        "summary":get_dashboard_summary(db),"active_page":active,"navigation":build_navigation(user),"user_role_label":role_label(user),
        "can_manage_media":user_has_permission(user,"media.manage"),"can_export_media":user_has_permission(user,"media.export")}


def _audit(db,request,user,action,entity_type,entity_id,details=None,site_id=None):
    create_audit_log(db,action=action,actor_user_id=user.id,site_id=site_id,entity_type=entity_type,entity_id=str(entity_id),details=details,ip_address=request.client.host if request.client else None)


def _parse_utc(value:str) -> datetime:
    parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _bounded_video_stream(db:Session,camera_id:UUID,start_at:datetime,end_at:datetime) -> StreamingResponse:
    availability=MediaPlaybackService(db).historical(camera_id,start_at,end_at)
    if not availability.segments: raise HTTPException(404,"No reliable recording covers this time window")
    source=db.scalar(select(MediaSource).where(MediaSource.camera_id==camera_id)); storage=db.get(MediaStorage,source.storage_id) if source else None
    if source is None or storage is None: raise HTTPException(404,"Recording source is unavailable")
    executable=shutil.which(settings.media_ffmpeg_path)
    if executable is None: raise HTTPException(503,"Playback processing is unavailable because ffmpeg is not installed or configured")
    inputs=[MediaPlaybackService.segment_path(segment,storage) for segment in availability.segments]
    offset=max(0.0,(start_at.astimezone(timezone.utc)-(availability.segments[0].started_at.replace(tzinfo=timezone.utc) if availability.segments[0].started_at.tzinfo is None else availability.segments[0].started_at.astimezone(timezone.utc))).total_seconds())
    duration=max(0.001,(end_at-start_at).total_seconds()); manifest=None
    if len(inputs)==1:
        input_args=["-ss",str(offset),"-i",str(inputs[0])]
    else:
        playback_dir=Path(storage.root_path).resolve()/".playback"; playback_dir.mkdir(parents=True,exist_ok=True)
        manifest=playback_dir/f"session_{uuid4().hex}.txt"
        manifest.write_text("".join(f"file '{str(path).replace(chr(39), chr(39)*2)}'\n" for path in inputs),encoding="utf-8")
        input_args=["-ss",str(offset),"-f","concat","-safe","0","-i",str(manifest)]
    codecs={str(segment.codec or source.detected_codec or source.expected_codec or "").lower() for segment in availability.segments}
    video_args=["-c:v","copy"] if codecs <= {"h264","avc1",""} else ["-c:v","libx264","-preset","veryfast"]
    command=[executable,"-hide_banner","-loglevel","error",*input_args,"-t",str(duration),"-map","0:v:0","-an",*video_args,
        "-movflags","frag_keyframe+empty_moov+default_base_moof","-f","mp4","pipe:1"]
    try: process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    except OSError as error:
        if manifest: manifest.unlink(missing_ok=True)
        raise HTTPException(503,"Playback processing could not be started") from error
    def content():
        try:
            while process.stdout:
                chunk=process.stdout.read(64*1024)
                if not chunk: break
                yield chunk
        finally:
            if process.poll() is None: process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill()
            if manifest: manifest.unlink(missing_ok=True)
    return StreamingResponse(content(),media_type="video/mp4",headers={"Cache-Control":"no-store"})


@router.get("",response_class=HTMLResponse)
def media_overview(request:Request,user:User=Depends(require_page_permission("media.view")),db:Session=Depends(get_db)):
    sources=tuple(db.scalars(select(MediaSource).options(joinedload(MediaSource.camera),joinedload(MediaSource.storage)).order_by(MediaSource.camera_id)).all())
    storages=MediaConfigurationService(db).storages(); now=datetime.now(timezone.utc)
    context=_context(request,db,user); context.update({"sources":sources,"storages":tuple((s,storage_health(s)) for s in storages),
        "recording_count":sum(s.recorder_state=="RECORDING" for s in sources),"configured_count":sum(s.recording_enabled for s in sources),
        "oldest":db.scalar(select(func.min(RecordingSegment.started_at)).where(RecordingSegment.state=="COMPLETE")),
        "gaps":sum(s.recorder_state in {"DEGRADED","DISCONNECTED","ERROR"} for s in sources),"safe_stream_label":safe_stream_label})
    return templates.TemplateResponse(request=request,name="media/overview.html",context=context)


@router.get("/storage/folders")
def browse_storage_folders(path:str="",_user:User=Depends(require_permission("media.manage"))):
    """Browse server-side directories without exposing files or file contents."""
    if not path.strip():
        roots=[Path(f"{letter}:\\") for letter in string.ascii_uppercase if Path(f"{letter}:\\").is_dir()] if __import__("os").name=="nt" else [Path("/")]
        return {"current":None,"parent":None,"directories":[{"name":str(root),"path":str(root)} for root in roots]}
    try:
        current=Path(path).expanduser().resolve(strict=True)
        if not current.is_dir(): raise HTTPException(400,"Selected path is not a directory")
        directories=sorted((item for item in current.iterdir() if item.is_dir()),key=lambda item:item.name.casefold())
    except HTTPException: raise
    except (OSError,RuntimeError) as error: raise HTTPException(403,"Directory cannot be accessed by ANPR Central") from error
    parent=current.parent if current.parent != current else None
    return {"current":str(current),"parent":str(parent) if parent else None,
        "directories":[{"name":item.name,"path":str(item)} for item in directories[:500]]}


@router.post("/storage/folders")
def create_storage_folder(request:Request,parent_path:Annotated[str,Form()],folder_name:Annotated[str,Form()],
                          user:User=Depends(require_page_permission("media.manage")),_csrf:str=Depends(require_session_csrf),db:Session=Depends(get_db)):
    name=folder_name.strip()
    invalid_windows_name=re.search(r'[<>:"/\\|?*]',name) or name.endswith((" ","."))
    reserved=name.upper().split(".",1)[0] in {"CON","PRN","AUX","NUL",*(f"COM{i}" for i in range(1,10)),*(f"LPT{i}" for i in range(1,10))}
    if not name or len(name)>100 or name in {".",".."} or invalid_windows_name or reserved:
        raise HTTPException(400,"Enter a valid folder name without path separators")
    try:
        parent=Path(parent_path).expanduser().resolve(strict=True)
        if not parent.is_dir(): raise HTTPException(400,"Current path is not a directory")
        child=(parent/name).resolve(strict=False)
        if child.parent != parent: raise HTTPException(400,"Folder must be created inside the current directory")
        child.mkdir()
    except FileExistsError as error: raise HTTPException(409,"A folder with this name already exists") from error
    except HTTPException: raise
    except (OSError,RuntimeError) as error: raise HTTPException(403,"ANPR Central cannot create a folder in this directory") from error
    _audit(db,request,user,"media.storage_folder_created","media_storage_folder",child,{"parent":str(parent),"name":name}); db.commit()
    return {"name":name,"path":str(child)}


@router.post("/storage")
def save_storage(request:Request,name:Annotated[str,Form()],root_path:Annotated[str,Form()],storage_type:Annotated[str,Form()]="LOCAL",
                 retention_days:Annotated[str,Form()]="30",minimum_free_space_gb:Annotated[int,Form()]=10,minimum_free_space_percent:Annotated[int,Form()]=5,
                 enabled:Annotated[bool,Form()]=False,user:User=Depends(require_page_permission("media.manage")),_csrf:str=Depends(require_session_csrf),db:Session=Depends(get_db)):
    try: item=MediaConfigurationService(db).save_storage(storage_id=None,name=name,storage_type=storage_type,root_path=root_path,enabled=enabled,retention_days=int(retention_days) if retention_days else None,minimum_free_space_gb=minimum_free_space_gb,minimum_free_space_percent=minimum_free_space_percent)
    except (MediaValidationError,ValueError) as error: raise HTTPException(400,str(error)) from error
    _audit(db,request,user,"media.storage_updated","media_storage",item.id,{"name":item.name,"type":item.storage_type,"enabled":item.enabled,"retention_days":item.retention_days}); db.commit()
    return RedirectResponse("/media",303)


@router.get("/cameras/{camera_id}",response_class=HTMLResponse)
def camera_recordings(camera_id:UUID,request:Request,start:str="",end:str="",user:User=Depends(require_page_permission("media.view")),db:Session=Depends(get_db)):
    camera=db.get(Camera,camera_id)
    if camera is None: raise HTTPException(404,"Camera not found")
    availability=None; markers=()
    if start and end:
        try:
            start_at=_parse_utc(start); end_at=_parse_utc(end)
            availability=MediaPlaybackService(db).historical(camera_id,start_at,end_at)
            markers=tuple(db.scalars(select(RecognitionEvent).where(RecognitionEvent.camera_id==camera_id,RecognitionEvent.captured_at>=start_at,RecognitionEvent.captured_at<=end_at).order_by(RecognitionEvent.captured_at)).all())
        except (ValueError,MediaValidationError) as error: raise HTTPException(400,str(error)) from error
    context=_context(request,db,user); context.update({"camera":camera,"source":MediaConfigurationService(db).source_for_camera(camera_id),"availability":availability,"markers":markers,
        "start":start.replace("Z", ""),"end":end.replace("Z", ""),"max_playback_minutes":settings.media_max_playback_minutes})
    return templates.TemplateResponse(request=request,name="media/camera.html",context=context)


@router.get("/cameras/{camera_id}/playback")
def historical_stream(camera_id:UUID,start:str,end:str,_user:User=Depends(require_permission("media.view")),db:Session=Depends(get_db)):
    try: return _bounded_video_stream(db,camera_id,_parse_utc(start),_parse_utc(end))
    except (ValueError,MediaValidationError) as error: raise HTTPException(400,str(error)) from error


@router.get("/cameras/{camera_id}/live",response_class=HTMLResponse)
def live_view(camera_id:UUID,request:Request,user:User=Depends(require_page_permission("media.view")),db:Session=Depends(get_db)):
    camera=db.get(Camera,camera_id)
    if camera is None: raise HTTPException(404,"Camera not found")
    try: source=MediaPlaybackService(db).live_source(camera_id)
    except MediaValidationError as error: raise HTTPException(409,str(error)) from error
    context=_context(request,db,user); context.update({"camera":camera,"source":source})
    return templates.TemplateResponse(request=request,name="media/live.html",context=context)


@router.get("/cameras/{camera_id}/live-stream")
def live_stream(camera_id:UUID,_user:User=Depends(require_permission("media.view")),db:Session=Depends(get_db)):
    try:
        source=MediaPlaybackService(db).live_source(camera_id)
        uri=resolve_stream_uri(source,live=True)
    except (MediaValidationError,RecorderError) as error: raise HTTPException(409,str(error)) from error
    executable=shutil.which(settings.media_ffmpeg_path)
    if executable is None: raise HTTPException(503,"Live proxy is unavailable because ffmpeg is not installed or configured")
    codec=str(source.detected_codec or source.expected_codec or "").lower()
    video_args=["-c:v","copy"] if codec in {"h264","avc1"} else ["-c:v","libx264","-preset","veryfast","-tune","zerolatency"]
    command=[executable,"-hide_banner","-loglevel","error","-rtsp_transport","tcp","-i",uri,"-map","0:v:0","-an",*video_args,
        "-movflags","frag_keyframe+empty_moov+default_base_moof","-f","mp4","pipe:1"]
    try: process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    except OSError as error: raise HTTPException(503,"Live proxy could not be started") from error
    def content():
        try:
            while process.stdout:
                chunk=process.stdout.read(64*1024)
                if not chunk: break
                yield chunk
        finally:
            if process.poll() is None: process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill()
    return StreamingResponse(content(),media_type="video/mp4",headers={"Cache-Control":"no-store"})


@router.post("/cameras/{camera_id}/configure")
def configure_camera(camera_id:UUID,request:Request,provider_type:Annotated[str,Form()],recording_stream_uri:Annotated[str,Form()]="",live_stream_uri:Annotated[str,Form()]="",
                     credential_reference:Annotated[str,Form()]="",storage_id:Annotated[str,Form()]="",pre_event_seconds:Annotated[int,Form()]=10,post_event_seconds:Annotated[int,Form()]=15,
                     segment_duration_seconds:Annotated[int,Form()]=60,media_time_offset_ms:Annotated[int,Form()]=0,expected_codec:Annotated[str,Form()]="",
                     enabled:Annotated[bool,Form()]=False,recording_enabled:Annotated[bool,Form()]=False,
                     user:User=Depends(require_page_permission("media.manage")),_csrf:str=Depends(require_session_csrf),db:Session=Depends(get_db)):
    camera=db.get(Camera,camera_id)
    if camera is None: raise HTTPException(404,"Camera not found")
    service=MediaConfigurationService(db); existing=service.source_for_camera(camera.id); was_recording=bool(existing and existing.recording_enabled)
    try: source=service.save_source(camera,provider_type=provider_type,enabled=enabled,recording_enabled=recording_enabled,
        recording_stream_uri=recording_stream_uri,live_stream_uri=live_stream_uri,credential_reference=credential_reference,
        storage_id=UUID(storage_id) if storage_id else None,pre_event_seconds=pre_event_seconds,post_event_seconds=post_event_seconds,
        segment_duration_seconds=segment_duration_seconds,media_time_offset_ms=media_time_offset_ms,expected_codec=expected_codec)
    except (MediaValidationError,ValueError) as error: raise HTTPException(400,str(error)) from error
    details={"camera_id":str(camera.id),"provider":source.provider_type,"stream":safe_stream_label(source.recording_stream_uri),"storage_id":str(source.storage_id) if source.storage_id else None}
    _audit(db,request,user,"media.source_updated" if existing else "media.source_created","media_source",source.id,details,camera.site_id)
    if was_recording != source.recording_enabled:
        _audit(db,request,user,"media.recording_enabled" if source.recording_enabled else "media.recording_disabled","media_source",source.id,{"camera_id":str(camera.id)},camera.site_id)
    db.commit()
    return RedirectResponse(f"/cameras/{camera.id}",303)


@router.post("/cameras/{camera_id}/test")
def test_stream(camera_id:UUID,request:Request,user:User=Depends(require_page_permission("media.manage")),_csrf:str=Depends(require_session_csrf),db:Session=Depends(get_db)):
    source=MediaConfigurationService(db).source_for_camera(camera_id)
    if source is None: raise HTTPException(409,"Recording source is not configured")
    try: result=probe_stream(source)
    except RecorderError as error: source.recorder_state="ERROR"; source.last_error=str(error); db.commit(); raise HTTPException(409,str(error)) from error
    source.detected_codec=result["codec"]; source.detected_width=result["width"]; source.detected_height=result["height"]; source.approximate_bitrate_kbps=result["bitrate_kbps"]; source.last_stream_activity_at=datetime.now(timezone.utc); source.last_error=None; db.commit()
    return RedirectResponse(f"/cameras/{camera_id}?stream_test=connected",303)


@router.get("/events/{event_id}",response_class=HTMLResponse)
def event_video(event_id:UUID,request:Request,user:User=Depends(require_page_permission("media.view")),db:Session=Depends(get_db)):
    event=db.get(RecognitionEvent,event_id)
    if event is None: raise HTTPException(404,"Recognition event not found")
    ref,availability,mode=MediaPlaybackService(db).event_playback(event_id); db.commit()
    holds=tuple(db.scalars(select(MediaEvidenceHold).where(MediaEvidenceHold.recognition_event_id==event_id).order_by(MediaEvidenceHold.created_at.desc())).all())
    exports=tuple(db.scalars(select(MediaExport).where(MediaExport.recognition_event_id==event_id).order_by(MediaExport.created_at.desc())).all())
    context=_context(request,db,user); context.update({"event":event,"reference":ref,"availability":availability,"playback_mode":mode,"holds":holds,"exports":exports})
    return templates.TemplateResponse(request=request,name="media/event.html",context=context)


@router.get("/events/{event_id}/stream")
def event_video_stream(event_id:UUID,_user:User=Depends(require_permission("media.view")),db:Session=Depends(get_db)):
    ref,availability,_mode=MediaPlaybackService(db).event_playback(event_id)
    if availability.status not in {"AVAILABLE","PARTIAL"}: raise HTTPException(404,f"Event video is {availability.status.lower().replace('_',' ')}")
    return _bounded_video_stream(db,ref.camera_id,ref.clip_start_at,ref.clip_end_at)


@router.get("/segments/{segment_id}")
def segment_content(segment_id:UUID,_user:User=Depends(require_permission("media.view")),db:Session=Depends(get_db)):
    segment=db.get(RecordingSegment,segment_id)
    if segment is None or segment.state!="COMPLETE": raise HTTPException(404,"Recording segment unavailable")
    source=db.get(MediaSource,segment.media_source_id); storage=db.get(MediaStorage,source.storage_id) if source else None
    if storage is None: raise HTTPException(404,"Recording storage unavailable")
    try:path=MediaPlaybackService.segment_path(segment,storage)
    except MediaValidationError as error:raise HTTPException(404,str(error)) from error
    return FileResponse(path,media_type="video/mp4",filename=None)


@router.post("/events/{event_id}/protect")
def protect_event(event_id:UUID,request:Request,reason:Annotated[str,Form()],user:User=Depends(require_page_permission("media.manage")),_csrf:str=Depends(require_session_csrf),db:Session=Depends(get_db)):
    ref=EventMediaService(db).for_event(event_id,resolve=False)
    if ref is None: raise HTTPException(404,"Recognition event not found")
    hold=MediaEvidenceService(db).protect(camera_id=ref.camera_id,start_at=ref.clip_start_at,end_at=ref.clip_end_at,reason=reason,user_id=user.id,event_id=event_id)
    _audit(db,request,user,"media.evidence_protected","media_evidence_hold",hold.id,{"event_id":str(event_id),"reason":reason}); db.commit(); return RedirectResponse(f"/media/events/{event_id}",303)


@router.post("/holds/{hold_id}/release")
def release_hold(hold_id:UUID,request:Request,user:User=Depends(require_page_permission("media.manage")),_csrf:str=Depends(require_session_csrf),db:Session=Depends(get_db)):
    hold=db.get(MediaEvidenceHold,hold_id)
    if hold is None:raise HTTPException(404,"Evidence hold not found")
    MediaEvidenceService(db).release(hold,user.id); _audit(db,request,user,"media.evidence_unprotected","media_evidence_hold",hold.id); db.commit()
    return RedirectResponse(f"/media/events/{hold.recognition_event_id}" if hold.recognition_event_id else "/media",303)


@router.post("/events/{event_id}/export")
def export_event(event_id:UUID,request:Request,user:User=Depends(require_page_permission("media.export")),_csrf:str=Depends(require_session_csrf),db:Session=Depends(get_db)):
    ref=EventMediaService(db).for_event(event_id,resolve=False)
    if ref is None:raise HTTPException(404,"Recognition event not found")
    job=MediaEvidenceService(db).request_export(camera_id=ref.camera_id,start_at=ref.clip_start_at,end_at=ref.clip_end_at,user_id=user.id,event_id=event_id)
    _audit(db,request,user,"media.export_requested","media_export",job.id,{"event_id":str(event_id),"start":ref.clip_start_at.isoformat(),"end":ref.clip_end_at.isoformat()}); db.commit(); return RedirectResponse(f"/media/events/{event_id}",303)


@router.get("/exports/{export_id}/download")
def download_export(export_id:UUID,request:Request,user:User=Depends(require_page_permission("media.export")),db:Session=Depends(get_db)):
    job=db.get(MediaExport,export_id)
    if job is None or job.state!="COMPLETED" or not job.output_relative_path:raise HTTPException(404,"Completed export not found")
    source=db.scalar(select(MediaSource).where(MediaSource.camera_id==job.camera_id)); storage=db.get(MediaStorage,source.storage_id) if source else None
    if storage is None:raise HTTPException(404,"Export storage unavailable")
    fake=RecordingSegment(relative_path=job.output_relative_path,state="COMPLETE",camera_id=job.camera_id,media_source_id=source.id,started_at=job.start_at)
    try:path=MediaPlaybackService.segment_path(fake,storage)
    except MediaValidationError as error:raise HTTPException(404,str(error)) from error
    _audit(db,request,user,"media.export_downloaded","media_export",job.id,{"checksum_sha256":job.checksum_sha256}); db.commit()
    return FileResponse(path,media_type="video/mp4",filename=f"anpr-evidence-{job.id}.mp4")
