# M21 — Unified Event, Health & Alert Management

## Purpose

M21 adds an operational projection over the authoritative ANPR Central domain models. It does not replace recognition, access decisions, parking sessions, visitor records, barrier actions, audit logs, or media records.

The four operator capabilities are:

1. Event Center — compact, searchable activity.
2. Attention — events classified for human review.
3. Active Alerts — persistent unresolved operational conditions.
4. System Health — current component/capability state.

An event is a meaningful occurrence. An alert is an unresolved operational condition. Normal recognition is an event and is never an alert.

## Event model

Categories are `TRAFFIC_ANPR`, `ACCESS_SECURITY`, `ADMIN_BUSINESS`, `OPERATOR_ACTION`, and `SYSTEM_HEALTH`. Severities are limited to `INFO`, `WARNING`, and `CRITICAL`. Severity and `attention_required` are independent.

`app/services/event_policy_service.py` is the single classification registry. `EventService.record()` applies this policy and enforces idempotency with `(source_type, source_id, event_type)`. `UnifiedEvent` stores only concise searchable context. Raw MQTT payloads, snapshots, credentials, and complete domain objects are not copied.

Implemented projection includes:

- live and recovered ANPR recognition;
- whitelist/blacklist matches and access decisions;
- parking session open/close;
- manual barrier request, camera acknowledgement, and failure;
- plate correction and session reconciliation;
- visitor registration/cancellation;
- selected audited access and visitor administration;
- component fault and recovery transitions;
- alert acknowledgement.

The monitor processes a bounded recent window. Historical records older than the first M21 monitor run remain available in their authoritative modules and are not automatically bulk backfilled.

## Attention policy

Normal recognition, whitelist match, and ordinary vehicle/session activity are informational. Blacklist/watchlist matches, access review/deny, session reconciliation, manual barrier release, and system faults require attention. A blacklist match remains a security event and does not create a persistent system-fault alert.

## Alert lifecycle

`OperationalAlert` supports:

- `ACTIVE`: condition exists and has not been acknowledged;
- `ACKNOWLEDGED`: condition still exists and a human has recorded review;
- `RESOLVED`: monitoring observed recovery.

The fingerprint is derived from alert type, site, component type, and component ID. Repeated observations update one condition instead of inserting a poll-by-poll duplicate. Acknowledgement never resolves or changes component health. Recovery automatically resolves both active and acknowledged alerts. Alert acknowledgement is audited.

Operators cannot manually mark a component healthy.

## Health states and claims

Current states are `HEALTHY`, `DEGRADED`, `FAULT`, `UNKNOWN`, `UNVERIFIED`, and `DISABLED`.

- **VERIFIED**: the monitored signal directly supports the claim, such as a current camera heartbeat, current completed recording segment, filesystem availability, or configured physical feedback.
- **DERIVED**: the state is calculated from authoritative Central records, such as access synchronization rollup or barrier command-path history.
- **UNVERIFIED**: a command path/camera acknowledgement exists but downstream physical/device feedback does not.
- **NOT CONFIGURED**: the optional capability is deliberately absent or disabled.

`DISABLED` and an intentional `UNVERIFIED` limitation do not make a site faulty.

## Truthful physical semantics

An RA5 MQTT reply with code 200 verifies only that the camera acknowledged the command. It does not prove:

- barrier movement or position;
- LED output;
- speaker/TTS output;
- traffic-light output;
- downstream RS-485 receipt.

Barrier physical state is verified only when the configured feedback mode supplies a current observation. With feedback mode `NONE`, feedback is `DISABLED`/not configured, not a fault. RA5-forwarded peripherals remain `UNVERIFIED` unless future hardware feedback establishes a stronger claim.

## Correlation

Recognition-driven events use the `RecognitionEvent.id` as their correlation identity. Existing explicit foreign-key relationships propagate that identity to access decisions, barrier actions, corrections, and parking-session activity. Events are never correlated merely because a plate, camera, or timestamp is similar.

## Process architecture

The web application remains independent from monitoring:

```text
FastAPI web/API/UI       python -m uvicorn app.main:app
Recorder                 python -m app.recorder
Monitoring worker        python -m app.monitor
```

Start the monitor from the repository directory with the same `ANPR_` environment:

```powershell
python -m app.monitor
```

The interval defaults to 30 seconds and is configured with `ANPR_MONITOR_INTERVAL_SECONDS`. Other centralized controls include `ANPR_MONITOR_PROJECTION_BATCH_SIZE`, `ANPR_MONITOR_DATABASE_SLOW_MS`, `ANPR_MONITOR_DATABASE_FAILURE_DEBOUNCE_COUNT`, and `ANPR_MONITOR_NETWORK_TIMEOUT_SECONDS`. Database latency uses three samples and requires at least two slow samples; connection failure is retained in worker memory only after the configured consecutive-failure threshold and is recorded/resolved when PostgreSQL becomes writable again.

The monitor may read state, project events, update health, and open/update/resolve alerts. It never makes an access decision, mutates a recognition, opens a barrier, sends a peripheral command, changes an access list, restarts a device, or performs automatic remediation. Recognition and barrier operation remain functional if the worker stops.

## Permissions

- `monitoring.view`: Event Center, alerts, and System Health.
- `alerts.acknowledge`: acknowledge an active condition with a note.
- `monitoring.manage`: reserved for future monitoring administration.

These permissions do not imply `barrier.operate`, camera management, or access-list management.

## Operator procedure

1. Keep `python -m app.monitor` running under the same service account/environment as ANPR Central.
2. Open `/events`; Attention is the default view. Use All Events for normal ANPR traffic.
3. Open Active Alerts, investigate through the linked component/source, and acknowledge with a concise operational note.
4. Do not interpret acknowledgement as recovery. Wait for monitoring to resolve the condition.
5. Open `/system-health` to distinguish core service, camera, media/storage, access, lane-equipment, and host states.
6. Use authoritative linked pages for evidence, barrier actions, parking sessions, visitors, and lane operations.

## Hardware validation limitations

Software tests verify policy, idempotency, correlation rules, health transitions, alert deduplication/lifecycle, permissions, CSRF, bounded APIs, and non-actuating recovered events. Physical barrier position and downstream LED, speaker/TTS, traffic-light, and RS-485 behavior require manual on-site validation. The UI deliberately reports those unverified limitations rather than inferring success from a camera acknowledgement.
