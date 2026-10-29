---
title: "fix: Gachibowli C3 history download fails silently and live-log byte 15 is read as Out"
type: fix
date: 2026-10-29
depth: standard
---

# fix: Gachibowli C3 history download fails silently and live-log byte 15 is read as Out

## Summary

Stop treating the C3 live-log in/out byte 15 as Out, capture the panel's real byte, make history-download failures loud and back off, get the download finishing again (read-only), and prove the existing repair then fixes the past week. Built on `feat/backup-fetch-restore`, which already labels guessed punches and gives admins the Fix punches page.

---

## Problem Frame

Production evidence from 2026-10-29:

- **The history download fails every cycle.** The panel (`10.10.16.6:4370`) accepts the session. `read_c3_table("transaction")` then gets `ConnectionResetError` at about 57 s while reading the prepared buffer in chunks (`essl_hub/collectors.py`, the 0x0E loop).
- **Live-log rows are never confirmed.** Since 10-20 there are 613 `realtime_fallback` rows and 1 history row. No byte-15 row has been repaired.
- **The failure is silent.** `collect_c3` swallows the error, falls back to the live log and logs nothing. The device shows `connected / TCP realtime` with no error.
- **The collector retries the failing download every cycle.** `_C3_HISTORY_AT` is set only on success, so each 60 s cycle spends about a minute on a download that cannot finish.
- **Byte 15 means nothing on this panel.** Employee NP0009 labelled three taps: 10-28 17:51 arrival, 10-29 00:15 leaving and 10-29 17:10 arrival. All three read byte 15. `_c3_trusted_direction` maps 15 to exit (KTD4 in `collectors.py`), so every arrival with byte 15 shows Out.
- **Frappe gets the guess.** `frappe_pending_punches` holds byte-15 rows for `FRAPPE_GUESS_HOLD` (24 h), waiting for history. Because history never confirms, the guessed log type is sent after the hold.

- **History is the trustworthy source; the live log is not.** Gachibowli has one reader outside the door (tap to enter) and one inside (tap to leave), and the user tapped outside to arrive and inside to leave. The history byte follows the reader, so it gives the true direction, and directions were right while history worked. The live-log byte does not follow the reader. Outside-reader arrivals read 15 (17:51, 17:10) and 0 (19:55), and the inside-reader leaving (00:15) read 15.

Before about 10-21 the 6-hourly history read overwrote the live guesses, so wrong labels were short-lived. The README records a full pull of about 30,000 events taking 80 s on this firmware. Since then the download is reset at about 57 s. The cause, whether table growth, WAN round trips or the age of the session, is for the U4 probe to establish.

---

## Requirements

- R1. A live-log row with in/out byte 15 carries no trusted direction. The hub infers it from neighbouring punches, and the day view labels it unconfirmed until history fills it.
- R2. Rows already stored with a byte-15 guess lose that guess, without touching rows that history has filled.
- R3. The collector records the panel's actual live-log in/out byte, not only the library's fallback value.
- R4. A failed history download is logged with its error and elapsed time. It is shown on the Devices page with "failing since", and retried on a backoff instead of every cycle.
- R5. The history download completes again against the production panel. The hub stays read-only towards the device: no settings writes and no log clearing.
- R6. Once a history read succeeds, the existing repair confirms past live-log rows. Punches already sent to Frappe with a guessed direction are listed for HR or an admin to fix.

---

## Scope Boundaries

- **No device writes.** The hub never clears panel logs or changes reader or door settings. Clearing old transactions in ZKAccess is the user's manual option, documented only.
- **No change to the X990 path**, the alternation rules in `hours.py`, or the quick-swipe windows.
- **Byte 0 stays trusted as entry for now.** One confirmed correct arrival (10-28 19:55) reads 0. A wrong live byte 0 is still overridden by history (`fill_punch_directions` replaces live guesses that history contradicts). Revisiting byte 0 waits for the raw byte capture.
- **No automatic Frappe re-send.** Frappe keeps the log type it was first sent; this plan lists those punches only.

### Deferred to Follow-Up Work

- **Raw-byte direction mapping.** Mapping directions from the raw byte (R3 data) once a week of samples is paired with known arrivals and leavings.
- **"Only new records" download.** An equality filter on GETDATA, if the U4 probe shows speed-up alone is not enough. Its wire encoding is undocumented. The PullSDK `NewRecord` option is out: it moves a read pointer on the panel, which breaks R5.

---

## Key Technical Decisions

