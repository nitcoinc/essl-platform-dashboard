---
title: "fix: Show why each punch is In or Out, let admins fix punches with an audit trail, and pull R2 backups to a dev box"
type: fix
date: 2026-10-28
depth: deep
---

# fix: Show why each punch is In or Out, let admins fix punches with an audit trail, and pull R2 backups to a dev box

## Summary

Three separable parts, one plan, built on separate branches:

- **Direction you can verify.** Every punch on the day page says how its In or Out was decided (device byte, live-log guess, hub alternation, duplicate, or a correction). If production evidence shows the live-log guess is wrong, a guessed exit is also confirmed from device history within minutes instead of up to 6 hours.
- **Admin fixes with an audit trail.** An admin approves employee fix requests like HR, and can fix a punch directly with a required reason. Every correction records who, their role at the time, when, why and by which path. The record cannot be edited or deleted.
- **Production data on a dev box.** One command, run on the production host, writes a scrubbed, verified copy of an R2 snapshot to a folder you copy out. The existing restore gains a guarded "replace the live database" mode.

The hub still never writes a device setting. Nothing here sends a new command to a device.

---

## Problem Frame

The user reported In and Out flipped again after the X990 window fix (floor of 30 s, `dabc525`). Their `/day` list for 2026-10-28 showed `Out, Out, In, In, In`, the first four marked unpaired. Reading the code and the raw rows they pasted:

- **Not the quick-swipe window.** A direction-less swipe always scores In first, and alternation never produces two Outs in a row. These five rows have a stored direction, so the hub relayed one.
- **All five are Gachibowli C3 rows from the live-log fallback** (`raw_json` source `realtime_fallback`), door 1. The first two carry in/out byte 15 (`raw_state` `0 · 15`), which the collector treats as exit for swipe events. The last three carry byte 0 (entry).
- **Byte 15 is a decode fallback, not a reading.** The C3 library cannot decode an exit from the live log and returns 15. The history table later gives the real byte (0 or 1) and `fill_punch_directions` repairs the row. In auto mode that history read runs at collector start and then every 6 hours (`ESSL_C3_HISTORY_HOURS`). Rows 1 and 2 were still byte 15 when queried, so no repair had happened.
- **Two readings of the same rows.** The 2026-10-12 decision treated 15 as a real exit (13,958 byte-1 rows in history, none byte 15), so these could be genuine exit-side taps on a door with unpaired data. Or the guess is wrong for this door. The user insists every tap was an entry. Only a history read settles it.

Nothing on the page tells the user what the device said versus what the hub decided, which is why the data looked "opposite" with no way to check. Fixing a wrong punch is also limited to HR: an admin may decide a request only when no other HR user exists, and the stored correction records no actor.

Production data is hard to inspect and repair: snapshots live in R2, restore only works onto an empty data folder, and the dev box has no production rows.

## Requirements

**Direction**
- R1. The day view states, per punch, how its direction was decided, using the same result the scoring used. HR and admins also see the raw facts (device, door, reader, byte, source); employees and managers do not.
- R2. If production evidence shows live-log guesses are wrong, a guessed direction is labelled unconfirmed and is confirmed from device history on a bounded schedule (U2 is conditional on that evidence).
- R6. The X990 alternation and 30 s window floor are unchanged by this plan; any change to them is a separate plan.
- R7. The hub writes no device setting and sends no new device command.

**Admin fixes**
- R3. An admin decides fix requests like HR. Nobody decides their own request.
- R4. An admin can add, flip or ignore a punch directly, with a required reason and a before/after hours preview.
- R5. Every correction stores who, their name and role at that moment, when, why and the path (request or direct). Corrections made before this change show "before audit trail" where the actor is unknown. Audit columns are append-only.

**Backups**
- R8. An R2 snapshot downloads to a local folder: verified, device passwords, API keys and queued device requests removed, stamped as a fetched copy with a new database ID so no hub component can upload, sync or write to a device from it.
- R9. A snapshot restores over a live database after proof that nothing else has the database open, with the old files kept aside.

## Scope Boundaries

- **No file import or export of punches.** The user chose snapshot download and restore.
- **No device writes**, and no X990 state-key or auto-switch changes (the user does not want to change device state).
- **Restore is a host command**, not a web button.
- **`fetch` runs on the production host**, where the R2 keys already live. The dev box receives the scrubbed file and never holds R2 credentials.

