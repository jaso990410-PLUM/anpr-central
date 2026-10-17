# M22 Historical Analytics & Reporting

## Scope

M22 provides period-based, site-scoped historical aggregation and CSV export. It does not replace Lane Operations, Recognition History, Stay History, Barrier Actions, Event Center, System Health, or Media Playback. Those modules remain the authoritative record-level investigation interfaces.

Reporting is observational. Viewing a report never publishes MQTT, operates a barrier, changes access authority, modifies recognition evidence, reconciles a session, acknowledges an alert, or controls a recorder. CSV export creates only a security audit entry describing the export request.

## Authoritative sources

- `ParkingSession`: Entry, Exit, occupancy, peak occupancy, and completed-stay duration.
- `RecognitionEvent`: separate LIVE and OFFLINE_RECOVERY recognition activity.
- `AccessDecision`: access outcome, denial reason, access status, and access-profile aggregation.
- `BarrierAction`: automatic/manual command activity and camera-delivery outcome.
- `RecognitionPlateCorrection` and `ParkingReconciliation`: operator interpretation and historical repair counts.
- `OfflineRecoveryRun`: recovery runs, imported records, duplicates, invalid records, and failures.
- `UnifiedEvent` and `OperationalAlert`: observed health transitions, incidents, alerts, and persisted recording gaps.
- `RecordingSegment`: segment-backed media coverage. Recorder process state is not coverage evidence.

## Metric definitions

### Entry

One authoritative ParkingSession opening whose `entered_at` falls inside the selected period. It is not a recognition count, ALLOW count, or barrier-command count. Duplicate recognitions do not add entries unless the existing session domain created another authoritative session.

### Exit

One authoritative ParkingSession closure whose `exited_at` falls inside the selected period. An Exit-camera recognition without a session closure is not an Exit.

### Current occupancy

The number of open authoritative ParkingSessions at the reporting snapshot. It is never calculated as today's Entry minus today's Exit.

### Historical occupancy

At time T, a session contributes when `entered_at <= T` and `exited_at > T` or it remained open at T. Sessions beginning before the reporting boundary are therefore included at the first observation.

### Peak occupancy

The highest reconstructed occupancy reached during the selected period, including the initial boundary occupancy and each authoritative session transition.

### Average stay

The arithmetic mean of non-negative durations for completed sessions whose closure is in the selected period. Open sessions are excluded. With no completed sessions the value is an em dash, not zero.

### Stay-duration distribution

Completed stays are grouped centrally into: under 30 minutes, 30–60 minutes, 1–2 hours, 2–4 hours, 4–8 hours, and 8+ hours.

### Peak entry period

The site-local reporting bucket containing the most authoritative session openings. Ties resolve to the earliest bucket. It is not based on recognition volume.

### Recognition activity

Recognition activity counts camera events independently of vehicle movement. LIVE and OFFLINE_RECOVERY remain separate. A recovered record does not imply a live decision or physical action. Plate corrections are counted as operator interpretation records and do not mutate RecognitionEvent.

### Access decision and denied access

Decision totals come directly from AccessDecision. Denied access is the DENY state; reasons use persisted reason codes with display-only friendly labels. Profile use comes from the AccessListEntry/AccessProfile relationship recorded by the decision.

### Manual barrier release

A BarrierAction whose trigger source is `MANUAL_RELEASE`. A camera acknowledgement proves only that the camera accepted the command. It does not prove physical barrier movement.

### Observed incident and duration

An observed incident is a persisted M21 fault transition inside the selected period. Reports do not infer downtime from missing recognitions. Duration is shown only where persisted start and resolved/end evidence exists. Monitoring gaps prevent uptime, availability SLA, or MTBF claims.

### Recording gap and media coverage

Recording-gap counts use persisted M21 `RECORDING_GAP` events. Known duration is shown only when the stored event contains parseable start and end evidence. Media coverage merges overlapping COMPLETE/PARTIAL RecordingSegment intervals and measures coverage across their observed span. It does not interpret a running recorder as 100% coverage and does not fabricate storage history.

## Timezone and range semantics

All visible periods and buckets use the selected Site timezone. Today is the current site-local calendar day, not a rolling 24 hours. Yesterday is the previous local calendar day. Custom end dates are inclusive in the UI and normalized internally to an exclusive next-midnight boundary. Interactive ranges are limited to 366 days and automatically use hourly, daily, or monthly buckets.

## Lane filtering

Entry counts use the session's entry RecognitionEvent camera. Exit counts use the exit RecognitionEvent camera. Occupancy under a camera/lane filter follows the entry-camera cohort so that one consistent population is reconstructed through time.

## CSV export

CSV uses the same normalized query and analytics services as the GUI. Files are UTF-8 with a BOM for common Windows spreadsheet compatibility and contain human-readable aggregate rows, site-local bucket labels, selected site/range, and lane context. They exclude credentials, stream URIs, filesystem paths, secrets, and unnecessary database identifiers.

## Permissions

- `reports.view`: open report pages and see aggregate analytics.
- `reports.export`: download filtered CSV. This is independently enforced by the export endpoint.

The administrator role receives both permissions through the M22 data migration. Other roles may be granted either permission through the existing authorization model.

## Known limitations

- The application currently has global permissions rather than per-user site assignments. Site validation ensures the selected site/camera exists and belongs together; it cannot enforce a site assignment model that does not yet exist.
- Watchlist detection totals depend on persisted `WATCHLIST_MATCH` unified events.
- Recording-gap durations are unavailable for older gap records that do not contain parseable start/end evidence.
- Media coverage is limited to the observed span of persisted completed/partial segments; it is not an SLA or retention-completeness claim.
- Reports query authoritative operational tables directly. No warehouse, materialized view, or daily statistics table is introduced.