- **15 means unknown, not exit.** The 10-26 check counted history bytes (0 and 1 only) and inferred that live 15 decodes from history 1. It never compared live and history row by row. The user-labelled taps disprove the inference: an outside-reader arrival reads 15. Unknown lets `_resolve_directions` infer from neighbours. For the user's 10-28 taps (17:51 15, 19:53 15, 19:55 0, 20:07 0, 20:22 0) that yields In, Out, In, In, In. The first three match their account.
- **Frappe gets only `trusted_direction`.** Inferred directions show on the day view but are never sent. An unconfirmed row goes after the hold without `log_type`, as X990 rows do.
- **Clear stored guesses with a new migration (v11).** Rows are `realtime_fallback` with `raw_direction_byte = 15` and `trusted_direction` set. The migration nulls `trusted_direction` only, which is collector metadata like `fill_punch_directions`, not a punch edit. A read-time override would leave Frappe and the API on the stale value. v10 is applied in prod and is not edited.
- **Capture the raw byte without changing `raw_direction_byte`.** `raw_direction_byte = 15` is the sentinel the Frappe hold and `fill_punch_directions` key on. The real byte goes into `raw_json` as `in_out_raw`, read from the 16-byte binary record (byte 11) or the key/value `inoutstatus`. It is evidence only and decides nothing.
- **History status lives in the collector status file, not the schema.** `collector-status.json` is already written every cycle and read by the web (`routes/common.py`). A per-device `c3_history` entry holds `last_ok_at`, `failing_since`, `last_error` and `next_try_at`. It resets on restart, which is acceptable because a restart retries immediately.
- **Back off after a failure.** The next try waits `ESSL_C3_HISTORY_RETRY_MINUTES` (default 30), so the panel is not tied up for about a minute of every cycle. Success keeps the 6 h schedule.
- **Probe first for the download fix.** The cause of the reset at about 57 s is unknown. A read-only probe runs every capability test in one window with the collector stopped, and the fix picks the first strategy the numbers support (see U4).

---

## Open Questions

- **Does the panel honour read lengths larger than its announced chunk, or pipelined 0x0E requests?** Resolved by the U4 probe in production.
- **Is byte 0 always a true entry?** Answered by the U2 raw byte data plus history once U4 works.

---

## Implementation Units

### U1. Live-log byte 15 carries no direction

**Goal:** Byte 15 becomes unknown on new and stored rows, and the day view labels it accordingly.

**Requirements:** R1, R2

**Dependencies:** none

**Files:**
- Modify: `essl_hub/collectors.py` (`_c3_trusted_direction`, retire `C3_REALTIME_15_EXIT_EVENT_TYPES`)
- Modify: `essl_hub/db.py` (`SCHEMA_VERSION = 11`, `MIGRATIONS[11]`)
- Modify: `essl_hub/day_story.py` (`is_unconfirmed_guess`, `punch_basis` text)
- Modify: `essl_hub/punch_alerts.py` (`is_unconfirmed_c3_guess`, if it keys on a stored direction)
- Modify: `essl_hub/routes/fixes.py`, `essl_hub/routes/fix_views.py`, `essl_hub/routes/fix_review.py` (flip and label a direction-less C3 row using the inferred direction)
- Modify: `README.md` (C3 directions section, KTD4 note)
- Test: `tests/test_collectors.py`, `tests/test_db.py`, `tests/test_day_story.py`, `tests/test_hours_breaks.py`, `tests/test_punch_alerts.py`, `tests/test_web.py`

**Approach:**
- `_c3_trusted_direction` returns entry for 0, exit for 3, and None otherwise, whatever the event type.
- The migration runs `UPDATE punches SET trusted_direction = NULL WHERE json_extract(raw_json,'$.source') = 'realtime_fallback' AND raw_direction_byte = 15 AND device_id IN (SELECT id FROM devices WHERE protocol = 'c3-pullsdk')`. The protocol value is a literal because migrations run without bound parameters. Rows filled by history already have byte 0/1 and are not matched. The migration also stores its cutover time in `settings` (`c3_live15_cutover_at`) for the U5 report.
- Fix requests and direct fixes on a direction-less C3 row offer and flip the direction the day view inferred, so approval does not fail with "needs a direction to flip to".
- Frappe hold logic is unchanged. After the hold, a direction-less row is sent without `log_type`, as for the X990.
- New basis text: "The live log gave no direction; counted as <In/Out> from the swipes around it; the device history confirms it later". Raw facts still show byte 15.