### Deferred to Follow-Up Work

- Sending corrections to Frappe. Corrections are overlays; Frappe still receives the stored direction, so a corrected day can differ from Frappe.
- A review list of direct fixes for HR, if one is wanted later.
- A read-only report of recent raw state bytes per device, if the user later wants X990 state keys.

## Key Technical Decisions

- **Provenance comes from the scoring result.** The label is derived from `_resolve_directions` output (kept, alternated, inferred, collapsed) joined with the stored row facts, never from stored columns alone, so it cannot disagree with the pairing. Nothing is stored or backfilled.
- **A live-log guess stays scored as stored.** Dropping it would change hours on a theory. If the evidence shows it is wrong, U2 confirms it from history on a cooldown. The guess label applies only to swipe event types the collector actually guessed.
- **Overlay rows carry their correction.** `apply_corrections` returns the correction's id, actor name, role, time, reason and path with each overlaid row (latest correction wins when several target one punch), so the day view can name the fixer.
- **Rebuild `punch_corrections` in a new migration (v10).** `request_line_id` is `NOT NULL` today, so a direct admin fix has nowhere to live. SQLite cannot relax a constraint, so v10 creates the new table, copies rows with explicit ids (keeping `sqlite_sequence`), recreates the index after the swap, and does the backfill in plain SQL. New audit columns are nullable or defaulted, so v9 code can still run against a v10 file during a rollback. Applied migrations are not edited.
- **The audit record is append-only and self-contained.** Each row stores the actor's id, name and role at write time, so a later role change, archive or removal cannot rewrite history. Triggers block updates and deletes of audit columns. The database layer refuses a direct fix where the actor is the corrected employee; this is never taken from a form value.
- **The database layer re-derives authority.** The direct-fix method takes only the actor id, loads the actor, requires an active admin, computes the role itself, refuses the actor's own punches, rejects archived or unknown targets, and re-checks the day's punch-set hash at apply time.
- **A flip on an unconfirmed guess warns.** The direct-fix preview says history may still change that punch, and a fix shows "device later reported X" if history disagrees.
- **Fetched copies are inert.** `fetch` shares the download-and-verify helper with `restore`, then scrubs on a temp copy, vacuums, verifies, and only then writes into the output folder. The `fetched_copy` marker makes the collector, alerts, Frappe sync and `backup adopt` refuse to run on it.
- **Replace-restore proves exclusivity, snapshots itself, keeps the old file.** It takes an exclusive lock on the live database (short timeout, abort on failure), takes the pre-restore snapshot inside the command, then renames the live files aside in a recoverable order.

## Open Questions

- **Evidence gate for U2.** Force a history read on production (`docker compose restart collector`, wait a minute) and re-run the pasted query. Rows 1 and 2 turn byte 0: the guess was wrong, build U2 and open a follow-up on the byte-15 mapping. They turn byte 1: the device reported exit, drop U2; U1 and the admin fix are the answer. They stay 15: history is failing; read the collector log. Also unknown: whether history lists a swipe immediately or with a lag, which sets U2's cooldown.
- **Decided: admin self-fix is blocked.** An admin's own punches go through a fix request that HR or another admin decides, so no one approves their own hours.
- **Decided: direct-fix reach is any past day.** Employee reports stay limited to the last 7 days. Every direct fix is audited.
- **Consequence to know:** a site with one admin and no HR has no approver for the admin's own punches. Adding a second approver is an operational step, not code.

---

## Implementation Units

### U1. Direction provenance on the day view

**Goal:** Each punch shows how its direction was decided; HR and admins see the raw facts.

**Requirements:** R1, R6, R7

**Dependencies:** U3 for the fixer's name only; everything else stands alone.

**Files:**
- Modify: `essl_hub/db.py` (the overlaid punch read adds `door`, `reader` and `json_extract` of the source, never the whole `raw_json`)
- Modify: `essl_hub/day_story.py` (`RawPunch` gains a basis label, derived from the `DayResult` and the row)
- Modify: `essl_hub/access.py` (one predicate for who sees raw facts: HR and admin)
- Modify: `templates/_day_breaks.html`
- Test: `tests/test_day_story.py`, `tests/test_db.py`, `tests/test_web.py`, `tests/test_access.py`

