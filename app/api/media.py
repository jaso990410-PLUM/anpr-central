from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import string
import re
from typing import Annotated
from urllib.parse import quote
from uuid import UUID
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
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
from app.models.site import Site
from app.models.user import User
from app.services.audit_service import create_audit_log
from app.services.dashboard_service import get_dashboard_summary
from app.services.media_service import EventMediaService, MediaConfigurationService, MediaEvidenceService, MediaPlaybackService, MediaValidationError, safe_stream_label, storage_health
from app.services.playback_artifact_service import (
    EventPlaybackArtifactService,
    PlaybackArtifactError,
)
from app.services.playback_workspace_service import (
    PlaybackWorkspaceService,
    local_day_bounds,
    site_zone,
    utc,
)
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


def _parse_utc(value:str, *, naive_timezone:str="UTC") -> datetime:
    clean=value.strip().replace("Z","+00:00")
    # In URL query strings an unescaped plus is decoded as a space. Accept
    # links generated before playback timestamps were percent-encoded.
    clean=re.sub(r" (?=\d{2}:\d{2}$)", "+", clean)
    parsed=datetime.fromisoformat(clean)
    if parsed.tzinfo is None:
        try: source_timezone=ZoneInfo(naive_timezone)
        except ZoneInfoNotFoundError: source_timezone=timezone.utc
        parsed=parsed.replace(tzinfo=source_timezone)
    return parsed.astimezone(timezone.utc)


def _display_time(value: datetime, timezone_name: str, *, milliseconds: bool = False) -> str:
    try:
        display_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        display_timezone = timezone.utc
    local = _parse_utc(value.isoformat()).astimezone(display_timezone)
    if milliseconds:
        return local.strftime("%d %b %Y · %H:%M:%S.%f")[:-3]
    return local.strftime("%d %b %Y · %H:%M:%S")


def _display_duration(seconds: float) -> str:
    whole_seconds = max(0, round(seconds))
    minutes, remainder = divmod(whole_seconds, 60)
    return f"{minutes}:{remainder:02d}"


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
        "gaps":sum(s.recorder_state in {"DEGRADED","DISCONNECTED","ERROR"} for s in sources),"safe_stream_label":safe_stream_label,
        "display_media_time":_display_time})
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