**Patterns to follow:** the v10 data migration in `essl_hub/db.py`; existing `test_realtime_event_25_with_15_has_no_direction_and_dedupes_with_history`.

**Test scenarios:**
- Happy path: realtime event 0 with byte 15 → `trusted_direction` None. Event 0 with byte 0 → entry. Byte 3 → exit.
- Edge: event 20 with byte 15 → None (was exit). 24/25 with byte 15 → None, as before. Existing tests asserting the old rule (`test_c3_realtime_exit_counts_as_out` and the "Guessed Out" text in `tests/test_web.py`) are updated.
- Integration: an employee flips a nulled byte-15 row the day view shows as In, and HR approves it → a direction correction to Out is stored.
- Integration: the sample day's `compute_day` result shows its segments, anomalies and effective minutes. Consecutive byte-0 entries (20:07, 20:22) stay unpaired, and that limitation is recorded in System-Wide Impact.
- Migration: a v10 database with three C3 rows (realtime byte 15 exit, realtime byte 1 exit already filled, history byte 1) → only the first loses its direction. X990 rows are untouched. Running the migration again changes nothing.
- Integration: the user's 10-28 sequence (15, 15, 0, 0, 0) scores In, Out (inferred), In, In, In. Rows 1–2 show the unconfirmed basis text.
- Integration: a history read carrying byte 0 for a nulled row → `fill_punch_directions` sets entry and the byte becomes 0.

**Verification:** No code path maps 15 to a direction. The v10→v11 upgrade test passes, and the day story for the sample day matches the user's account.

---

### U2. Record the panel's raw live-log in/out byte

**Goal:** Every realtime row stores the byte the panel actually sent.

**Requirements:** R3

**Dependencies:** none

**Files:**
- Modify: `essl_hub/collectors.py` (realtime loop in `collect_c3`)
- Test: `tests/test_collectors.py`

**Approach:**
- Read the realtime frame through a helper that rebuilds `C3.get_rt_log` on `panel._send_receive` and `panel._rtlog_command`. The helper keeps the binary-to-key/value switch and `_update_inout_status`, and holds each 16-byte record (or key/value pairs) next to the parsed `rtlog` record. `raw["in_out_raw"]` takes byte 11, or the `inoutstatus` value in key/value mode.
- Parsing, identity and fingerprints are unchanged, so dedupe with history still holds.
- If the raw frame is unavailable, `in_out_raw` is omitted and nothing fails.

**Patterns to follow:** `read_c3_table` / `_read_c3_frame` (the hub already speaks the frame protocol directly); `c3/rtlog.py` record layout.

**Test scenarios:**
- Happy path: a binary frame with byte 11 = 1 → row has `in_out_raw` 1, `raw_direction_byte` 15 and no direction.
- Happy path: byte 11 = 0 → `in_out_raw` 0, `raw_direction_byte` 0, entry (the library decodes 0 itself; `in_out_raw` changes nothing).
- Edge: key/value mode `inoutstatus=2` → `in_out_raw` 2.
- Edge: a frame length that is not a multiple of 16 → library behaviour is unchanged and the key is omitted.
- Integration: the fingerprint of a realtime row with `in_out_raw` equals its history twin's fingerprint.

**Verification:** New realtime rows in a test run carry `in_out_raw`, and fingerprints are unchanged.

---

### U3. History failures are logged, shown and backed off

**Goal:** A failing history download is visible and stops tying up the panel every cycle.

**Requirements:** R4

**Dependencies:** none

**Files:**
- Modify: `essl_hub/collectors.py` (`collect_c3`, `_c3_history_due`, history state)
- Modify: `essl_hub/collector_main.py` (`collector_status` adds `c3_history`)
- Modify: `essl_hub/routes/common.py`, `templates/devices.html` (warning line per C3 device)
- Modify: `essl_hub/config.py`, `docker-compose.yml`, `README.md` (`ESSL_C3_HISTORY_RETRY_MINUTES`)
- Test: `tests/test_collectors.py`, `tests/test_collector_main.py`, `tests/test_web.py`

**Approach:**
- Keep per-device history state: `last_ok_at`, `failing_since` (first failure of the current run), `last_error`, `next_try_at`. Wall-clock times are kept for the status file and monotonic times for scheduling.
- On failure, log a warning with the error and elapsed seconds, then set `next_try_at = now + retry minutes`. On success, clear the failure fields and use the 6 h schedule.
- `_c3_history_due` honours `next_try_at`. `ESSL_C3_MODE=history` still raises, as today.
- The Devices page shows "History download failing since <time>: <error>; live-log directions are unconfirmed" when `failing_since` is set, or "History last read <time>" otherwise. A missing or old status file shows nothing new.