**Approach:**
- Labels, in precedence order: corrected by a correction ("Fixed by <name>" once U3 lands, "Fixed" before); duplicate swipe, not counted; inferred between neighbours; device byte from history ("Device said Out"); unconfirmed live-log guess ("Guessed Out from the live log; history confirms it later"); device live byte 0 ("Device said In"); no stored direction ("Hub alternated").
- The raw-facts block renders only when the access predicate allows it, and the route passes those fields only then.
- The function takes the scored result, so it uses the window the day was actually scored with.

**Patterns to follow:** `punch_flags` and `CORRECTION_FLAGS` in `essl_hub/day_story.py`; the raw list in `templates/_day_breaks.html`.

**Test scenarios:**
- Happy path: C3 history row, byte 1 → "Device said Out", raw facts visible to HR and admin.
- Happy path: X990 row with no stored direction → "Hub alternated".
- Edge: realtime row byte 15 on a swipe event → "Guessed Out…"; the same row after a history repair (byte 1) → "Device said Out".
- Edge: a collapsed quick re-swipe → "duplicate swipe, not counted"; a direction-less swipe filled between neighbours → "inferred".
- Edge: door-denied byte-15 row (no stored direction) never gets the "Guessed" label.
- Error/permission: an employee and a manager viewing a day see the basis line but the response body contains no door, reader or byte.
- Integration: a day shaped like the reported one (`exit(15), exit(15), entry, entry, entry`) renders a label on all five punches.

**Verification:** the reported day renders a basis line on all five punches and no existing day-view assertion regresses.

---

### U2. Confirm guessed live-log exits on a cooldown (conditional)

**Goal:** A byte-15 live-log row is repaired from device history within minutes instead of up to 6 hours.

**Requirements:** R2, R7

**Dependencies:** the Open Question evidence (byte 0 result). Skip this unit if rows 1 and 2 turn byte 1.

**Execution note:** Add the failing test first: a stored byte-15 realtime row makes history due after the cooldown.

**Files:**
- Modify: `essl_hub/collectors.py` (history due check)
- Modify: `essl_hub/collector_main.py` (mark due after storing a byte-15 realtime row)
- Test: `tests/test_collectors.py`, `tests/test_collector_main.py`

**Approach:**
- The history-due check also returns true when an unconfirmed guess was stored since the last history read, but only after a minimum gap (start at 10 minutes) and with backoff after a failed read. The polling interval is 60 s, so "one per poll" is no bound; the cooldown is.
- Every real exit reads as byte 15 on the live log, so a stream of exits must not become a full download per poll. A cap per hour backs the cooldown.
- The read is the existing GETDATA table download; nothing new is sent to the device.

**Patterns to follow:** `_c3_history_due` and `_C3_HISTORY_AT` in `essl_hub/collectors.py`; the "R5 repair" block in `essl_hub/collector_main.py`.

**Test scenarios:**
- Happy path: a byte-15 row stored → history read once the cooldown has passed; the row then carries its real byte and direction.
- Edge: a stream of byte-15 rows across many polls → at most one forced read per cooldown and per hour cap.
- Edge: no byte-15 rows → the 6 h schedule is unchanged.
- Error path: the history read fails → backoff grows, the realtime path still runs, and the due mark persists.
- Edge: history does not yet list the swipe → the row stays 15 and is retried after the next cooldown, not every poll.
- Integration: after the repair, `fill_punch_directions` updates the row and the day view basis changes from "guessed" to "device said".

**Verification:** with the fake panel, a guessed exit is corrected after the cooldown and polls between do no full download.

---

### U3. Audit-ready corrections schema (migration v10)

**Goal:** A correction can exist without a fix request, and every correction records who made it, immutably.

**Requirements:** R5

**Dependencies:** none

**Execution note:** Characterize the current `punch_corrections` read paths (`_corrections_for`, `apply_corrections`) before the rebuild.