@router.get("/playback", response_class=HTMLResponse)
def playback_workspace(
    request: Request,
    site: str = "",
    camera: list[str] = Query(default=[]),
    selected_date: str = Query(default="", alias="date"),
    selected_time: str = Query(default="", alias="time"),
    zoom: str = "24h",
    event: str = "",
    start: str = "",
    end: str = "",
    user: User = Depends(require_page_permission("media.view")),
    db: Session = Depends(get_db),
):
    service = PlaybackWorkspaceService(db)
    sites = service.sites()
    selected_event = None
    if event:
        try:
            selected_event = db.get(RecognitionEvent, UUID(event))
        except ValueError as error:
            raise HTTPException(400, "Select a valid recognition event") from error
        if selected_event is None:
            raise HTTPException(404, "Recognition event not found")
    try:
        requested_site_id = UUID(site) if site else None
    except ValueError as error:
        raise HTTPException(400, "Select a valid site") from error
    selected_site = db.get(Site, selected_event.site_id) if selected_event else (db.get(Site, requested_site_id) if requested_site_id else None)
    if requested_site_id and selected_site is None:
        raise HTTPException(404, "Site not found")
    if selected_site is None:
        selected_site = next((candidate for candidate in sites if service.cameras(candidate.id)), sites[0] if sites else None)
    if selected_site is None:
        raise HTTPException(404, "No active site is available")
    zone = site_zone(selected_site)
    cameras = service.cameras(selected_site.id)
    camera_by_id = {camera_row.id: camera_row for camera_row in cameras}
    selected_camera_ids: list[UUID] = []
    for value in camera:
        try:
            camera_id = UUID(value)
        except ValueError as error:
            raise HTTPException(400, "Select a valid camera") from error
        if camera_id in camera_by_id and camera_id not in selected_camera_ids:
            selected_camera_ids.append(camera_id)
    if selected_event and selected_event.camera_id in camera_by_id:
        selected_camera_ids = [selected_event.camera_id, *[item for item in selected_camera_ids if item != selected_event.camera_id]]
    selected_camera_ids = selected_camera_ids[:4]
    if not selected_camera_ids and cameras:
        selected_camera_ids = [cameras[0].id]

    playhead = None
    if selected_event:
        selected_source = MediaConfigurationService(db).source_for_camera(selected_event.camera_id)
        playhead = utc(selected_event.captured_at or selected_event.received_at) + timedelta(
            milliseconds=selected_source.media_time_offset_ms if selected_source else 0
        ) - timedelta(seconds=2)
    elif selected_time:
        try:
            playhead = _parse_utc(selected_time, naive_timezone=selected_site.timezone)
        except ValueError as error:
            raise HTTPException(400, "Select a valid playback time") from error
    elif start:
        try:
            playhead = _parse_utc(start, naive_timezone=selected_site.timezone)
        except ValueError as error:
            raise HTTPException(400, "Select a valid advanced range") from error
    if playhead is not None:
        day = playhead.astimezone(zone).date()
    elif selected_date:
        try:
            day = date.fromisoformat(selected_date)
        except ValueError as error:
            raise HTTPException(400, "Select a valid playback date") from error
    else:
        day = datetime.now(zone).date()
    if zoom not in {"24h", "6h", "1h", "15m"}:
        raise HTTPException(400, "Select a valid timeline zoom")
    day_start, day_end = local_day_bounds(day, zone)
    coverage_by_camera = {
        camera_id: service.coverage(camera_id, day_start, day_end)
        for camera_id in selected_camera_ids
    }
    if playhead is None:
        latest = max(
            (datetime.fromisoformat(str(item["end"]).replace("Z", "+00:00")) for ranges in coverage_by_camera.values() for item in ranges),
            default=None,
        )
        playhead = (latest - timedelta(seconds=1)) if latest else datetime.combine(day, time(hour=12), zone).astimezone(timezone.utc)
    playhead = min(max(playhead, day_start), day_end - timedelta(milliseconds=1))

    advanced_end = None
    if start or end:
        if not start or not end or not selected_camera_ids:
            raise HTTPException(400, "Advanced Search requires a camera, start, and end")
        try:
            advanced_start = _parse_utc(start, naive_timezone=selected_site.timezone)
            advanced_end = _parse_utc(end, naive_timezone=selected_site.timezone)
            MediaPlaybackService(db).historical(selected_camera_ids[0], advanced_start, advanced_end)
        except (ValueError, MediaValidationError) as error:
            raise HTTPException(400, str(error)) from error
        playhead = advanced_start

    markers = service.markers(selected_site.id, tuple(selected_camera_ids), day_start, day_end, zone)
    selected_cameras = tuple(camera_by_id[item] for item in selected_camera_ids)
    camera_states = {}
    for camera_row in cameras:
        source = MediaConfigurationService(db).source_for_camera(camera_row.id)
        camera_states[str(camera_row.id)] = source.recorder_state if source else "NOT CONFIGURED"
    camera_data = []
    for camera_row in selected_cameras:
        source = MediaConfigurationService(db).source_for_camera(camera_row.id)
        camera_data.append({
            "id": str(camera_row.id),
            "name": camera_row.name,
            "code": camera_row.code,
            "state": source.recorder_state if source else "NOT CONFIGURED",
            "coverage": coverage_by_camera[camera_row.id],
        })
    first = day.replace(day=1)
    previous_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    workspace_data = {
        "timezone": selected_site.timezone,
        "dayStart": day_start.isoformat().replace("+00:00", "Z"),
        "dayEnd": day_end.isoformat().replace("+00:00", "Z"),
        "playhead": playhead.isoformat().replace("+00:00", "Z"),
        "zoom": zoom,
        "event": str(selected_event.id) if selected_event else None,
        "advancedEnd": advanced_end.isoformat().replace("+00:00", "Z") if advanced_end else None,
        "cameras": camera_data,
        "markers": markers,
    }
    context = _context(request, db, user)
    context.update({
        "sites": sites,
        "selected_site": selected_site,
        "cameras": cameras,
        "selected_camera_ids": {str(item) for item in selected_camera_ids},
        "selected_cameras": selected_cameras,
        "camera_states": camera_states,
        "selected_date": day.isoformat(),
        "selected_date_display": day.strftime("%d %b %Y"),
        "calendar_title": day.strftime("%B %Y"),
        "calendar_weeks": service.calendar(selected_site, day),
        "previous_month": previous_month.isoformat(),
        "next_month": next_month.isoformat(),
        "markers": markers,
        "workspace_data": workspace_data,
        "zoom": zoom,
        "advanced_start": playhead.astimezone(zone).replace(tzinfo=None).isoformat(timespec="seconds") if start else "",
        "advanced_end": advanced_end.astimezone(zone).replace(tzinfo=None).isoformat(timespec="seconds") if advanced_end else "",
        "max_playback_minutes": settings.media_max_playback_minutes,
    })
    return templates.TemplateResponse(request=request, name="media/playback.html", context=context)