**Patterns to follow:** `collector_status` / `write_status`; how `routes/common.py` reads `collector-status.json`; `_logger.warning` style in `collector_main.py`.

**Test scenarios:**
- Happy path: history succeeds → state shows `last_ok_at`, no failure, and the next try is after 6 h.
- Error: history raises `ConnectionResetError` → one warning logged with the error text, `failing_since` set, the realtime fallback still returns events, and the next cycle within 30 min skips history.
- Edge: a second failure keeps the original `failing_since`. A success after failures clears it.
- Integration: a status file with a failing entry → the Devices page renders the warning for that device only, and an admin sees it.
- Edge: no status file → the Devices page renders as before.

**Verification:** A forced failure in tests produces a log line, a status entry and a page warning, and the next cycle does not attempt history.

---

### U4. Make the history download complete (read-only)

**Goal:** `read_c3_table("transaction")` finishes against the production panel.

**Requirements:** R5

**Dependencies:** U3 (so failures stay visible while iterating)

**Execution note:** Probe production first. Pick the strategy from measured numbers, not guesses.

**Files:**
- Create: `scripts/c3_history_probe.py` (read-only measurement CLI)
- Modify: `essl_hub/collectors.py` (`read_c3_table`, `C3_TABLE_TIMEOUT_SECONDS`)
- Modify: `README.md` (probe usage and the manual panel-log option)
- Test: `tests/test_collectors.py` (fake socket covering the chunk loop), `tests/test_c3_history_probe.py`

**Approach:**
- The probe connects with the device's DB settings and asks the operator to stop the collector first. It reads only and makes one attempt per mode, stopping at the first panel reset. The modes run in order in one window:
  - `--measure` replays the collector's session (connect, device params, user table, then transaction). It reports total size, announced chunk, CRC, per-request round-trip time and bytes per second, logs progress every 5 s, and records the offset when the read fails. Elapsed time is measured from connect and from GETDATA.
  - `--fresh-session` is the same download on a new connection with no user-table read first, which tests whether the age of the session matters.
  - `--read-length N` sends one 0x0E request longer than the announced chunk, capped at 65,531 bytes, the frame maximum minus the offset.
  - `--pipeline N` keeps N 0x0E requests in flight.
  Each mode reports whether the replies were valid and the throughput it got.
- Decision rule: take the first strategy the probe shows finishing the download.
  1. **Dedicated session.** Download the transaction table on its own connection, if the session age is the limit.
  2. **Larger read lengths.** Use the capped length, if the panel accepts it and round trips dominate.
  3. **Pipelining.** Keep N requests in flight, if the panel answers queued requests in order.
  4. **None works.** Stop and give the user the numbers. The remaining options are the deferred filter research, or the user clearing old panel logs in ZKAccess.
- Each strategy keeps the CRC check and falls back to the current loop on an unexpected reply.

**Patterns to follow:** the existing `read_c3_table` framing and CRC check, and the read-only probe run on 2026-10-29.

**Test scenarios:**
- Happy path: fake panel with total 10 chunks → the download completes in fewer round trips under the chosen strategy, and the parsed rows equal those from the current loop.
- Error: reply offset mismatch or empty chunk → `ConnectionError`, as today.
- Error: CRC mismatch → `ConnectionError`.
- Edge: total smaller than one chunk → a single request.
- Probe: a fake socket that resets mid-read → the probe prints the offset, total and elapsed time, and exits non-zero.

**Verification:** The production collector records `TCP history`, and the Devices page shows "History last read". The 10-28 and 10-29 arrivals for NP0009 then read entry.

---

### U5. Confirm the backlog repair and list punches Frappe got wrong

**Goal:** Prove the past week self-repairs on the first good history read, and give HR a list of punches already sent to Frappe on a guess.

**Requirements:** R6

**Dependencies:** U1 (the repair test); U4 for the repair to happen in production

**Files:**
- Create: `scripts/c3_guessed_frappe_report.py`
- Modify: `README.md`
- Test: `tests/test_collector_main.py`, `tests/test_c3_guessed_frappe_report.py`