**Files:**
- Modify: `essl_hub/db.py` (`SCHEMA_VERSION` 10, `MIGRATIONS[10]`, overlay read joins actor)
- Modify: `essl_hub/hours.py` (`apply_corrections` returns the correction facts on each overlaid row)
- Modify: `essl_hub/db_fixes.py` (writes stamp the actor; new direct-correction method)
- Modify: `deploy/install.sh` (take the pre-update snapshot from the running old image before `docker compose build`; today the new image refuses to snapshot a database whose schema is behind it, so the safety net fails on exactly this deploy)
- Test: `tests/test_db.py`, `tests/test_hours.py`, `tests/test_migrate.py`, `tests/test_backup.py`

**Approach:**
- New columns: `created_by`, `created_by_name`, `created_by_role` (name and role captured at write time), `path` (`request` or `direct`), `reason`. No stored self flag.
- `request_line_id` becomes nullable and stays unique. Append-only triggers block updates and deletes of the audit columns; a future revert is a new row.
- Backfill in plain SQL: actor from the decided line (`created_by_name` "before audit trail" when the decider is gone), role NULL, `path` `request`, `reason` from the request's `decision_note` (not the requester's note).
- A direct write refuses an empty reason (same `RuleViolation` style as `FIX_NOTE_MAX_CHARS`).
- A restored v9 snapshot migrates when web or collector boots; `backup now` fails with "schema behind" until then (README note in U7).

**Patterns to follow:** migration v9 (`punch_archives`) for style; `decide_fix_lines` for writing corrections inside a transaction.

**Test scenarios:**
- Happy path: a v9 database with corrections migrates; ids, `sqlite_sequence` and overlay results are unchanged; the actor comes from the old decided line.
- Edge: a correction whose decider was deleted migrates with "before audit trail", still applies.
- Edge: updating or deleting an audit column is blocked by the trigger.
- Edge: v9 code inserting a correction into a v10 file (new columns omitted) still succeeds.
- Error path: a direct write with an empty or whitespace reason raises; nothing is written.
- Integration: a v9 snapshot restored through `backup restore` opens and migrates; hours for a corrected day are identical before and after.

**Verification:** hours for corrected days match before and after, and the deploy script snapshots before the build.

---

### U4. Admin decides fix requests like HR

**Goal:** An admin can approve or reject employee fix requests at any time, except their own.

**Requirements:** R3, R5

**Dependencies:** U3

**Files:**
- Modify: `essl_hub/access.py` (`can_approve_fix`)
- Modify: `essl_hub/routes/fix_review.py` (`_readonly_reason` wording, decision actor)
- Modify: `templates/fix_review_detail.html`
- Test: `tests/test_access.py`, `tests/test_web.py`

**Approach:**
- Admin may decide whenever the admin is not the requester. Remove the "no other HR exists" condition. `Database.decide_fix_lines` keeps its own requester check and now stamps the correction with the deciding actor (U3).
- `fix_approvers` (who is emailed) keeps HR first, falling back to admins.

**Patterns to follow:** the existing KTD6 rule and its tests in `tests/test_access.py`.

**Test scenarios:**
- Happy path: admin approves a request while an active HR user exists; the correction applies and records the admin as actor.
- Error path: admin cannot approve their own request; HR cannot either.
- Edge: employee and manager roles still cannot decide.
- Integration: after approval the day view names the admin, and hours change as the preview showed.

**Verification:** the old read-only notice ("Admins approve only when no HR user…") no longer appears for admins.

---

### U5. Admin direct fix on the day view

**Goal:** An admin can add, flip or ignore a punch on an employee's day without a request.

**Requirements:** R4, R5, R7

**Dependencies:** U1, U3