@router.get("/cameras/{camera_id}",response_class=HTMLResponse)
def camera_recordings(camera_id:UUID,request:Request,start:str="",end:str="",user:User=Depends(require_page_permission("media.view")),db:Session=Depends(get_db)):
    camera=db.get(Camera,camera_id)
    if camera is None: raise HTTPException(404,"Camera not found")
    params = [f"camera={camera_id}"]
    if start:
        params.append(f"start={quote(start, safe='')}")
    if end:
        params.append(f"end={quote(end, safe='')}")
    return RedirectResponse(f"/media/playback?{'&'.join(params)}", status_code=302)


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
    event_at=event.captured_at or event.received_at
    clip_duration=max(0.0,(ref.clip_end_at-ref.clip_start_at).total_seconds())
    recognition_offset=max(0.0,min(clip_duration,(ref.media_anchor_at-ref.clip_start_at).total_seconds()))
    timezone_name=event.site.timezone
    context=_context(request,db,user); context.update({
        "event":event,"reference":ref,"availability":availability,"playback_mode":mode,"holds":holds,"exports":exports,
        "event_time_display":_display_time(event_at,timezone_name),
        "event_time_technical":_display_time(event_at,timezone_name,milliseconds=True),
        "anchor_time_display":_display_time(ref.media_anchor_at,timezone_name,milliseconds=True),
        "clip_start_display":_display_time(ref.clip_start_at,timezone_name,milliseconds=True),
        "clip_end_display":_display_time(ref.clip_end_at,timezone_name,milliseconds=True),
        "clip_duration":clip_duration,
        "clip_duration_display":_display_duration(clip_duration),
        "recognition_offset":recognition_offset,
        "recognition_percent":(recognition_offset/clip_duration*100) if clip_duration else 0,
        "pre_event_seconds":recognition_offset,
        "post_event_seconds":max(0.0,clip_duration-recognition_offset),
        "active_hold_count":sum(hold.released_at is None for hold in holds),
    })
    return templates.TemplateResponse(request=request,name="media/event.html",context=context)


@router.get("/events/{event_id}/stream")
def event_video_stream(event_id:UUID,_user:User=Depends(require_permission("media.view")),db:Session=Depends(get_db)):
    ref,availability,_mode=MediaPlaybackService(db).event_playback(event_id)
    if availability.status not in {"AVAILABLE","PARTIAL"}: raise HTTPException(404,f"Event video is {availability.status.lower().replace('_',' ')}")
    try:
        artifact=EventPlaybackArtifactService(db).prepare(
            event_id=event_id,
            camera_id=ref.camera_id,
            start_at=ref.clip_start_at,
            end_at=ref.clip_end_at,
            availability=availability,
        )
    except PlaybackArtifactError as error:
        raise HTTPException(503,str(error)) from error
    return FileResponse(
        artifact,
        media_type="video/mp4",
        headers={
            "Accept-Ranges":"bytes",
            "Cache-Control":"private, max-age=60",
            "X-Content-Type-Options":"nosniff",
        },
        content_disposition_type="inline",
    )


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