**Approach:**
- Test that one history batch containing many older live-log fingerprints repairs all of them through `collect_once` and `fill_punch_directions`.
- The report reads the database read-only and lists C3 `realtime_fallback` punches with a `frappe_sync_log` row of status `synced`.
- The hub never stored the log type it sent, so the report rebuilds it:
  - **Sent as OUT:** synced before the v11 cutover (`c3_live15_cutover_at`) with byte 15 and event 0–3 or 20, under the retired rule.
  - **Possibly sent as OUT:** byte 0/1 now (filled by history), synced at or after the event plus `FRAPPE_GUESS_HOLD`.
- Columns: employee, day, time, rebuilt log type, current direction, `checkin_name`. Output is CSV to stdout, so HR can fix the check-in in Frappe or with Fix punches.

**Patterns to follow:** `scripts/remap_c3_ids.py` (read-only by default, `ESSL_DB_PATH`); `frappe_sync_log` columns.

**Test scenarios:**
- Integration: 20 nulled live-log rows across 7 days plus a history batch with their bytes → all 20 filled, nothing inserted, and directions match the bytes.
- Happy path: a byte-15 row synced before the cutover is listed as OUT. A history-filled byte-0 row synced after its hold is listed as possibly OUT. An unsynced row and a history row are not listed.
- Edge: the empty case prints only the header.

**Verification:** Both tests pass, and the report runs against a fetched production copy (`backup fetch`) without writing.

---

## System-Wide Impact

- **Hours, alerts and exports:** byte-15 punches move from "Out" to an inferred direction, so some past days rescore after the migration. Most should get closer to the truth. Days that HR already corrected keep their overlays.
- **Frappe:** after the hold, unconfirmed punches are sent without `log_type` instead of `OUT`. Rows sent before the fix are only reported. A Gachibowli day can mix typed and untyped check-ins, and a Shift Type set to "Strictly based on Log Type" ignores untyped ones. History working again (U4) is what removes the mix.
- **Consecutive live byte-0 entries** (for example 20:07 and 20:22) stay unpaired until history confirms them.
- **Panel load:** U3's backoff stops a full download attempt every minute.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| No download strategy works on this panel | U4 step 4 stops with numbers. Live-log rows stay unconfirmed but are no longer wrong-by-default (U1), and admins can fix them. |
| Byte 0 also turns out unreliable | U2 data shows it, and history overrides live guesses it contradicts. Scoped as a follow-up; byte 0 stays trusted only on the current evidence. |
| Out-of-spec reads upset a door controller | The probe runs off-hours with someone on site, makes one attempt per mode and stops at the first reset or reboot. |
| Migration nulls rows HR already reasoned about | Overlays win over stored directions, so corrections are untouched. |
| Probe competes with the collector for the panel session | The probe asks the operator to stop the collector, as the 10-29 probe did. |

---

## Sources & Research

- **Production evidence (2026-10-29):**
  - The device row, source counts and NP0009 rows for 10-28 and 10-29.
  - The probe run: connected, then reset at 57.5 s inside `read_c3_table`, at the chunk-read line.
- **User labels:** 10-28 17:51 arrival, 10-29 00:15 leaving, 10-29 17:10 arrival. Gachibowli has one reader outside (enter) and one inside (leave), and the user tapped outside to arrive and inside to leave.
- **README baseline:** a full history pull of about 30,000 events took 80 s on AC Ver 18.1.2.
- **Code:**
  - `essl_hub/collectors.py`: `collect_c3`, `read_c3_table`, `_c3_trusted_direction`, `_c3_history_due`.
  - `essl_hub/collector_main.py`: `collect_once`, `collector_status`.
  - `essl_hub/db.py`: `fill_punch_directions`, `frappe_pending_punches`, `FRAPPE_GUESS_HOLD`.
- **Vendored library:** `c3/consts.py` `InOutDirection` decodes only 0, 2, 3 and 15. `c3/core.py` `get_device_data` sends two trailing GETDATA bytes the authors label "purpose or meaning unknown".
- **External:**
  - PullSDK `GetDeviceData`: the `Options` value `NewRecord` downloads only new event records, and `Filter` supports `field=value` conditions (equality only, per pyzkaccess). The wire encoding is undocumented, so this is deferred.
  - [ZKTeco Pull SDK User Guide](https://www.scribd.com/document/442591279/PullSDK-User-Guide-EN-V2-0-201201-1-doc), [PyZKAccess](https://bdragon300.github.io/pyzkaccess/), [vwout/zkaccess-c3-py](https://github.com/vwout/zkaccess-c3-py).
- **Prior plan:** `docs/plans/2026-10-26-001-fix-attendance-history-and-quick-swipes-plan.md`, the pre-implementation check 2 that this plan overturns.