**Files:**
- Modify: `essl_hub/routes/fixes.py` (new admin-only routes, CSRF and a per-actor rate limit)
- Modify: `essl_hub/routes/fix_views.py` (shared preview, validation)
- Modify: `essl_hub/db_fixes.py`
- Modify: `essl_hub/access.py` (`can_fix_punches_directly`: admin only, and never on the admin's own punches)
- Modify: `templates/_day_breaks.html` (per-punch actions for admins)
- Test: `tests/test_web.py`, `tests/test_access.py`, `tests/test_db.py`

**Approach:**
- Per-punch actions: "Mark as In/Out" (flip), Ignore, and "Add missing punch" with time and direction, plus a required reason.
- Confirm-then-apply: show hours before and after using the same overlay merge as the HR preview, then write. Never edits `punches`. A flip on an unconfirmed guess shows the history warning.
- The database method follows the authority rule in Key Technical Decisions: actor id only, everything else derived, punch-set hash re-checked at apply.
- The day view then shows "Fixed by <name> (admin), <time>: <reason>".

**Patterns to follow:** `fix_review.py` confirm-then-apply preview; `_check_missed_time` for time validation; `punch_set_hash` to refuse applying against a changed day.

**Test scenarios:**
- Happy path: admin flips the first Out of the reported day to In; the day pairs correctly and the row names the admin, role at the time, path `direct` and the reason.
- Happy path: admin adds a missing Out; preview hours equal applied hours.
- Edge: ignoring an already-ignored punch, or a fingerprint owned by another employee, is inert.
- Edge: the day changed between preview and apply (new punch collected) → refused with a refresh prompt.
- Edge: flipping a byte-15 guess later confirmed by history shows "device later reported X".
- Error path: an admin fixing their own punch is refused and pointed at the request path; missing reason, a non-admin role, a forged role or self flag in the form, an archived target, a bad CSRF token and a missing time all refuse with no write.
- Integration: alerts and exports score the day with the correction, matching the day view; Frappe still receives the stored direction (deferred).

**Verification:** every direct fix appears in the day view with its actor and reason, and `punches` rows are byte-identical before and after.

---

### U6. `backup fetch`: a scrubbed local copy of an R2 snapshot

**Goal:** Produce a safe, inert copy of a snapshot for inspection, run on the production host.

**Requirements:** R8

**Dependencies:** none

**Files:**
- Modify: `essl_hub/backup.py` (`fetch` command; shared download-and-verify helper with `restore`; `fetched_copy` marker)
- Modify: `essl_hub/collector_main.py`, `essl_hub/alerts_main.py`, `essl_hub/frappe_sync.py` (refuse to run on a fetched copy)
- Modify: `README.md` (backups section)
- Test: `tests/test_backup.py`, `tests/test_collector_main.py`, `tests/test_alerts_main.py`

**Approach:**
- `fetch [--key KEY] --out DIR` runs through the backup service on the production host (for example with the output folder mounted), then the file is copied to the dev box. It takes no lock on the live database path and works in a 0700 temp folder inside the output folder, removed on any failure.
- Steps on a temp copy: verify (opens, integrity, foreign keys, schema not newer); set `devices.comm_password` to NULL; delete `api_keys`; clear queued door-parameter requests; re-apply the existing scrub of sessions, sign-in codes, report links and the mail outbox; stamp a fresh `database_id` and the `fetched_copy` marker; `VACUUM`; re-verify; then write the final 0600 file and refuse to overwrite. Employee names, emails and punches remain; the README says to run the dev web app only, with mail disabled.
- The marker makes collector, alerts, Frappe sync and `backup adopt` refuse to start on the copy.
- README documents the host procedure and that a developer who wants to run `fetch` on their own machine needs a separate read-only R2 token.

**Patterns to follow:** `cmd_restore`, `_verify_restored`, the `--drill` branch and `test_drill_restore_gets_new_id_and_never_uploads`.

**Test scenarios:**
- Happy path: fetch the newest snapshot into an empty folder; it opens, passes integrity, has a different `database_id`, blank device passwords, no API keys, no queued door requests.
- Happy path: a planted secret in `comm_password` is absent from the final file's raw bytes after the vacuum.
- Edge: `--key` for a nightly object; an existing output file is refused; the temp folder is gone after success and after failure.
- Error path: corrupt download leaves nothing behind; a snapshot newer than the code is refused.
- Integration: collector, alerts and `backup adopt` refuse a fetched copy; the live data folder is unchanged after a fetch.

**Verification:** after `fetch`, the production data folder is unchanged and the output file passes every scrub assertion.

---

### U7. Guarded restore over a live database

**Goal:** Replace the live database from a snapshot, with exclusivity proven and the old files kept.

**Requirements:** R9

**Dependencies:** U6

**Files:**
- Modify: `essl_hub/backup.py` (`restore --replace`)
- Modify: `README.md` (stop, restore, start procedure)
- Test: `tests/test_backup.py`

**Approach:**
- `--replace --yes`: take an exclusive lock on the live database with a short timeout and abort if it cannot be had (something still has the file open); run the pre-restore snapshot inside the command; move `-wal`, `-shm` and then the main file aside to timestamped `pre-restore` names (0600, never deleted); install the verified snapshot with the existing guarantees (database ID kept, sessions gone, everyone signs in again).
- README gives the host procedure (stop `web`, `collector` and `alerts`, run the command through the backup service, start them) and the consequences: roles, archives and fixes made after the snapshot are rolled back (re-check departed or demoted accounts); a v9 snapshot needs web or collector to boot once before backups resume; keep the pre-restore files as audit evidence.

**Patterns to follow:** `_refuse_existing`, `backup_lock`, `cmd_restore` temp and rename flow.

**Test scenarios:**
- Happy path: existing database plus `--replace --yes` → old files moved aside, snapshot in place, database ID preserved.
- Error path: no `--yes`, a second connection holding the database, a failed pre-restore snapshot or a failed verify leaves the live files exactly as they were.
- Edge: stray `-wal` or `-shm` without a main file is moved aside too; running twice never overwrites an earlier `pre-restore` copy.
- Edge: a crash between renames leaves a recoverable state (test the order).
- Integration: after replace, the web layer opens the restored database and the schema version matches.

**Verification:** restoring a snapshot over a modified database returns the older rows and leaves the modified file recoverable.

---

## System-Wide Impact

- **Frappe and API consumers** still receive stored directions; corrections are not pushed (deferred), so a corrected day can differ from Frappe.
- **Alerts and exports** read through the same overlay path, so direct fixes reach them with no change; U5's integration test proves it.
- **Backups and deploy:** snapshots carry schema v10, and an older image refuses them. Deploy order is code first, and U3 changes `deploy/install.sh` so the pre-update snapshot is taken before the new image builds.
- **Roles:** admin gains approve and direct-fix power. HR keeps its rights; the append-only audit record is the control.

## Risks & Dependencies

| Risk | Mitigation |
|---|---|
| v10 table rebuild loses or reorders corrections | Characterization test of overlay results and `sqlite_sequence` before and after; one transaction; nullable new columns so v9 code still works for a rollback; snapshot taken before build (U3). |
| Forced history reads load the C3 | Cooldown, backoff and an hourly cap (U2); the unit is skipped unless the evidence needs it. |
| A direct admin edit hides a real device fact | Overlay only; the raw punch stays listed with its device byte and the fixer's name; a "device later reported" note when history disagrees. |
| Insider abuse through direct fixes | Append-only audit, authority re-derived in the database layer, no direct fix of one's own punches. |
| A fetched copy leaks secrets or acts on production | Scrub, vacuum and byte-level test; the marker stops collector, alerts, Frappe sync and adopt; no R2 keys on the dev box. |
| Replace-restore rewinds data | Exclusive-lock proof, pre-restore snapshot inside the command, kept pre-restore files, README on rolled-back roles and archives. |
| Evidence changes the diagnosis | The Open Question gate: U1 and U3–U7 stand either way; only U2 depends on it. |

## Sources & Research

- Raw rows pasted by the user (2026-10-28), the user's `/day` screenshot and the quick-swipe scenarios run against `compute_day`.
- `essl_hub/hours.py` (`_resolve_directions`, `apply_corrections`), `essl_hub/collectors.py` (`collect_c3`, `_c3_trusted_direction`, `_c3_history_due`), `essl_hub/collector_main.py` (history repair), `essl_hub/db.py` (`fill_punch_directions`, corrections and archives schema), `essl_hub/access.py` (`can_approve_fix`), `essl_hub/backup.py` (`cmd_restore`, `_build_snapshot`), `deploy/install.sh`.
- Earlier plans: `docs/plans/2026-10-12-001-fix-punch-accuracy-and-admin-upgrades-plan.md` (byte-15 decision and its evidence), `docs/plans/2026-10-26-002-feat-missed-punch-alerts-and-corrections-plan.md` (fix requests), `docs/plans/2026-10-27-001-feat-id-issue-punch-archive-and-ui-polish-plan.md` (archive overlay model).
- Document review (2026-10-28): coherence, feasibility, scope, security and adversarial passes; their findings are folded in above.
