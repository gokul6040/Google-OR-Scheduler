# Therapy Scheduler Rules

## Clinician shifts — 18 November 2026

User decisions (17–18 Nov 2026): centres can run several therapist shifts (IQRAA:
Shift 1 9:00–5:00, Shift 2 9:40–5:30, Shift 3 12:35–7:40); staff design shifts in a
shift creator ("add session / add break", each row starting where the last ended),
assign clinicians to shifts, and the scheduler books each clinician only in their
shift; the calendar gets a shift filter (Shift 1 by default). "By default all should
follow this 40 min slot"; a centre may use another length, but not two lengths in
one centre; overlapping times "should show up as a warning"; children may wait
through short breaks but not through lunch; 12:35–1:15 is lunch for some shifts and a
session for others; the working day is 7:00 AM – 9:00 PM.

- **Implemented — shift creator (Shifts screen).** Per centre: shifts made of
  sessions and breaks (Short break or Lunch). A new row starts at the previous
  row's end; a session's end is start + the centre's session length (40 min by
  default). Save rejects wrong lengths, times outside 7:00 AM–9:00 PM, rows that
  overlap inside the shift, and a break cutting through a session.
- **Implemented — overlap warning.** A session that partly overlaps another active
  shift's session at the same centre (e.g. 9:20–10:00 against 9:00–9:40) is listed
  as a warning; such a shift can be saved as a **draft** but not used for
  scheduling until fixed. Sessions must match another shift's times exactly or not
  touch them, so within a centre sessions never partly overlap.
- **Implemented — default shift.** Every centre starts on the original timetable
  ("Shift 1", sessions 9:00–4:50, lunch 12:35–1:15, short breaks 10:20 and 3:15).
  It is stored the first time the centre's shifts are edited. Clinicians without an
  assignment work it. When a centre gets its first extra shift, its current
  clinicians are recorded on Shift 1, so only clinicians who arrive later (e.g. from
  the Zoho sync) show **Check shift**.
- **Implemented — deactivating shifts and the session length, 18 Nov 2026.** User
  decision: every centre starts on 40-minute Shift 1 (IQRAA keeps it); any shift,
  including Shift 1, can be deactivated after an "are you sure" warning. The
  default shift is deactivated last (other shifts first); a non-default shift
  still needs its clinicians moved first. Once a centre has no active shift its
  clinicians are not scheduled (shown as "No shift"); bookings stay as booked and
  are listed in the warning. The next shift put in use becomes the centre's
  default, and **its first session sets the session length** for the whole centre
  (e.g. 9:00–9:50 → 50 minutes; 15 minutes to 3 hours); every later session and
  shift at that centre must use that length until all its shifts are deactivated
  again. One length per centre; other centres are unaffected. User decision (18 Nov
  2026): the centre's original 40-minute **Shift 1 is never deleted** — deactivating
  it keeps it in the list as **Inactive** (not used for scheduling) with a
  **Reactivate** button. Reactivating first shows a warning (user decision, 18 Nov
  2026): a centre uses one session length, so every shift of another length (in use
  or draft) is listed and deleted on confirm; shifts already using 40 minutes stay
  in use with their clinicians; clinicians left without a shift move to Shift 1;
  upcoming bookings outside Shift 1 are listed and stay as booked. Shift 1 becomes
  the default again and the centre returns to 40 minutes. Reactivation runs the same
  overlap check as "Save and use": if a kept shift has a session that partly overlaps
  a Shift 1 session (e.g. 9:20–10:00 against 9:00–9:40), the clashing times are shown
  and Shift 1 stays inactive until staff fix them; exact matches and back-to-back
  sessions are fine. Deactivating any other shift deletes it
  ("Delete shift", after an "are you sure" warning).
- **Implemented — assigning shifts (Clinical team).** Shift column with a dropdown
  per clinician and a bulk "Assign shift" for clinicians of one centre. A shift
  belongs to one centre. Upcoming bookings that fall outside the new hours are
  listed and need "Save anyway"; they are **never moved or cancelled** and stay
  valid for planning (the clinician keeps that one session open for them).
- **Implemented — shift filter and Select all (Clinical team), 18 Nov 2026.** The
  Filters panel has a Shift filter (grouped by centre). The header tick box selects
  every clinician the current search and filters show, on every page, so e.g. all
  Shift 2 clinicians can be moved together before Shift 2 is removed. A shift can
  only be removed once nobody is on it (Shift 1 never). User decision (19 Nov 2026):
  deactivating Shift 1 still works as before, and the Shift filter has **No shift
  (n)** — the active clinicians with no active shift (not scheduled), shown even when
  their calendars are empty. Staff pick it, tick Select all and assign them; they
  leave the list once on an active shift, and No shift (0) means everyone is
  covered. Clinical team only, not the Calendar. With many centres (19 Nov 2026) the
  Shift filter follows the Location filter: with no location it offers only All
  shifts and No shift (n) across all centres, with a hint to pick a location; with a
  location it lists that centre's shifts, each with its count of active clinicians,
  plus No shift (n) for that centre. Changing the location resets a shift from
  another centre to All shifts. While any active clinician (at the chosen location)
  has no shift, a banner above the table says "⚠ N clinicians have no shift and
  won't be scheduled · Show them"; Show them applies the No shift and Active filters
  and clears the name search and department filter (the location stays), so it
  lists exactly the clinicians the banner counts. The banner hides at 0.
- **Implemented — kept bookings that overlap new times, 18 Nov 2026.** User
  decision: when a shift edit or a shift move leaves a kept booking that partly
  overlaps a new session (e.g. kept 9:00–9:40, new 9:20–10:00), show a warning.
  The confirmation lists each such booking with the new time it overlaps. On that
  day the planner does not book the clinician (or room, beyond capacity) at the
  overlapping new time. Clashes are checked by clock time, not session number, in
  the planner, the quick preview, replacement offers and the safety check, so a
  clinician, child or room can never be double-booked by overlapping times.
- **Implemented — shift hours outside the planner, 18 Nov 2026.** A manual booking
  at a time outside the clinician's shift shows the warning "This time is outside
  the doctor's shift" and, like every booking warning, needs "Reserve anyway". The
  manual-booking clash checks (clinician, child, room) compare clock times. The
  replacement search ("pull a later visit forward") only offers openings inside each
  clinician's shift, and treats a time as available unless it is marked unavailable,
  like the planner (so new shift times such as 12:35 can be offered). Moving a visit
  to a start time outside its doctor's shift is refused with "9:40–10:20 isn't a
  session time in this doctor's shift. Choose one of their shift times." (user
  wording, 19 Nov 2026).
- **Implemented — booked times stay visible, 18 Nov 2026.** Every booking shown on
  the calendar keeps its time row, even after no shift uses that time any more
  (e.g. 5:30 PM removed from the last shift that had it).
- **Implemented — break changes, 18 Nov 2026.** Saving a shift that is in use, or
  removing one, lists upcoming back-to-back visits the change would split (e.g. a
  short break turned into lunch, or a removed shift's short break) and needs "Save
  anyway"; the visits stay as booked. A schedule preview made before such a change
  can no longer be saved (its "has anything changed" check includes the centre's
  sessions and which of them chain).
- **Implemented — scheduling.** Each clinician is available only in their shift's
  sessions. A centre's sessions are ordered by clock time; a back-to-back visit may
  chain two sessions with no gap or with a short break between them, never across
  lunch or an unexplained gap. A child may see clinicians from different shifts in
  one visit when the times chain (e.g. 11:55 with a Shift 1 clinician, then 12:35
  with a Shift 3 clinician). Part-day absences, moves ("Move this visit"),
  replacement searches and admin bookings all use the centre's session order.
- **Implemented — session numbers.** Original sessions keep numbers 0–9, so no
  saved booking, preference or availability row was converted. A new session time's
  number encodes its own start and end (100000 + start minute × 1440 + end minute),
  identical in every database and process.
- **Implemented — preferences.** A child's preferred times are the start times
  their own clinicians' shifts use; "nearest free time" follows the centre's clock
  order. Morning means a start before 12:30.
- **Implemented — calendar.** A Shift picker (default shift first, other shifts,
  All shifts) appears once a centre has more than one shift. Rows are the sessions
  of the clinicians shown, in clock order; gaps show as "Break" or "Lunch break";
  a session a clinician does not work shows "Lunch", "Break" or "Off shift" and
  cannot be booked. The picker changes the view only, never scheduling.
- **Implemented — Zoho.** A booking is sent with the Zoho time slot matching its
  times. User decision (18 Nov 2026): a booking whose time Zoho does not have yet
  waits on its own with the reason "Zoho has no 18:20–19:00 time slot yet"; every
  other booking that day is still pushed, and the waiting one goes in a later push
  once the slot exists and the lookups are refreshed. Zoho slots are matched to
  sessions by their start and end times, so a freshly started app finds new slots
  without first loading the shift times. On 18 Nov 2026 the 12:35, 16:50 and 17:30
  40-minute slots (numbers 11–13) were added to Thanal's slot list via the API with
  the user's OK; 18:20 and 19:00 were refused because Thanal's slot form only
  accepts start times 09:00–18:00 (Thanal to widen it). On 19 Nov 2026 the export
  lookup file was refreshed from Zoho (read only) with the three new slots, so
  bookings at 12:35, 16:50 and 17:30 are pushed with slots 11, 12 and 13.
- **Not in this version:** switching a centre's session length on a future "from
  date" while keeping the old length until then, an "Add missing times to Zoho" button (needs Thanal's written OK), shift start
  dates, different shifts per weekday, clinicians at two centres.
- Tests: `tests/test_shifts.py` (IQRAA shifts, 15-session day, warnings and draft,
  scheduling inside shifts, lunch, affected bookings, check-shift rule, overlapping
  kept bookings, clock-time room use, off-shift manual booking, off-shift
  replacement, booked time kept on calendar, break to lunch, deactivating Shift 1,
  first session sets the length, default deactivated last, Shift 1 kept and reactivated with a warning, other shifts deleted, same-length shifts kept, reactivation blocked by overlapping times, Clinical team No shift flag, move outside the
  doctor's shift explained),
  `tests/test_thanal_export.py` (held rows, new Zoho slot without loaded shift
  times, missing Zoho time), `tests/test_web_app.py` (calendar data),
  `tests/test_multi_centre_e2e.py` (night plan across four centres with 40-, 50- and
  30-minute shifts and one with no shift: visits inside each doctor's shift, no
  clock overlaps, WhatsApp offers created with the real times and not sent, replies,
  same-centre replacement suggestions, Zoho rows built or held per time).

## Exact preferred start time — 16 November 2026

User decision: "preffered time, should have all the time slots ! … suppose patient
a preferes 9.40 … then our system should try to schedule that patient in that
time", agreed as: the time is when the visit starts, "No priority" day means every
day, and it stays a wish, not a rule.

- **Implemented.** Preferred time offers the 10 slot start times (9:00 AM–4:10
  PM); since the same day several can be picked (see below).
- The preferred time is the visit's **start** time: the scheduler rewards a visit
  by the slot it starts in, and the visit's other therapies follow straight after
  (R-02). This applies to Morning/Afternoon too (a visit starting in the band).
- With an exact time, the chosen slot scores 100 and each slot away from it 20
  less, so the nearest free start time is preferred when the chosen one is taken.
- With no preferred day picked, a chosen time applies on every working day; with
  picked days it applies on those days (see "Several preferred days" below).
  Clearing both removes the preference.
- It remains a soft preference (R-22): it never costs a child a session and ranks
  below meeting the weekly plan, catch-ups and even spacing (R-15).
- Tests: `tests/test_web_app.py` (exact time every day, nearest-slot scores),
  `tests/test_scheduler.py` (visit starts at the preferred time).

## Several preferred days and start times — 16 November 2026

User decisions: "if 3 sessions are there … theyll have to come 3 times a week …
we should be able to provide which those 3 days are (preffered days) … for each -
it should be dynamic"; **picked days win over even spacing**; then "they could
select multiple preferences … 1 pm, 2pm , 3pm etc.. Multiple select should be
enabled … Similiarly for days Monday, tuesday , wed, thursday or whatever!" —
so any number of days and any number of start times (this replaced the earlier
"up to the visits a week" limit the same day).

- **Implemented.** The patient drawer has Mon–Sat day buttons and the 10 start
  time buttons, all multi-select, with Morning / Afternoon / Clear shortcuts for
  the times. It shows the child's visits a week for reference (the most frequent
  active therapy: 3 + 2 sessions/week → 3 visits; 2 + 1 → 2).
- Times: a chosen start slot scores 100; other slots score 20 less per slot from
  the nearest chosen one, so the nearest free time is used when all are taken.
  Saved as `patients.preferred_time = "slots:5,6,7"`; older "Morning",
  "Afternoon" and "09:40" values are still read.
- Picked days without times mean any time on those days; times without days
  apply every day. Nothing picked = no preference.
- Weekly planning: every slot of a picked day scores a day bonus (300 + the time
  score, ×100 reward = 30,000+ per visit), which outweighs uneven spacing (5,000
  per day of deviation, R-15) but stays below meeting the weekly plan.
- Daily planning (tomorrow only; Thanal nightly job and browser generator): a
  child is **not booked on a day staff did not pick while their remaining picked
  days this week can still hold every remaining visit**. When fewer days than
  visits are picked, the extra visits are booked on any day as before. If a picked day is full, the child is booked
  on the following days as soon as the remaining picked days can no longer hold
  the remaining visits; a full **last** picked day of the week can leave that
  visit unbooked that week, as with any full day.
- Older single-day preferences (no day bonus) are not held; they keep the R-22
  soft preference until staff re-save them.
- Tests: `tests/test_web_app.py` (several days and times, more days than visits,
  labels, older formats), `tests/test_scheduler.py` (several start times),
  `tests/test_preferred_days.py` (picked days beat even spacing; daily hold,
  release when fewer days than visits, full picked day, booking on a picked day).

## Refresh from Zoho on the Clinical team page — 16 November 2026

User request: "add that zoho refresh button in clinicians tab also".

- **Implemented.** The Clinical team page (workflow app) has the same Refresh from
  Zoho button as Patients. It runs the same full import (`/api/zoho/refresh`),
  shares the same one-at-a-time, 5-minute cool-down and 23:45–00:30 IST block,
  and reloads the clinician list when done. New clinicians start with their Zoho
  status; existing clinicians keep the status set in our app. Sends no messages.

## Clinician Active/Inactive is set in our app — 16 November 2026

User decision: "I want to be able to click status , and make them active and
inactive just like patients tab - like if active they will be taken , if not they
wont be - also keep the existing active as active , and existing inactive as
inactive … this should be done in our thing itself, and backend will handle it !"

- **Implemented.** On the Clinical team page, staff click a clinician's status to
  mark them Active or Inactive, using the same confirmation as the Patients page.
  Marking inactive needs a reason (1–1,000 characters); both changes are saved
  with an audit record (`therapist_status_changed`). Endpoint:
  `POST /api/therapists/<id>/status`; admin accounts only (doctor accounts stay
  limited to their own sessions).
- Active clinicians are used when new sessions are generated; inactive clinicians
  get no new sessions (rules R-04 and S-05 unchanged). Visits already booked for a
  clinician marked inactive stay on the calendar for staff review, as in the
  existing consultant-inactive rule; the toast says how many upcoming visits remain.
- Our app owns each existing clinician's status. The nightly Zoho syncs (Thanal
  consultant import and the Zoho test import) no longer change it; Zoho's status
  is used only when a clinician is first imported. Existing statuses were kept as
  they were.
- A patient cannot be marked active while their current plan names a clinician
  who is inactive in our app; the Mark active check names the therapy and
  clinician and is evaluated live, not from the nightly Zoho snapshot.
- Marking a clinician active raises their centre's stored ceiling to at least the
  active clinician count (S-03). The ceiling is never lowered, so kept bookings
  cannot make a centre unplannable.
- Tests: `tests/test_therapist_status.py`, `tests/test_thanal_consultant_import.py`,
  `tests/test_thanal_daily_sync.py`, `tests/test_flow_test_zoho.py`.

## Push planned visits while parent messaging is off — 15 November 2026

User decision (15 November 2026): while WhatsApp messaging to parents is switched
off, the Zoho push sends every planned visit as **Scheduled**, starting with the
already-planned 13 and 14 November 2026 (pushed once, by hand) and then every day
through the normal 23:00 push.

- Controlled by the operator switch `MAIN_WORKFLOW_PUSH_UNSENT_AS_SCHEDULED=true`
  (default off). It never applies when live messaging is on
  (`MAIN_WORKFLOW_LIVE=true`); turn it off at the WhatsApp cutover.
- At push time, a target-day visit still **waiting for confirmation** becomes
  Confirmed (audit `flow_assumed_yes_unsent`) when its parent was never messaged
  and never replied, and the child is active. Visits whose offer was actually sent
  keep the existing rule (unanswered sent offers are assumed accepted at 23:00).
- The preview (dry run) only counts them and changes nothing.
- Cancelled and needs-rescheduling visits are unchanged: a visit released before
  its first push is never sent; one released after is updated to Cancelled.
- Automatic schedule (IST): 23:00 pushes tomorrow, 07:00 re-pushes today's late
  changes (`thanalpush`, `thanalrepush`).

Implemented in `flow_test/zoho_export.py` (`push_unsent_enabled`,
`_finalise_unsent`); covered by `tests/test_main_workflow.py`
(`test_push_switch_*`).

### Push into the trial form only, skipping Zoho workflows — 16 November 2026

User decision (16 November 2026): "Nothing should be pushed anywhere else, only
push in trail one !, skip workflow or whatever idc". Thanal's form workflow on
`Schedule_Adhoc_Trial` refused 185 of 225 rows for 13 November because staff had
already booked those doctors in the main schedule (`All_Schedules`), and on
success it creates session records in other reports.

- Operator switch `MAIN_WORKFLOW_ZOHO_SKIP_WORKFLOW=true` (default off) sends
  `skip_workflow: ["form_workflow", "schedules"]` on every create and update.
- It applies only to Thanal's `thanal/eic` trial form and report; any other
  target is written with workflows as before.
- Effect: rows land in the trial form only; Zoho's overlap check and its
  session-creating workflow do not run for them.

Implemented in `flow_test/zoho_push.py` (`workflow_options`); covered by
`tests/test_thanal_export.py`
(`test_skip_workflow_switch_only_applies_to_thanal_trial_form`).

## Therapy courses end at a session total — 12 November 2026

User decisions (12 November 2026):

- A prescription with a frequency and a duration is a **course** of
  `sessions/week × months × 4` sessions (1 month = 4 Monday–Saturday weeks).
  Example: 2/week for 2 months = 16 sessions.
- **A course ends when its total is reached, not on a calendar date.** If two
  months pass at 4/16, scheduling continues until 16/16. The plan's calendar end
  date no longer blocks bookings for a course (it still does for legacy plans
  with no duration).
- **Counting:** every saved session (proposed, pending, confirmed or completed)
  counts as attended. A parent Yes or no reply keeps it. A cancellation, a parent
  No or a recorded no-show removes it again, so 6/16 becomes 5/16 and one more
  session is booked later. Off-plan manual reservations never count.
- **Pace:** each week still asks for at most the prescribed weekly frequency.
  Missed sessions are made up by finishing later, never by doubling up. For a
  course, the previous-week shortfall rule (R-23) now only gives priority; it no
  longer raises the weekly number. Legacy plans without a duration keep R-23 as is.
  In the last week the request is only what is still owed (e.g. 15/16 asks for 1).
- **Start:** everyone starts at 0 from the first session the app saves. Sessions
  before go-live are not counted.
- **New plan = new course:** a new Zoho plan record creates new requirements, so
  the count starts again at 0. Editing the same Zoho record does not reset it.
- **At the total** the therapy gets no new bookings. A manual reservation for it
  shows the warning "This therapy course already has all N sessions booked or done".
- **Patient side panel only** (not the Patients list): each therapy shows "6/16 sessions" (or "· complete"); the
  label says sessions, not completed, because upcoming saved sessions are included.
- **Repeated cancellations:** when a child's last 4 sessions in a row were
  cancelled, declined (parent No / patient absence) or no-show, the Patients tab
  and the patient panel show "4 cancelled in a row" for staff to follow up. A
  doctor's absence or system housekeeping (a replaced proposal, a child made
  inactive) does not count. The warning clears once a session that has taken
  place (today or earlier) was not cancelled; upcoming bookings do not clear it.

Implemented in `database/repository.py` (`course_target`,
`course_session_counts`, `load_scheduling_problem`, `load_daily_scheduling_problem`),
`services/rolling_schedule_service.py`, `services/admin_reservation_service.py`,
`services/course_progress.py`, `web_app.py`, `web/app.js` and `web/styles.css`.
Covered by `tests/test_course_progress.py`.

## Staff calendar cancellation — 12 November 2026

- Staff can cancel **This session** or **Entire visit** from a saved proposed,
  pending or confirmed appointment. A visit means the same patient/date/bundle;
  another visit or day is unaffected. A reason is required. Completed therapy or
  another terminal status rejects the selected operation without partial changes.
- Cancelled rows remain as `CANCELLED_PATIENT` for history, preserving their IDs,
  prescriptions and attendance. They stop reserving resources and counting toward
  booked weekly targets; the prescribed sessions/week does not change. Staff
  cancellation does not record whole-day unavailability.
- The new `/api/appointments/{id}/staff-cancel` operation commits cancellation,
  staff/reason audit, offer closure and queue cleanup together. The shared route
  takes the sender lock before the workflow lock. Repeated/concurrent cancellation
  requests cannot double-decrement counts or duplicate the audit.
- Every combined offer containing a cancelled session is closed. Its unsent offer,
  reminder and dependent text jobs become obsolete; stale replies cannot restore
  the appointment. Remaining pending sessions of a partially cancelled offer stay
  booked with a staff-review warning; they are not silently confirmed or re-offered.
- Unfilled cancelled openings stay red and clickable on the calendar but are
  excluded from the active appointment/doctor totals. The existing suggested-patient
  and manual-patient reservation flow opens after cancellation. Staff select and
  confirm/offer a replacement; nothing is automatically booked or messaged. Once a
  replacement reserves an opening, its cancelled source card is hidden.
- Existing Zoho export rules apply: never-pushed cancelled rows are not created;
  previously pushed rows update to Cancelled with the same Scheduler_Key on the
  next explicit/scheduled push. Cancellation itself performs no external write.

Implemented in `services/cancellation_service.py`, `flow_test/shared_ui.py`,
`web_app.py` and `web/app.js`. Covered by `tests/test_staff_cancellation.py` and
the staff-cancellation cases in `tests/test_thanal_export.py`. Deploy the matching
backend and frontend together. Operational workflow requests to the older `/cancel`
route are rejected with a refresh instruction, so an old browser cannot bypass the
required scope/reason or queue cleanup. The separate original app's legacy handler
is unchanged. The new UI never falls back when `/staff-cancel` is unavailable. The calendar
advertises `staffCancellationEnabled`; the new UI disables cancellation with an
explanation when connected to an older backend without this capability.

## Thanal appointment export — 11 November 2026

The Thanal adapter exports exactly Branch, Registration, For_Parent, Date_field,
Department, Consultation_Type, Consultant, Appointment_Time_Slot, Slot_Number,
Status and Scheduler_Key, plus Date_Time, End_Time and Duration, to
Schedule_Adhoc_Trial / Schedule_Adhoc_Trial_Report. These three workflow values
come from the appointment date and selected slot in clinic local time:
Date_Time uses dd/MM/yyyy HH:mm, End_Time dd/MM/yyyy HH:mm:ss, and Duration
is the slot length in minutes. Zoho's On Validate workflow creates the related
session; exports preserve that workflow. The eleven selected report columns
remain the user-facing output. Date-only or inconsistent timing payloads are rejected.
Registration uses the child's existing Creator registration ID. Live read-only
report evidence confirms this lookup exposes Name2 (child name); names are
displayed in the local preview, never used to choose an identity. For_Parent is
always false as explicitly requested by the user.

Department and consultation mappings use observed Creator IDs joined to imported
therapy IDs. Time slots match both start and end time, branch and verified Zoho
slot number. The checked-in non-patient lookup catalog covers all ten 40-minute
scheduler slots. The sampled report also contains shorter slots; those are not
substituted. Missing or ambiguous mappings block the push with a named preview
reason. No master records are created or names guessed to repair a missing ID.

Confirmed appointments export as Scheduled, including appointments confirmed by
the existing manual unanswered-offer rule. The assumed/explicit reply distinction
stays local. Completed and cancelled appointments retain Completed / Cancelled.
A visit already pushed and later declined (NEEDS_RESCHEDULING) is updated to
Cancelled on its existing Zoho row (user decision, 11 November 2026); a visit
released before any push is never exported. NO_SHOW remains held pending approved
destination semantics. Nothing is silently relabelled Scheduled. Automatic push
times are defined under "Daily automation" below.

The existing stable Scheduler_Key, durable batches and acknowledgements remain
in use. Creator lookup objects are compared by ID when reconciling a lost write
response or checking for changes. Repeat pushes must not duplicate rows. Preview
is read-only; held lookup/status rows wait on their own and the rest of the day is
still pushed (user decision, 18 Nov 2026; see "Clinician shifts"). The separate test-gokul
export retains its original text-field contract. No WhatsApp sends are enabled.

Covered by tests/test_thanal_export.py and existing export/main-workflow tests.
Actual HTTP checks use the fictional Postman simulation; live schema, lookup
shapes and master references were inspected read-only. No live appointment writes
are implied by those tests.

## Thanal release procedure (prepared locally)

`deploy_thanal.ps1` and `main_workflow/thanal_release.py` are a separate, guarded
release for Thanal's subscription, main public database and existing registry.
Schema migration and the initial source/roster import run as manual jobs inside
Azure; the website is stopped until these finish. Messaging stays off and the
messaging worker is not started. The source pull uses the complete master reports
and Calicut-only All_Therapy_Schedule_Plans. The initial approved eligibility
snapshot applies once, with current source validation able to hold more patients
inactive; later deployments preserve local eligibility and later valid new patients.
Deployment creates no daily timer, saved schedule, message or Zoho appointment push.
Local release tests cover wrong-target rejection, credential isolation, bounded
roster encoding and clinical row preservation during additive schema migration.
See DEPLOY_THANAL.md for commands, backup/restore details and failure behaviour.

## Daily automation — user decisions, 11 November 2026

Every day's work prepares **tomorrow** (Asia/Kolkata). Times are IST.

| Time | Step | Command |
|---|---|---|
| 00:00 | Import Zoho (existing `thanaljob`) | `main_workflow.sync --apply` |
| 02:00 | Plan tomorrow, check it and save it; offers are only queued | `main_workflow.daily_plan` |
| 09:00 | Worker sends the queued offers (timed templates) | existing worker |
| 14:00 | One reminder to parents who have not replied (16:00 / +1 h for later sends) | existing worker |
| 23:00 | Finalise unanswered sent offers as accepted (R-31) and push tomorrow to Zoho | `main_workflow.push --day tomorrow --apply` |
| 07:00 | Re-push today's late changes (e.g. a late No becomes Cancelled) | `main_workflow.push --day today --apply` |

- **02:00 auto-save without staff review** when every check passes. A failed check
  saves nothing and leaves a "Night plan failed — nothing saved" staff alert; an
  incomplete save (held centre/children) leaves a "needs review" alert. Inputs that
  change mid-search are planned again once. A run that already saved tomorrow does
  nothing. Live messaging without timed templates is refused, so offers can never go
  out at 02:00. The job never sends a message.
- **Weekend:** Sunday is closed. The Saturday-night plan and the Saturday 23:00 /
  Sunday 07:00 pushes do nothing. Sunday's 02:00 run plans Monday, so Monday's
  parents are messaged on Sunday from 09:00.
- **Unanswered at 23:00:** sent offers without a reply count as accepted and are
  pushed as Scheduled (existing R-31 rule; unsubscribed or undelivered ones stay for
  staff).
- **Late changes:** the 07:00 push updates the same Zoho rows; it never duplicates.
- Replies, replacements (staff-chosen) and messaging rules are unchanged.

Implemented in `main_workflow/daily_plan.py` and `main_workflow/push.py --day`; covered
by `tests/test_daily_automation.py` and `tests/test_thanal_export.py`. The Azure
timers and WhatsApp cutover are deployment steps (DEPLOY_THANAL.md); until they are
enabled these remain manual.

## Thanal daily import operation — 11 November 2026

The separately configured `thanaljob` imports through the running app's
authenticated API, using the app's image and structured three-report contract.
It does not need a direct database URL. The intended Azure cron is
`30 18 * * *` UTC, or 00:00 IST daily, with one replica and no automatic
execution retry. This setup is separate from the initial release procedure.
Imports preserve staff-controlled patient eligibility and do not reapply the
initial roster. They can prepare an unsaved draft; no appointments are published,
no WhatsApp messages are sent, and no appointments are written to Zoho by the
timer. Live messaging remains disabled and its worker remains stopped.

A cloud verification run committed 443 valid plans / 758 therapy requirements,
with 273 plans held for source corrections and zero messages sent.

**Patients and held plans (user decisions, 11 November 2026).** Every night pulls all
records. A **new** child with a complete plan (valid consultant, therapy, phone, active
consultant) starts **active**; a new child whose plan has a problem is still saved, but
**inactive**, and the status dialog lists what to fix in Zoho. The import **never switches
an existing inactive child on** — only staff can, whatever the reason they are inactive.
An **active** child whose latest Zoho plan breaks keeps being scheduled on their previous
valid plan, with a "Latest Zoho plan has a problem — previous plan still in use" staff
item; other children with problem plans are held from new bookings and offers until
corrected. A run with held plans reports `COMPLETED_WITH_ISSUES` and exits 0 (Azure shows
it as succeeded); only a real failure (Zoho unreachable, invalid source, draft error)
exits non-zero. Held runs still retain their checkpoint, so the next run re-reads all.
Covered by `tests/test_thanal_daily_sync.py`.

**Refresh from Zoho (user decision, 12 November 2026).** The Patients page has a
"Refresh from Zoho" button that runs this same import immediately, in the background,
so a child corrected in Zoho can be marked active the same day instead of after the
next 00:00 run. It applies exactly the rules above (it never switches an inactive child
on; staff still press Mark active), sends no message and changes no booking. One refresh
runs at a time, with a five-minute cool-down between refreshes (each uses Zoho API calls)
and none between 23:45 and 00:30 IST, when the nightly import runs. The page shows the
outcome: new active, new inactive, inactive children now ready to activate, and how many
still need fixing in Zoho. Covered by `tests/test_manual_refresh.py`.
This does not bypass any validation or change scheduling constraints. The
structured import/preservation rules remain covered by
`tests/test_thanal_daily_sync.py`; this operation was checked against the live
app and aggregate job logs. See DEPLOY_THANAL.md for the report names and
verification procedure.

## One-time active patient roster — 10 November 2026

### Staff activation checks (implemented locally)

- Staff activation in a Thanal database requires a usable latest Calicut plan,
  complete registration identity, valid Indian mobile number, valid therapy
  frequency/duration, and an active assigned consultant at the same centre with
  the required therapy capability. The manager explicitly changed the earlier
  contact-only policy: invalid phones now block manual activation too.
- Opening the status dialog checks the latest complete source review and shows
  every detected problem in plain language, naming the affected therapy and
  consultant where available. Mark active is unavailable until checks pass.
  Staff can close the dialog or check again after correcting Zoho and syncing.
- The server repeats these checks on an inactive-to-active POST; direct API
  requests cannot bypass the dialog. Blocked activation returns HTTP 409 and
  preserves the patient's status, appointments and audit history.
- A newly imported child with an invalid phone or inactive assigned consultant
  starts inactive. Valid new children start active. Existing staff-controlled
  status remains unchanged by source edits; staff review existing active children
  if their source details become invalid. The manager explicitly approved making
  one existing child inactive for an invalid phone, leaving 354 active patients.
- Each structured sync publishes its activation review in the same transaction
  as imported records. A partial source import can publish patient-specific
  corrections; source failures/rollbacks cannot overwrite the previous review.
  Successful correction allows manual activation without automatically changing
  local eligibility. The spreadsheet is never an ongoing activation condition.
- A roster-baselined database without a source review blocks activation until
  a source check is available. Legacy/test databases without that baseline or
  Thanal review keep their existing activation behaviour.
- Tests: integrated source correction/multiple problems in
  `tests/test_thanal_daily_sync.py`, authenticated read/direct-POST rejection,
  missing plan and recovery in `tests/test_main_root_workflow.py`.

### Original baseline

- The manager's supplied roster is a one-time baseline, matched by exact
  registration code, never by name. Listed patients with usable latest Calicut
  plans and active assigned consultants are active. Other current patients are
  inactive. Listed children and identifiable Calicut plan children remain visible
  in the patient directory even when their source plan is invalid or absent.
- Missing/ambiguous registration identity, invalid latest plans, and inactive
  assigned consultants keep a listed child inactive with an explicit reason.
  Invalid contact numbers alone did not prevent scheduling eligibility in the original baseline; they were
  reported separately and follow the existing scheduled-message contact rule.
- Apply through `services/patient_roster_service.py` in one transaction under
  workflow/inbound coordination. The baseline is audited and the same operation
  cannot be replayed. Existing status-change behavior applies, including cancelling
  future proposed visits on deactivation; confirmed visits and history remain.
- The spreadsheet is not a daily allowlist. Daily sync preserves existing local
  eligibility, and a genuinely new patient with a valid later source plan starts
  active through the existing importer. Correcting an inactive patient's source
  data does not override staff-controlled eligibility automatically.
- Verified by the one-time roster tests in `tests/test_thanal_daily_sync.py`:
  invalid-plan visibility, inactive consultant handling, contact-only diagnostics,
  identity matching, replay prevention and activation of a later new patient.

## Thanal source contract — 10 November 2026

### Integrated daily sync (implemented locally)

The normal `main_workflow.sync --apply` path now routes through
`main_workflow/thanal_sync.py` when source is thanal/eic and the configured reports
are All_Registrations, All_Consultants and All_Therapy_Schedule_Plans. Every run
rereads complete master snapshots without inventing Modified_Time/consultant
timestamps. Plans are fetched using the uniquely resolved Branch lookup ID for
Iqraa Thanal Super Speciality EIC Centre Calicut; other branches are rejected.
Unrelated clinical fields are stripped before the job sends source data to the app.

All projected registrations are retained in source settings; only children with
valid latest Calicut plans enter the imported treatment roster. Missing/invalid
phones on unused registrations produce diagnostics, NOT staff inbox tasks.
Invalid phone numbers do NOT exclude an otherwise valid plan from generation.
When saving/offering a scheduled visit cannot bind a verified contact, the visit
is held with a contact review item. Dispatch rechecks current source contacts;
unattempted offers may adopt corrected phones; attempted/sent/uncertain offers
are never rebound or resent by sync.

Valid consultants retain one physical identity and multiple capabilities. Source
row errors on unrelated consultants remain diagnostic; affected current plans
are quarantined, never assigned to a guessed doctor. Fundamental identity/DB
adoption conflicts stop and roll back the whole import. Valid masters, source
bindings, prescriptions, contact version and checkpoint share one transaction.

All plan statuses are considered. Newest Added_Time selects the course; tied
newest versions are held. No fallback to an older valid plan occurs when the
newest plan is invalid. Renewals/removals retire demand without changing existing
bookings, attendance or old prescription links; affected saved visits get review
items. Staff-controlled patient eligibility is preserved. A previously imported
child missing a valid scoped plan is held for source review, not silently deleted.

Bad current plans create source review items and yield partial=true; valid plans
still import, but the previous successful checkpoint is retained. Each subsequent
run rereads all reports, so corrections can clear quarantine and advance it.
Sync starts an unsaved background draft only; no appointments/offers are saved
and no messages are sent by importing. The import route and CLI are exercised by
tests/test_thanal_daily_sync.py, including real HTTP via the fictional Postman mock.

Registration-only reading is implemented in `services/thanal_registration_reader.py`
and covered by `tests/test_thanal_registration_reader.py`. It selects Creator ID,
Registration_Id, Name2, Phone_Number, Branch, Status and Added_Time, discarding
unrelated clinical/identity/address columns. Phones use the existing Indian-mobile
normalization; invalid numbers become None with a review issue, never an older or
guessed contact. Duplicate registration codes retain distinct Creator IDs for
review rather than silently merging people. Source statuses are preserved without
activating/deactivating patients or assuming which registrations require therapy.
This stage performs no database writes, saves no schedule and sends no messages.

Consultant-only import is implemented separately in
`services/thanal_consultant_import.py`, covered by
`tests/test_thanal_consultant_import.py`. `python -m main_workflow.consultants`
validates the full report against an explicitly configured Thanal main database;
only `--apply` commits. It does not generate a draft or import patients/plans.
One `ZC-<Creator ID>` identity retains every declared Consultations capability;
the primary department is a stable representative, not a restriction to one
capability. Branch/therapy source IDs are bound to local IDs in workflow settings.
No consultant creation/edit timestamps are fabricated. Missing source rows never
delete/deactivate local records. Explicit Active/Inactive values are respected;
blank/unknown values and malformed branch/capability lookups block an atomic apply.
Existing names under unadopted source IDs and structural changes to a booked or
prescribed consultant require review. Appointment/patient/plan rows are unchanged.
New branches use the number of active consultants as initial capacity, counting
each person once regardless of therapy count; existing capacities are preserved.

User-confirmed: include the latest plan regardless of its Status (Draft, Scheduled
or blank). This does not authorize importing every historical plan as simultaneous
demand or inferring patient eligibility from registration statuses.

For `thanal/eic` / `All_Therapy_Schedule_Plans`, `Per_Week` is weekly session count
and the second number, currently exposed as `No_Of_Week`, is duration in MONTHS,
despite that field's misleading API name. Examples confirmed by the user:
`1, 1` = one session per week for one month; `2, 3` = two sessions per week for
three months. Missing/zero durations or frequencies are flagged, never filled
from guesses. This mapping is Thanal-specific, not a general weeks-to-months rule.

The new read-only projection in `services/thanal_source_reader.py` joins plan
`Name.ID` to registration `ID` and therapy `Consultant.ID` to consultant `ID`.
Consultant `Consultations` supplies declared therapy capabilities;
`Consultation_Type` is employment type (Full-Time/Part-Time), NOT therapy.
Source Added_Time uses observed `dd/MM/yyyy HH:mm:ss`. Ambiguous latest creation
timestamps require review; missing Modified_Time is not invented. These rules
are tested in `tests/test_thanal_source_reader.py`. The projection is NOT yet
connected to database imports; the existing scalar importer remains unchanged.

## Performance and scaling pass — 8 November 2026

**Implemented locally; covered by `tests/test_performance_pass.py` (SQLite and PostgreSQL).**
No deployment, live message or Zoho write is implied.

- **Upgrade step required.** Existing stores must run `python -m main_workflow.migrate --apply`
  before this release starts; the app refuses to start otherwise. The step is additive and
  idempotent: it adds the offer visit day, an offer-to-appointment link table, a Zoho
  push-tracking table, a shared preview table, a contact-list version and query indexes.
  Existing rows are kept; old per-appointment push-tracking settings rows are moved, not lost.
  **Deployment (9 November 2026):** `deploy_main.ps1` checks whether the step is still needed;
  if so, it waits for running sends and Zoho pushes, backs up the data, stops the old app and
  WhatsApp worker, applies the step and verifies it before starting the new version. The
  worker always runs the same image as the app. `flow_test/deploy_azure.ps1` refuses to deploy
  while the step is pending. Covered by `tests/test_main_release.py` (fake Azure; PostgreSQL).
- **Export preparation (#18)** reads only the target date: its offers (through the link
  table, so a visit moved to that date is still found), their replies and decisions, and the
  rows last pushed under that date. Results are identical to before.
- **Contacts (#22, #23).** Webhooks and sends check only the numbers involved, with an indexed
  lookup. The imported parent roster is cached per version; the version is written in the
  same transaction as each import, so a changed roster is always picked up and an unchanged
  one is never re-read.
- **Staff screens (#24)** read without the workflow lock, from one consistent snapshot, and
  return only the selected date's children. Full phone numbers are never included.
- **Database locks (#25)** keep one reusable connection per lock. A dropped connection is
  replaced before the lock is taken, never while it is held.
- **Indexes (#26)** cover offers (day, state, recipient, child), jobs (kind/status, age), inbox
  (resolved, child) and audit entries (entity/action).
- **Retention (#27). Management decision: 180 days.** The worker cleans up hourly: finished
  WhatsApp jobs, offers whose visit day has passed the period, resolved inbox items and Zoho
  push tracking for older dates. Expired send timers and previews are always removed.
  Appointments, therapy plans, patients, the audit log, unfinished or failed jobs and
  unresolved inbox items are never removed; session counts are unaffected.
  `WORKFLOW_RETENTION_DAYS` may change the period (30–3650).
- **Several app copies (#28).** Only one Zoho push runs at a time across every copy (database
  lock). Command-line pushes with `--apply` ask the app to push, so they use the same lock.
  Schedule previews are stored in the database, so any copy can save them; they expire after
  24 hours.

## Centre search budget — 10 November 2026

**Implemented locally; covered by `tests/test_centre_planning.py` and scheduler regressions.**
Production daily previews use CP-SAT with a maximum search time of 300 seconds
for EACH independent centre/resource component, processed sequentially in one
application container. Centres sharing a patient or assigned therapist remain
one component to prevent conflicts. The budget is not divided by demand or by
consultant. A completed optimal/infeasible search returns immediately; unused
time is not deliberately spent or transferred to another component. Model/input
preparation and validation are additional to the solver's search time limit.

Large inputs never skip CP-SAT based on requirement count or a short requested
budget. A greedy placement may supply a solution hint only; the returned result
comes from CP-SAT and passes the existing validator. FEASIBLE is the best valid
incumbent found within the limit, not a claim of optimality. UNKNOWN/INFEASIBLE
does not produce a saveable new draft. Preferences and spacing remain existing
soft objectives; no clinical constraint or capacity rule is relaxed.

Production manual and post-import previews run as database-backed background
jobs. They never save appointments or send messages. A separate planning lock
serializes searches across replicas; workers release the workflow lock after
snapshotting inputs, so replies/sends/staff edits continue. Interrupted preview
jobs can be recomputed on polling. Saved previews still recheck the input
fingerprint and local date; changes during the search require a fresh preview.
Test workflows retain explicitly selected short solver budgets for fixtures;
each component receives that full budget and still uses CP-SAT.

## Corrective pass, unsubscribe and phone format — 8 November 2026

**Implemented locally; covered by `tests/test_corrective_pass.py`.** No deployment, live
message or Zoho write is implied.

- **Truthful saves.** When a centre (or group of centres sharing children or therapists)
  is held because saved bookings conflict, the preview and the save name the held centres
  and the children who got no new appointment. The save returns `complete: false`, its
  message starts with "INCOMPLETE", and a staff warning is stored even for an unsaved preview.
- **Warnings reopen.** A staff warning that was resolved reopens with fresh text when the
  same failure happens again. This includes sending warnings (paused, allowance,
  configuration, source record, deadline/retry limit, same-day contact). The one exception
  is "delivery overdue", which is re-checked every cycle: once staff dismiss it for a
  message, it does not reopen for that same unchanged message.
- **Bounded replacement search** (applies only to the opt-in automatic fill after a No;
  off by default since 11 November 2026). Every failure, including one while loading inputs, is
  recorded with its reason. A job is retried at most three times, then stops and stays in
  the staff inbox; resolving the item retries it once more. A schedule that keeps changing
  allows only a few rechecks. A job holds a 15-minute lease, and only the worker that still
  owns it may save suggestions or record a failure; a worker whose lease was taken over
  leaves the new owner's job untouched. "No replacement found" creates a staff item.
- **Allowance reasons.** Sending learns why it cannot send now: parent pacing or a send in
  flight, the 24-hour allowance, a provider pause, the per-second rate or a reply sent first.
  Only an exhausted allowance for a parent not already counted holds an offer for staff.
- **Named Zoho failures.** Each record Zoho does not save becomes a staff task naming the
  child, date, time and Zoho's reason. Saved records are kept; the batch stays open.
  Within a failed bulk update, records are retried one at a time so one bad record cannot
  block the rest. A task closes automatically once its record saves. Zoho's own
  explanation (for example "Doctor is inactive") is kept, not just its code. A bulk
  rejection is tied to a child only when the response proves its order; otherwise each
  unconfirmed child's task lists all of that batch's rejection reasons, marked as
  unmatched. The command-line push reports failures the same way; if the staff tasks
  cannot be created, the push error says so and lists the records.
- **Unsubscribe and subscribe.** A parent who replies exactly `unsubscribe` (any case,
  spaces ignored) is added to a stored list in the database with the date and their
  children. While listed, no offers or reminders are sent to that number; held offers show
  as `UNSUBSCRIBED`, are never assumed accepted at the cutoff, and staff see the list. A
  reply of exactly `subscribe` removes the number; later offers send normally. Offers held
  while unsubscribed go back to the queue only if never sent, still pending, every visit is
  still awaiting confirmation and none is in the past; the normal send checks (active
  patient, deadline, source sync, contact window) still apply. Others are closed as
  obsolete and not sent. The list is
  by phone number, so siblings sharing it are covered together. Both actions are audited.
  No confirmation message is sent and the child's visits stay scheduled (see questions).
- **Reminder time.** Sending starts at these times and is then paced by the queue; it is
  not a deadline for every message. Each unanswered offer gets one reminder, and the
  parent always has at least one hour with the first offer: an offer sent before 13:00
  is reminded at 14:00; one sent 13:00–15:00 at 16:00; a later offer (for example a
  substitute) one hour after it was sent, at any time of day. If that hour would end on
  the visit day, no reminder is sent. All existing reminder checks still apply.
  (`tests/test_therapy_schedule_template.py`)
- **Late reminders.** A reminder that is delayed until the visit day itself is not
  sent: its "confirm for tomorrow" wording would mislabel today's visit. It is closed as
  obsolete and the original offer stands. A waiting offer is never given a retry time in
  the past; if its sending time has already passed, it goes to staff instead.
  (`tests/test_therapy_schedule_template.py`)
- **Parent phone format.** Numbers are stored as 91 plus a 10-digit Indian mobile starting
  6–9. `9876543210`, `09876543210`, `+91 98765 43210` and `0091 9876543210` are converted.
  A number written with `+` or `00` keeps its country code: only `91` is accepted, so
  `+65 8123 4567` is flagged, never turned into an Indian number. Anything else, including
  foreign and landline numbers, is never guessed: that patient is set aside with a staff
  task while the other patients import.

## Selected production fixes — 8 November 2026

Local implementation; no deployment or live messaging is implied. Regression
coverage is in `tests/test_production_fixes.py`, the existing daily/absence/admin
reservation tests, import/contact tests and opt-in Zoho HTTP simulation tests.

- The first successful daily save records `daily_planning_started` in the audit
  log. Pre-start records or missing history never create catch-up demand. Current
  week saved visits count towards the existing prescription. Previous-week
  shortfall is considered only after a complete Monday–Saturday week falls on
  or after that baseline; an initial partial week does not fabricate a full-week
  debt. Daily runs do not reset the baseline. Weekly frequencies are not changed.
- When capacity is scarce, the fast daily planner favours the smallest share of
  the weekly visit target already booked, within existing absence/carryover
  priorities; CP-SAT includes a diminishing fairness preference. Patient IDs
  remain deterministic tie-breakers. This improves distribution, not a guarantee
  that all demand fits. Existing safety constraints remain mandatory.
- Saved explicit admin assignment, availability and absence warning overrides
  are recognised during validation using the recorded decision's warning codes.
  They do not permit newly generated assignment substitutions or remove overlap
  checks. A completed pinned therapy remains historical fact when a later
  therapist absence is recorded; new appointments still cannot enter the absence.
- Daily planning isolates conflicting saved bookings to independent resource
  components. A blocked centre retains its saved appointments, gets no new
  bookings/offers and is surfaced for staff review. Independent centres proceed.
  Centres sharing a patient or therapist stay coupled for safety. Validation
  findings for held components are retained in result statistics and unmet reasons.
- Parent Yes/No application and acknowledgment commit independently of repair.
  A No releases the agreed visit/day; the released visit stays on the calendar in
  red as "Needs rescheduling". **User decision (11 November 2026): nothing fills
  the opening automatically.** Staff open it, choose a child from the ranked
  Suggested list or by search, then send an offer or confirm without messaging
  (see "Ranked substitutes"). The former automatic replacement job is opt-in only
  (`fill_released_after_decline`); a job already queued in an existing database
  still completes, runs outside the workflow lock, limited to affected openings
  and centres, and its suggestions stay proposed until staff publish them. A
  changed snapshot is retried instead of overwriting newer work. Failed events have bounded retries, an inbox item and staff retry
  through resolution after correction. A repair failure never undoes a decline.
  Failed replies block exports only for the date to which they apply.
- Identified patient validation failures are quarantined while valid patients
  import. Existing appointments are retained, affected patients receive no new
  automatic offers, and the successful sync checkpoint does not advance. Correcting
  the row and replaying the same window clears its quarantine. Ambiguous identity,
  malformed report structure and timestamp-window failures still stop the import.
  A consultant can become inactive (set by staff in our app since 16 November
  2026) without erasing history or stopping the whole
  import; future saved bookings require staff review and new generation excludes
  inactive consultants. Missing contacts hold the affected offers for staff,
  rather than rolling back the whole calendar save.
- Zoho report pages request 200 records. Reads, writes and reconciliation share a
  conservative 48-starts/minute budget and one concurrent response. PostgreSQL
  deployments coordinate through the shared `zoho_api_call_budget` table and
  advisory lock; all replicas must use the same database/schema. The optional
  `ZOHO_BUDGET_DATABASE_URL` selects dedicated coordination storage. SQLite
  clients share `ZOHO_CALL_BUDGET_FILE` (default: a local temporary-directory
  budget); multi-host SQLite deployments require shared storage or one integration
  worker. Storage failures stop calls safely. HTTP 429 without Retry-After waits
  60 seconds, with at most 180 seconds total rate-limit waiting per read.
- Export reads are bounded by Scheduler_Key, never the whole historical report.
  Creates use up to 200 records; updates with identical field changes are grouped
  using explicit owned IDs. Workflows remain enabled. Acknowledgments are matched
  by key/ID, never response position, and persisted once per batch. Partial or
  uncertain outcomes are reconciled by owned keys; saved results are retained,
  unsuccessful rows remain resumable, and completion waits for all rows. Long
  pushes refresh tokens after 45 minutes when a refresh token is available.
- The shared WhatsApp dispatch store tracks unique offer recipients over a
  rolling 24 hours. `WHATSAPP_UNIQUE_PARENTS_PER_24H` defaults to 2,000 and must be
  set to the verified account allowance. Uncertain sends conservatively consume
  budget. Reminders to an already-counted parent do not consume a second unique
  recipient; acknowledgment replies are not blocked by this offer budget.
  **User decision:** offers exceeding capacity wait for staff review; there is
  no automatic patient priority policy. Once capacity is available, resolving
  their inbox item requeues them subject to validation and the sending deadline.
- Same-day replacements use date-specific session buttons only inside a verified
  contact window. Without that window they wait visibly for parent contact;
  they never use the approved daily template's incorrect tomorrow wording.
  Future offers wait until their supported sending day, and invalid/past-date
  offers create staff attention instead of silently disappearing.

Cancellation notifications, partial-visit policy, opt-out handling and app access
were not authorised in this selected change and retain their existing behaviour.
Provider allowances and production form workflows still require live/staging
verification; fictional HTTP tests cannot establish those account-specific facts.

## Prepared therapy template and timed reminders — 5 November 2026

**Timed sending is disabled.** On 6 November 2026, Meta accepted a one-off test
with three therapies on a single line. The timed workflow still needs end-to-end
validation before activation.

- The opt-in `therapy_schedule_v1` profile supplies positional variables in
  numeric order: 1 = centre; 2 = chronological therapy/time entries on one line,
  separated by spaces and prefixed with 🟥; 3 = `1 therapy` or `n therapies` matching the listed
  scheduled sessions; 4 = exactly `.` for the first request, or the bold reminder
  text for the follow-up. Actual saved slot times/durations are used unchanged.
- Both sends use the same configured approved template and the same offer token.
  The prepared profile expects Yes then No quick replies, without Help. Exact
  approved name, language, variable/button structure must be supplied/verified
  before enabling it. Switching an old queued offer to a different template
  profile is blocked rather than guessing missing parameters.
- When timed sending is explicitly enabled, offers saved for tomorrow are held
  until 09:00 IST on the preceding day. The worker queues at most one reminder
  per offer from 14:00 IST, before the existing reply deadline (late offers: 16:00
  or one hour after sending; see "Reminder time" above). Sunday targets
  are skipped; Sunday processing can prepare Monday offers. Missed worker ticks
  can be caught up later the same day; delivery is not guaranteed at an exact
  second. This change does not automate the midnight source pull/review/save.
- Only unanswered, unexpired, unchanged pending visits with an accepted original
  send qualify. Confirmed/declined/cancelled visits, failed/unknown original sends,
  changed contacts, and received replies still awaiting processing are excluded.
  Eligibility is checked again immediately before sending. A reminder does not
  create another booking, restart the reply deadline, or infer acceptance.
- The existing reply cutoff and cancellation/replacement rules are retained.
  Timing/profile controls default to off/legacy.

## Flexible Zoho fields and registration contacts — 5 November 2026

Implemented in `services/zoho_source_mapping.py`, the shared Zoho importer, and
the configurable main/test source providers. Covered by
`tests/test_zoho_source_mapping.py` and the optional local-HTTP checks in
`tests/test_zoho_source_http.py`:

- Source reports may include any extra columns and may change their order or
  remove unused columns. The import selects explicit stable field names/aliases;
  unknown fields are ignored and are not retained in the source cache. Missing
  required information, conflicting aliases or unsupported nested values are
  reported rather than inferred from names, row positions or similar columns.
- When a registration report is configured, each current plan's registration ID
  joins to the matching registration's Phone. That registration is authoritative
  even if a plan also carries a phone. Phone validation remains mandatory for
  referenced registrations. No phone is required for an unused registration.
- For repeated registration codes, the newest Added Time selects the current
  registration. Modified Time breaks equal creation-time ties if available.
  The existing newest-created treatment-plan selection remains in place. Two
  newest records with identical timestamps are ambiguous and block the import;
  an edit to an older record does not replace a newer-created record.
- Joined mode fetches complete paginated snapshots of plans, consultants and
  registrations on each sync. This detects a registration phone edit even when
  its plan is unchanged or no Modified Time field is exposed. Missing edit time
  is not fabricated or treated as proof that the source has not changed.
  Incremental two-report imports retain their existing timestamp requirements.
- Joined imports require a complete registration snapshot every time. Source
  validation, projections, contact changes, cache and checkpoint commit together.
  Failed imports retain existing bookings/contacts and create the established
  staff review item. Existing sent/uncertain-offer protections remain in force.
  Missing plans/consultants do not imply deletion or cancellation. No source
  status/deactivation or historical appointment-import policy is inferred here.

## Separate treatment courses — 28 October 2026

Implemented for the shared Zoho `Plan_Report` importer used by main operations
and the isolated test workflow; covered by `tests/test_treatment_courses.py`
and the incremental import tests:

- Registration ID remains the permanent patient identity. A different immutable
  Zoho plan record ID starts a separate treatment course, including when the
  therapies and assigned doctor match the previous course. `source_plan_id`
  groups that course's treatment requirements. Same-ID edits retain requirement
  IDs and progress; repeated imports do not duplicate a course.
- The newest course by Added Time determines current demand; Modified Time
  breaks creation-time ties and still controls incremental update retrieval.
  Editing an older course never replaces a newer-created course. Requirements from
  other courses become inactive without rewriting their original dates,
  duration, frequency, assignment or appointments. Old appointments and attendance
  stay attached to the old requirement; they never satisfy the new course.
  Future old-course bookings remain reserved and generate a staff-review warning;
  importing a renewal does not automatically cancel bookings or send offers.
- Retired requirements with saved bookings remain available to scheduling
  validation with zero new demand, including zero carry-over demand. Existing
  therapist, branch, availability, continuity and capacity checks still apply.
- Same-ID changes to a booked requirement's doctor or branch retain the existing
  staff-review guard. Adding a therapy to the same ID preserves progress for its
  other therapies. A new ID deliberately starts fresh progress for every therapy.
- The additive upgrade creates one nullable `source_plan_id` column and its
  index. It does not rewrite clinical rows. On the next import, active legacy
  requirements are associated with the previously cached plan ID before any
  incoming renewal is processed. First source adoption can reuse unbooked local
  requirements; unidentified local requirements with history keep NULL.
  Previously overwritten prescriptions cannot be reconstructed by this upgrade.
- Main and retained-store workers require the explicit
  `python -m main_workflow.migrate --apply` upgrade when this column is missing.
  Test-store initialization and normal `init_db` include the additive upgrade.
  Migration applies to the selected schema only. No live migration is implied.
- State appointment/progress entries include `requirement_id` and
  `source_plan_id`. Default weekly progress shows current requirements;
  `weekly_progress(..., source_plan_id=...)` can inspect a historical course.

## WhatsApp reliability — 28 October 2026

### Independent dispatch and provider outage controls — 28 October 2026

Implemented locally and covered by `tests/test_whatsapp_dispatch.py`,
`tests/test_whatsapp_http_simulation.py` and existing queue/concurrency tests:

- Web startup queues live offers/callbacks without starting a sender by default.
  `python -m services.whatsapp_worker --workspace main` runs the chosen live queue
  in an independent process. Explicit embedded mode is for local compatibility.
- Every live scheduling send reserves a shared allowance per business phone
  number: default five starts in a sliding second, with shared parent cooldown
  and in-flight protection. Every sender of that number must use the same control
  database. Control failure holds unsent jobs without consuming send attempts.
- Replies/receipts process before acknowledgments and new offers. The standalone
  worker has separate inbound, acknowledgment and offer lanes; a pending eligible
  acknowledgment takes the next available shared rate slot. Parent cooldowns,
  consent/contact checks, deadlines and booking validation still apply.
- Network calls hold sender-lane ownership, not the calendar/workflow mutex.
  Reset drains both sender lanes. Recovery checks both ownership locks without
  blocking; a timestamp alone cannot steal a send. A committed send-start marker
  is the final booking-check boundary: later calendar edits cannot recall a send
  already started. Late results do not restore a closed/confirmed offer deadline.
- Delivery receipts arriving before an HTTP result is stored remain queued for
  correlation while a matching recipient send is active.
- Provider/network failures involving three distinct parents within 60 seconds
  pause dispatch for 30 seconds by default. Only one eligible unsent/retry job
  probes recovery. Repeated failure reopens the pause; successful/recipient-only
  rejection closes it. A bad recipient/pair limit does not open a global circuit.
  Config/template errors retain the existing explicit configuration-resume rule.
- Circuit state and rate limits persist across restarts. UNKNOWN sends never
  become automatic retries or probe messages. An abandoned probe reservation
  expires after two minutes without requeueing its uncertain send.

See `WHATSAPP_WORKER.md` for shared control provisioning and coordinated rollout.
No live rollout or production verification is implied by local implementation.

Implemented and covered by `tests/test_whatsapp_reliability.py` and the opt-in
local HTTP checks in `tests/test_whatsapp_http_simulation.py`:

- Only explicit allowlisted temporary Meta rejections receive bounded exponential
  retries with jitter. Attempts and due times persist across restarts. Unknown
  acceptance is never automatically resent.
- Configuration failures pause outbound dispatch while received replies continue
  processing. Staff explicitly resume after fixing configuration; unknown sends
  remain for review.
- Cross-process locks protect recovery. Jobs known not to have started sending
  can recover; interrupted or legacy uncertain sends enter the staff inbox.
- Retry exhaustion and live sending deadlines require staff follow-up without
  changing bookings. Offers stop at the first visit start in IST; acknowledgment
  texts retain their existing send cutoff. Historical preview dates remain usable.
- Overdue delivery after the configurable default of 15 minutes creates one
  staff alert, never a resend. A terminal delivery receipt resolves that warning.
- Additive queue migrations preserve appointments and historical jobs. See
  `WHATSAPP_RELIABILITY.md` for settings, rollout and verification boundaries.

## Cancelled-session picker — 27 October 2026

- Clicking or double-clicking a released/cancelled appointment opens its replacement
  drawer immediately, with Suggested patients first. Empty suggestions leave Choose
  patient available for an explicit admin choice and existing warning review.
- Loading and failed requests are visible and retryable. An older opening/search
  response cannot overwrite the current opening or list. Repeated clicks while a
  patient's preview is pending issue one preview request. Selecting a patient only
  previews the reservation; the existing explicit confirmation remains required.
- Covered by `node --test tests/replacement_picker.test.cjs`, existing reservation
  API tests and browser checks. Scheduling eligibility and override rules are unchanged.

## Main application workflow and retained test data — 26 October 2026

The established OR constraints, objectives, slot configuration, validator and weekly
accounting are unchanged. The former two-start candidate shortcut has been removed.
The enabled main application serves
the complete tested flow at `/` and `/api/`; `/workflow` is a compatibility alias.
Real-data operations always require the main access key. The hosted no-key
setting is limited to retained test data. The existing `deploy_main.ps1` targets
that test installation. The initial real-data rollout uses `MAIN_WORKFLOW_LIVE=false`
until sending is explicitly approved.
Main planning and response repair use normal SOLVER_SETTINGS; the separate fictional
test flow retains its existing shorter search limit.

`app.py --test-data` selects the existing configured test store and uses the existing
Azure test worker for live messaging. It preserves records, IDs, source/export
checkpoints and stored mode, performs no DDL/seed/import on startup, and starts no
second sender. Native main APIs and signed webhooks cannot fall through to the old
main database. `/test` shares this runtime. The optional `--pause-messaging` flag
holds newly created outgoing jobs as WAITING_FOR_ENABLE. Existing outbound jobs are
unchanged merely by opening the app. These boundaries are covered by
test_main_root_workflow.py.

The enabled main and test workflows plan **one selected day only**, while counting
saved proposed, pending, confirmed and completed sessions in that Monday–Saturday
week. Staff review before saving; stale inputs require a new review. Saving
tomorrow's reviewed day and queueing its offers remain one transaction with
idempotent retries. Multiweek planning requests and pull-forward chain routes are
unavailable in these workflows. The browser generator defaults to tomorrow in
Asia/Kolkata and shows only that day's draft. Covered by
`tests/test_main_root_workflow.py`.

Replacement suggestions search all active patients' remaining weekly demand. Each
session a candidate takes must match one of the candidate's own unmet therapies with
the same assigned doctor, therapy and centre, in the candidate's therapy order; fall
within valid plan dates; respect patient/doctor availability and exact absence
intervals; and leave saved bookings and branch capacity intact. Pending
confirmations reserve weekly demand. Released/no-show records do not reserve demand.
Missing explicit availability rows use R-13's normal operating-hours default.
Suggestions never move a later visit, reserve a patient or send a message.

**Ranked substitutes (user decision, 11 November 2026).** Real multi-therapy plans
almost never matched the former exact whole-visit rule, so a No left every opening
without suggestions. Now:
- A substitute may need other therapies too and may take only part of them on that
  day; they are only being offered a substitute visit.
- A child already booked that day may take an opening directly before or after
  their visit when the result is one continuous visit (no lunch crossing, same
  centre). A separate later visit remains excluded (see D-01). Booking such a joined
  opening raises no "another visit" warning.
- The complete-visit checkbox lists only children who can take every released
  session. Unchecked, it lists everyone who can take the selected session, best fit
  first: children who can take the whole released visit, then those who can take
  more of its sessions, then this session only; ties follow waitlist priority, then
  the most sessions still owed this week. When nobody can take the whole visit the
  drawer switches to the per-session list. Each suggestion shows its fit.
- Staff still choose and confirm; nothing is offered automatically.

Covered by `tests/test_replacement_eligibility.py`, `tests/test_replacements.py` and
`node --test tests/replacement_picker.test.cjs`. Refresh eligibility when the scope
or saved data changes.
The isolated test schema has no main waitlist table; its absence means ordinary
priority. Existing main waitlist priorities are used when that table is present.
Suggestion reads do not create tables or migrate the selected database.

Main reset remains unavailable. Simulated replies are allowed only for a retained
preview test store, never a live test store or genuine main store. Test-data mode
retains `ft:` references and test-gokul export ownership; genuine main mode uses
`mw:` and its configured main target. Changing to genuine main records still requires
explicit five-table preparation, a separate key/operator/source contract and reviewed
identity mapping. Main source imports do not deactivate unrelated records or invent
parent numbers. A consultant-name/source-ID collision stops atomically for review.

Source timestamp reconciliation is an integration prerequisite for publishing and
manual finalisation. Unsent offers pause as WAITING_FOR_SYNC without a provider
attempt and resume only after successful reconciliation, using current contacts.
In a paused main app, resumed jobs remain held until explicit messaging activation.
Already-sent/uncertain offers keep their actual recipient. Failed delivery receipts
produce one attributed staff item with numeric provider codes, preserve booking
status and do not trigger automatic resends. Staff-cutoff acceptance is displayed
as assumed acceptance rather than an actual parent Yes. In the real-data
workflow, the cutoff requires a recorded send. Simulated offers stay pending
for staff review when live messaging is off. Test stores can still exercise
assumed acceptance using simulated sends. These guarantees are covered by
test_main_workflow.py and test_flow_test_pipeline_guards.py.

See main_workflow/README.md for contract prerequisites and future commands. Local
changes do not deploy new Azure code or activate timers. The existing Azure test
sender is enabled for the configured test-data workflow.

This is the plain-language source of truth for how the scheduler should behave.
Update a rule here before changing the scheduler.

## Cross-process daily workflow safety — 27 October 2026

- Daily workflow mutations and outbound processing share database/schema-scoped
  coordination. Competing application copies must acquire the same mutex before
  saving a reviewed calendar, reserving through the workflow, or processing jobs.
  Revalidate the reviewed input fingerprint after acquiring it; reject a stale
  save without partially saving appointments or offers.
- Queue jobs move from QUEUED to PROCESSING through a conditional database update;
  a job can have only one successful claim. Startup and surviving-worker recovery
  cannot reclassify a job while another coordinated worker is sending it.
- After interrupted processing, a durable send-start marker (or a legacy claim
  without ownership metadata) makes outbound acceptance uncertain: retain the job
  as UNKNOWN and create staff review, without automatic resend. A new owned job
  that never reached send-start can be safely requeued. Replay inbound
  and status processing transactionally. Callback persistence remains independent
  of slow outbound HTTP. Live recipient cooldowns are shared between workers.
- Existing staff-authorized warning overrides and clinical rules remain intact.
  Local source adoption, imports and booking changes do not gain new permissions.

Covered by `tests/test_workflow_concurrency.py` and the existing batch, readiness,
reservation and main-workflow regressions. See `CONCURRENCY.md` for deployment and
multi-replica preview-routing requirements. No database tables are added.

## Status key

- **Implemented:** The current application enforces the rule.
- **Partly implemented:** Some supporting behavior exists, but the full workflow is not ready.
- **Not implemented:** This is agreed behavior that still needs to be built.
- **Manager decision needed:** The application should not assume an answer yet.

## Test final schedule export (R-31)

**Implemented locally**, covered by `tests/test_flow_test_zoho_export.py` and the
real CLI/HTTP Postman scenario `postman/checks/verify_scheduler_push.py`.
The user authorized writes only to `nidhin.kumar_turnb/test-gokul`, form `Scheduler`,
report `Scheduler_Report`. Main/prod Zoho targets remain inaccessible to this path.

- The user chose a fixed daily cutoff eventually and **manual execution for now**.
  Explicit `zoho_push --date YYYY-MM-DD --apply` finalises that chosen date; no
  timer, automatic publication on save, or new Azure resource is introduced.
- The `/test` calendar also offers **Push to Zoho**. Its preview shows the chosen
  calendar date, unanswered sent visits and last successful push before confirmation.
  Confirmation runs the same resumable exporter, for all saved appointments on that
  date regardless of visible calendar filters. Unsaved previews are excluded.
  OAuth remains on the server; authenticated push requests release the workflow
  lock while contacting Zoho and overlapping calendar pushes are rejected.
  Missing credentials, scopes or unique-key fields stop before finalisation.
- Already-persisted queued parent replies are handled first. Explicit No removes
  that child's visit as before. An offered replacement uses its own appointment.
  Only sent unanswered offers can be assumed accepted. Help/Reschedule, unsent,
  waiting-for-contact, uncertain/failed-delivery and changed offers remain for staff
  review. Unpublished proposals are not automatically confirmed.
- Assumed acceptance confirms the local appointments and records `flow_assumed_yes`;
  export Status is `ASSUMED_CONFIRMED`. It never fabricates a parent Yes, sends a
  WhatsApp acknowledgement for silence, or marks attendance completed. Actual Yes
  uses `CONFIRMED`. Manual cutoff is explicit and independent of the ten-hour expiry.
- One outgoing row represents one therapy appointment. It carries Registration ID,
  patient name, date (`dd-MM-yyyy`), doctor and time (`hh:mm AM/PM`, IST), together
  with `Scheduler_Key` and `Status`. Zoho sets its own Added Time in the configured
  report; that creation field is not assigned or used as the sole change detector.
- Form preflight validates required link names, Single Line Status and a unique
  Single Line Scheduler_Key. The existing client needs create/read/update plus
  read-form-definition scopes. One-time user consent saves only a test-prefixed
  refresh token in ignored `flow_test/.env`, preserving the main token/settings.
- Stable namespace/appointment keys and payload fingerprints live in the existing
  test settings table. Repeat pushes skip unchanged rows, include older edited
  appointments, update previously exported moved/cancelled/released rows, and add
  replacement appointments separately. Unrelated remote rows are never modified;
  withdrawn history is retained with its status rather than deleted.
- A frozen resumable batch persists acknowledgements per row. The successful-push
  time advances only when the batch is complete, and cutoff/completion are displayed
  in IST. Failed or uncertain writes preserve the old completed checkpoint; unique
  key reconciliation prevents creating a second row after a lost response. Resuming
  an older batch is followed by a fresh check for intervening schedule changes.
- Source imports/daily generation still never export results. Local reset does not
  erase Zoho history; a new namespace prevents local ID reuse overwriting old rows.
  Storage remains 19 tables. Shared workflow mutexes coordinate local batch state;
  see `CONCURRENCY.md` for coordinated-worker rollout and preview-routing limits.

## Isolated daily coworker flow test (R-30)

**Implemented only in `flow_test/`; covered by `tests/test_flow_test.py` and
`tests/test_flow_test_storage.py`.**
The separate launcher seeds eight fictional patients and two fictional therapists
in an isolated test database. It reuses the existing 40-minute slot configuration
and deterministic `FLOW_TEST_SOLVER_SETTINGS` in `config.py`. Default preview
does not access Zoho. The separately authorized source importer and manual Scheduler
export are documented below; neither changes the main application's rolling horizon
or runs production migrations. SQLite remains the default preview storage.
PostgreSQL may share the existing database only through an explicitly selected
`flow_test` or `flow_test_preview` schema. Per-engine schema translation redirects
the shared ORM models without modifying their definitions or the main sessions.
The PostgreSQL test connection has no public search-path fallback. A provisioned
test login has schema-scoped access, with no access to existing scheduler tables.
The initializer creates only the 19 tables needed by the test workflow and
rejects unmarked populated namespaces or unrelated tables. Unqualified shared
database connections remain prohibited. Legacy dedicated `_flow_test` databases
remain supported. Main database initialization stays unchanged. The approved
bounded-search correction below preserves all clinical constraints.

The existing server also mounts the test at `/test`, serving the same HTML,
JavaScript, styles and UI handlers as the main application. The fixed Monday–Saturday calendar,
filters, appointment controls, preferences, directories, operations and the
generator screens run against the isolated test session. The shared test
generator uses the same one-day input builder as the standalone daily flow;
New slots are restricted to
one explicit Monday–Saturday date. Completed and saved past/future visits in
that week still count toward targets and stay fixed. Generating is read-only;
only selected-day appointments enter the preview/save payload. Saving is bounded
to that one date, so other-day pinned rows cannot be duplicated or rewritten.
Repeating generation/save does not duplicate existing visits. Weekly demand
remains distinct from the number of sessions on the selected day in the UI.
The standalone browser generator also limits new slots to tomorrow in
Asia/Kolkata and rejects rolling requests. Its save window is one day.
The test workspace uses Asia/Kolkata for its "today" counts; saving opens the date saved.
These boundaries are covered by `tests/test_flow_test_mount.py`. A test banner and
authentication dialog identify the workspace. WhatsApp actions extend the same
appointment drawer and Operations page: publish a continuous visit, view sending
and delivery separately from the validated parent reply, resolve Help requests,
and record per-therapy completed/no-show attendance after confirmation. Operations
supports a selected date, batch publication, replacement offers, queue history
and weekly progress. Open status panels refresh without sending. Preview-only
reply controls use the existing processor and drain the preview queue; live mode
hides these controls and rejects simulation server-side. Recipient/window checks
remain enforced by the backend. NO, HELP and RESCHEDULE are audited only after
their recipient, snapshot and event-time checks pass. Delivery never confirms a
visit. No new tables are required. Session selection uses a request-scoped
context, with a separate test preview store; neither is swapped globally.
Zoho/source import routes are blocked and the external assistant is disabled.
Unknown test routes never fall through to production. The standalone daily
flow and its APIs remain available. Default mode remains preview.
Test-only queue metadata is separate from production metadata. With explicitly
enabled mounted live mode, the signature-verified existing WhatsApp callback
routes registered parent/offer-recipient events for the configured business phone to test
storage; other contacts keep their existing flow. This is covered by
`tests/test_flow_test_mount.py`. The daily solver and scheduling rules are unchanged.

The admin selects an initial roster and one Monday–Saturday date. New proposals
are restricted to that date while saved bookings and completed sessions across
the same week count toward demand. Saved unrelated visits remain fixed. The
fictional roster is A–H, all eligible for the initial plan. Anu handles Speech
and Ravi handles OT across the existing configured working day (09:00–16:50,
with the existing breaks). No artificial child-time restrictions or fixed
appointment times are supplied. Missing child availability means all working
slots are permitted; OR assigns actual times. Explicit future family restrictions
remain supported. Centre capacity remains two simultaneous sessions.
A and E each require Speech and OT; the others require one therapy. A–D's
existing weekly frequencies are preserved. E needs Speech twice and
OT twice weekly; F/H need Speech twice and G needs OT twice. These are fictional
test inputs, not clinical policy. The v1/v2-to-v3 data upgrade adds missing E–H
once and removes the old fixture's availability restrictions. It retains existing appointments,
offers, replies, absences, attendance, patient requirements and audit history;
it neither plans appointments nor sends messages. Existing populated test stores
upgrade only from the recognized v1 marker, with the upgrade audited. Conflicting
new child codes require review rather than replacement. This is covered by
`tests/test_flow_test_expansion.py`; older fixed-time regression fixtures remain separate.

The user explicitly authorized a fresh start in the live isolated test schema.
An authenticated test reset with the exact confirmation `RESET flow_test` clears
old appointments, offers, queued jobs, parent responses, Help requests, attendance,
absences, contact windows, manual reservations and old audit rows. It reseeds only
the eight fictional inputs and two therapists, with a new reset audit and cutoff.
It never accesses the main/public schema or sends a message. It is serialized
with the test worker; cached previews are cleared. Old inbound callbacks at or
before the reset cutoff cannot restore erased replies or open contact windows;
receipts for deleted messages are ignored. Fresh START is required before sending
new session offers. Resetting database history does not delete messages already
delivered to WhatsApp phones. This is covered by `tests/test_flow_test_reset.py`.
Confirmed save policy for the shared `/test` calendar: saving the selected day
automatically queues offers for all still-proposed visits on that date **only
when it is tomorrow in Asia/Kolkata**. Saving today or a later date queues no
offers; there is no clock-triggered publication when that date becomes tomorrow.
Previewing still sends nothing. Existing pending/confirmed/completed visits are
not republished. One continuous visit receives one offer covering its therapies.
Saving appointments, creating offers and queuing jobs commit together; missing
parent phone bindings reject the save without partial bookings or messages.
Closed contact windows no longer block the save: those offers are committed as
WAITING_FOR_CONTACT while other parents' offers proceed. A new verified parent
message resumes the waiting offer. The save response includes the waiting count.
Before saving, a fingerprint of current weekly bookings, requirements, absences,
availability, preferences and branch capacity must still match the reviewed
preview. Changed inputs require regeneration; no stale appointments/offers are saved.
Retrying a successful preview returns its saved result without another message;
regeneration/save also skips previously published visits. If the date crosses
midnight and changes whether the preview would send, staff must refresh it.
The save control states whether it queues real or preview offers. This behavior
is covered by `tests/test_flow_test_mount.py`; live deployment is a separate step.
The standalone daily API and replacement drawer retain explicit publication.
Replacement proposals generated by a decline are not automatically messaged.
Each offer retains its appointment snapshot and recipient binding. Preview mode
uses the same queue but sends nothing externally.

Preview mode sends nothing externally and permits simulated replies. Live mode
requires sender credentials and a reachable authenticated webhook. Zoho-imported
patients use their validated Parent No. directly, without a separate number list
or per-patient local file entry. Optional local recipient bindings remain for
the standalone seeded fixture. These modes use different
databases or test schemas. Live storage settings never redirect the default
preview into live records. Only current parent contacts or recipients already bound
to saved test offers on the configured business number can supply
accepted incoming events, and the HTTP webhook verifies Meta's signature first.
The webhook commits incoming jobs before acknowledgment. Coordinated durable
workers take turns applying them, deduplicating callback IDs across restarts.

For the local main app using the live `flow_test` schema, real test offers are on
by default and the existing Azure test worker is the sole sender. Source imports
are forwarded through that worker so contacts update with the shared data; the
local app refreshes its roster and does not process sender jobs. A successful
source sync can release previously held offers after rechecking their recipients.
The `--pause-messaging` launcher option holds new outbound offers instead.
This behavior is covered by `tests/test_main_root_workflow.py` and the disposable
`postman/checks/verify_remote_sender.py` integration check.

Live coworker offers default to `FLOW_TEST_MESSAGE_MODE=session`: each coworker
sends START first, opening a 24-hour service window. Ordinary interactive
Yes/Cannot attend/Help buttons require no template within that window. The window
is bound to the coworker and receiving business number, using verified inbound
event time rather than callback receipt time. Old callbacks never advance it;
delivery receipts and outbound sends never open it. Admin publication validates
all recipients' phone bindings before saving offers. Closed windows create waiting
jobs; the worker rechecks the window before sending.
If a queued offer's window closes, the job waits as WAITING_FOR_CONTACT without
attempting a send. A fresh incoming coworker message resumes these unsent jobs.
The dashboard shows per-recipient readiness. START itself changes no appointment.
Session offers greet the explicitly configured coworker name, falling back to
the fictional patient's name. They state the published therapy, weekday/date,
centre and each session's Indian local time. A continuous visit lists all its
therapies in one message; names are never inferred from a WhatsApp profile.
The test label and visit reference remain present. Copy changes do not alter
appointment generation or reply-button payloads.
Optional `FLOW_TEST_MESSAGE_MODE=template` retains proactive offers through a
matching approved template; its configured three buttons are required only in
that mode. Session mode does not bypass template requirements outside 24 hours.

YES confirms the whole current visit. **Confirmed decision: Cannot attend / NO
means unavailable for the entire appointment date.** After offer/recipient/time
validation, it immediately stages explicit `day` responses for every unfinished
visit belonging to that child on that date, releases those bookings as
`NEEDS_RESCHEDULING`, and marks affected open offers `DECLINED`. There is no
visit/day follow-up question. Other children and dates remain unchanged; a shared
coworker phone does not broaden the absence to other fictional patients.
A repeated webhook cannot apply the same decline twice. Existing completed therapy
is never undone; a conflicting day declaration goes to staff review under the
unchanged response-service validation. Explicit legacy VISIT commands still retain
their supplied visit scope; DAY is equivalent to the new NO default. The decline
releases only its own visits, retaining pending/no-response visits and unrelated
bookings; staff choose any replacement (no automatic proposal since 11 November
2026). A staff-chosen replacement offer is reserved demand, not family confirmation. HELP/RESCHEDULE creates
an admin task without moving or releasing appointments. Unmatched/stale replies
are sent to the admin inbox. Delivered/read message receipts are not acceptance.
Offer snapshots and recipient binding reject replies to changed/closed visits.
The verified reply's event timestamp determines whether it met the deadline,
not the later processing time. A timely delayed reply may resolve an EXPIRED
offer if its booking is still retained; an explicitly CLOSED/released offer
cannot be restored. The corresponding expiry inbox item is resolved after a
timely confirmation/decline. Late events still go to staff review.
Response changes and follow-up jobs commit together or roll back together.

The configurable test offer deadline defaults to 600 minutes (10 hours) after sending.
Expiry flags staff follow-up and rejects late acceptance; it does not
automatically cancel the booking. An explicit admin action can release it.
The user-approved manual final-schedule export described below can instead assume
acceptance for a sent unanswered offer at the manual cutoff, with a distinct audit.
This test deadline does not answer any pending production response-policy
question. Ambiguous outgoing sends are retained as UNKNOWN without automatic
resending and create a deduplicated staff inbox item; unfinished incoming jobs resume on restart. Obsolete offers are not
sent. The test requires one running process per database.
The mounted and standalone workers send batches of up to five different parent
numbers concurrently (`FLOW_TEST_SEND_BATCH_SIZE`, 1–20). A batch contains only
one message per number; repeated sends to a shared phone are ordered and paced
six seconds apart in live mode. Every offer keeps its own durable send result
and reply deadline. Short workflow transactions serialize mutations; separate
sender locks span HTTP so calendar/reply processing remains available. Reset
waits for both sender lanes; HTTP threads never share ORM sessions.
An ambiguous failure is UNKNOWN and is not automatically resent. Preview draining
can still run one job at a time for deterministic checks. Batch sending does not
remove the provider contact-window requirement before sending. Inbound callback
persistence has a separate lock, so a slow outbound request cannot hold up the
webhook acknowledgment. Imports and resets coordinate with both locks.
Long session offers switch to compact wording without omitting any therapy or
time, and are checked against the existing 1,024-character body limit before queuing.

User-approved search correction: daily, replacement and unbounded weekly searches
consider every valid start in their respective planning boundary. The previous
two-start-per-day candidate thinning shortcut is removed.
A 100-child/10-consultant/two-branch regression fits all 100 daily sessions in
CP-SAT with short or extended test budgets; blocking one doctor-slot reduces that to 99. All
existing clash, lunch, availability, assigned-doctor and weekly-target rules remain.

Staff records COMPLETED or NO_SHOW per confirmed therapy, allowing partial visit
attendance. Completed sessions count as delivered; proposed/pending/confirmed
sessions count as reserved; released/no-show sessions count as neither. The next
daily run uses this updated same-week record. Automatic cross-week carry-forward
and production fairness/priority changes are outside this isolated test.

## Patient scheduling status

Staff can search for all patients in the directory and change scheduling status
from Edit. Deactivation requires confirmation and a written reason. The status
and timestamped audit entry commit together in the internal database. Reactivation
preserves previous reasons and treatment history. Zoho imports preserve existing
patient status and continue to load patient details and treatment plans.

Inactive patients supply no new scheduling demand, including carryover, and
cannot receive new manual or replacement bookings. A draft containing a new
booking for a now-inactive patient must be regenerated before saving. Deactivation
withdraws current and future proposals and offers that are still waiting to send.
Offers that may have reached a family go to staff review and are excluded from
assumed acceptance at the Zoho export cutoff while the patient is inactive.
A WhatsApp Yes received while the patient is inactive goes to staff review and
does not confirm the visit or send a confirmation.
Confirmed appointments and past attendance are retained. Existing replies,
attendance, and appointment outcome exports continue to use the existing
workflow. Patient scheduling status and reasons are stored internally.

## Confirmed operating rules

| ID | Rule | Status | Current behavior or work needed |
|---|---|---|---|
| R-01 | A child must not attend two different locations on the same day. If the treatment plan uses Location A and Location B, those therapies must be placed on different days. | **Implemented** | New therapy groups are split by location and placed on different days. The final safety check also rejects saved or manually moved schedules that put one child at multiple locations on the same day. |
| R-02 | Therapies during one visit should be continuous so the child does not have to wait between therapies. | **Implemented** | New visits are placed back-to-back in treatment-plan order. If an absence, cancellation or no-show removes a middle session from a saved visit, validation preserves the remaining bookings and verifies the gap against the original released slot. The released slot does not count as booked therapy. Covered by `tests/test_absence.py`. |
| R-03 | A continuous therapy group must not cross the lunch break, 12:35–13:15. Sessions may start at 13:15. | **Implemented** | Morning and afternoon are treated as separate blocks. The weekly calendar displays the actual lunch gap from the shared slot configuration, rather than shading the first afternoon slot. |
| R-04 | Every treatment requirement must be delivered only by its assigned therapist. | **Implemented** | Therapist continuity is a hard constraint. If the assigned therapist is absent, unavailable, inactive, or fully booked, the scheduler records the session as unmet and carries it forward where applicable; it never assigns another therapist. |
| R-05 | Generate tomorrow's draft only from the browser. | **Implemented** | Tomorrow is calculated in Asia/Kolkata. The generator counts saved visits for the full Monday–Saturday week and restricts new placements and preview saves to tomorrow. See `tests/test_web_app.py` and `tests/test_main_root_workflow.py`. |
| R-06 | Running the scheduler again with new data must not rearrange the existing weekly schedule unnecessarily. | **Implemented for saved appointments** | Proposed, pending, confirmed, completed, and manually changed appointments in the selected Monday–Saturday week are kept in place. Only unmet needs are placed into remaining openings. Unsaved previews are not permanent and may be regenerated. |
| R-07 | Manual changes made by management or an administrator take priority. The scheduler must preserve them and work around them. | **Implemented for saved manual moves** | Staff can move an appointment or its complete continuous group. The change is checked for clashes, recorded as manual, and protected during later scheduling runs. An explicit lock/unlock policy remains in `questions.md`. |
| R-08 | When a child or therapist absence releases an appointment, the affected requirement must receive priority without rearranging unrelated saved appointments. | **Implemented** | A daily draft gives priority to outstanding released requirements from the current week through its target day. Saved visits stay fixed. |
| R-09 | In the daily workflow, fill cancellations from eligible patients' remaining weekly demand without moving later appointments. | **Implemented** | Latest user correction: plan one day only and search all patients for matching doctor, therapy and centre, valid continuous visits and remaining weekly requirements. Staff select and review a replacement before reserving or offering it. Multiweek planning and pull-forward chains are excluded from the daily workflow. See replacement eligibility tests above. |
| R-10 | Schedule as many required therapies as safely possible and allow enough search time per independent centre. | **Implemented: five minutes per production centre/group** | Production daily planning always uses CP-SAT in Best Effort mode, with up to 300 seconds per independent centre/resource group, sequentially; shared patients/therapists keep centres coupled. Search returns early on proof of optimality/infeasibility. A timeout retains a validated FEASIBLE incumbent; no solution means no saveable new draft. There is no size-based greedy bypass. Background preview jobs keep long searches outside HTTP request lifetimes and outside the workflow lock. Explicit shorter fixture/legacy search choices remain CP-SAT and apply per component. |
| R-11 | A child added after the schedule was created may use a matching therapist’s free future slot without disturbing existing appointments. | **Implemented** | On the next run, the new child’s active treatment requirements are included, all saved active appointments remain fixed, and only suitable remaining openings are considered. |
| R-12 | Every unmet therapy should show the main reason it was not placed and any other constraints that contributed. | **Implemented** | The unmet-demand report now distinguishes therapist contention, location saturation, the one-visit-per-day limit, fixed availability or absence restrictions, continuous-block restrictions, and a search ending before an otherwise valid opening was used. This diagnostic does not change the generated timetable. |
| R-13 | Active therapists work every configured slot from Monday through Saturday unless they are marked absent; children may attend any configured slot. | **Implemented** | This is a manager-confirmed operating policy. It creates availability records for all active therapists and patients during import, without assigning a time preference. |
| R-14 | `Plan_Detail` is the source of demand for the active scheduler. | **Implemented** | The approved source sheets are persisted as `plan_detail`, `therapist_master`, and `specialty_master` tables. The scheduler projection is built from those tables: each valid row in a child's latest plan version creates a treatment requirement with its registration ID, therapy type, weekly frequency, assigned therapist, branch, duration, and plan start date. Exact duplicate rows and non-positive frequencies are excluded with import warnings. |
| R-16 | Therapy and specialty names must use `Specialty_Master` canonical values. | **Implemented** | The importer validates therapy types against the master mapping and stores its canonical specialty. A differing supplied specialty is normalized and logged for review. |
| R-17 | A patient absence or cancellation should show children who can fill the released appointment without breaking therapist continuity. | **Implemented** | Selecting a released appointment in Operations opens a dedicated **Possible replacements** drawer. Candidates need the same therapy, branch, and assigned therapist; must be available and not absent at the exact opening; and must still have an unmet weekly target. They may need other therapies too, and a child already coming that day qualifies only when the opening joins their visit. Children who can take the whole released visit are listed first, then those who can take more of it, then single sessions. Suggestions never reassign the therapist or book anyone automatically. |
| R-22 | A patient's recurring preferred day and morning/afternoon time band must influence scheduling without preventing therapy when another available time is the only safe option. | **Implemented** | The patient preference table stores Patient ID, preferred days (up to the plan's visits a week, or every day), and a morning/afternoon band or exact start time (16 November 2026); picked days are held for in daily planning (see the 16 November 2026 sections). The scheduler considers all valid candidate starts and rewards a visit by its start slot, favouring the nearest time to an exact preference. |
| R-23 | Give unmet weekly targets priority in the following week, without inventing pre-start history. | **Implemented** | For a fixed course (12 Nov 2026) the shortfall only adds priority; the course finishes later instead (see "Therapy courses end at a session total"). Daily drafts add a previous Monday–Saturday shortfall only when that full week is on or after the first saved daily-run baseline. Missing pre-start history adds nothing. Saved and completed sessions count toward the target; new placements remain on the selected date. Covered by daily capacity and production regression tests. |
| R-20 | Today's-session views must allow an operator to narrow the displayed appointments by branch, therapist, and patient. | **Implemented** | The dashboard filters the current day's loaded appointments in the browser and updates the displayed count. Filtering does not alter appointments or scheduling rules. |
| R-15 | Repeated visits should have a gap wherever capacity permits. | **Implemented as a preference** | The scheduler prefers even spacing—such as Monday, Wednesday, Friday for 3× weekly—but will use consecutive or wider-gap days when that is the only way to meet weekly demand. Therapies belonging to the same visit remain continuous. |
| R-18 | A child’s therapy must be delivered at the branch recorded on the treatment plan. | **Implemented** | The scheduler uses the `Plan_Detail` branch as the appointment location and only considers therapist capability at that branch. |
| R-19 | A therapy plan is schedulable only from its plan start date until its stated duration ends. | **Changed 12 Nov 2026** | Courses with a duration now end at their session total, not the calendar end (see "Therapy courses end at a session total"); the calendar end below applies only to plans without a duration. The scheduler rejects slots before the plan starts or after its calendar-month duration ends. Plans without a valid duration retain no expiry date and are logged during import. |
| R-20 | A cancellation or absence must not rearrange unrelated existing appointments. | **Implemented** | Only the affected appointment is cancelled or released for rescheduling. Replacement-child and pull-forward lists are advisory only; no other appointment is moved, cancelled, or rebooked unless an administrator explicitly makes and validates that change. |
| R-21 | Zoho Creator appointment sync uses the `therapy-scheduler` application's single flat schedule form. | **Implemented** | Each appointment is posted only to `All_Therapy_Schedules`, including its appointment date. Patient, therapist, therapy type, branch, and therapy plan are plain-text fields, so the sync never creates or depends on separate Creator lookup records. A one-time Zoho consent stores a local refresh token so restart reconnection is automatic. Bulk exports select unsynced active appointments only, send at most 200 in one Creator request, and store a local sync record only for Creator-confirmed rows. |
| R-22 | Creator Plan Detail imports must not overwrite the scheduler's live source data without explicit approval. | **Implemented** | A manual pull reads `Plan_Detail_Report` into PostgreSQL `zoho_plan_detail_records`, keyed by the immutable Creator record ID. Repeated pulls update only those staging rows. The live `plan_detail` source used by scheduling remains unchanged until a separately approved promotion workflow is added. |
| R-24 | A conversational scheduling assistant may interpret an operator request, but it must never make scheduling decisions or access scheduling data on its own. | **Implemented** | Azure OpenAI receives only the operator's current message and returns a constrained JSON action. The local scheduler calculates read-only daily, centre, therapist, therapy, patient, absence, rescheduling, available-slot answers, and a maximum of ten names from the last summary. If the model response is unavailable, the local application still handles a named patient summary, a named therapist availability request, and a greeting without sending or exposing scheduling data. A named available-slot request is scoped to the resolved therapist and lists only that therapist's unoccupied, available times; a general request returns the first openings across therapists. A named patient summary is scoped to that patient’s appointments and accepts the patient’s name, code, or displayed `Name · Code` label; it takes precedence over the prior-name-list follow-up shortcut. Therapy labels may omit a trailing “Session”; therapist lookups may omit a trailing source-system code only when that base name resolves uniquely, otherwise the assistant presents temporary-session choices and uses the operator’s number, full display name, or therapist code. A chat absence uses the existing full-day absence workflow: a patient’s released sessions include advisory replacements, while a therapist’s released sessions are listed for rescheduling. An ambiguous patient or therapist name requires an explicit temporary-session choice before any change. Browser-tab memory retains only the last summary scope and a pending choice for 30 minutes, and Clear memory removes that context and its visible conversation without changing the schedule. A cancellation still requires one uniquely matched active appointment; ambiguous or missing matches change nothing, and replacement suggestions use existing scheduling rules. |
| R-25 | WhatsApp webhook verification and test messages must not change appointments or expose a public send endpoint. | **Implemented for webhook receipt, backend test sender, Help acknowledgment, and button-click records** | Meta webhook verification returns its challenge only when the configured verify token matches. Incoming events require a valid `X-Hub-Signature-256` calculated with the Meta App Secret and are acknowledged without scheduling changes or logging message content. Authenticated template quick replies and interactive reply-button taps for the configured sender are retained in `whatsapp_button_clicks` with the WhatsApp profile name (nullable, not a verified patient identity), phone number, button label/payload, inbound message ID, receiving phone ID, and timezone-aware click/recording times (see R-26 for stored Indian-time columns). Repeated callbacks produce one record per receiving phone ID and message ID, including old taps that are no longer eligible for an auto-reply. Names and phone numbers are not written to application logs or exposed through a new public API. A fresh template Help button tap queues the exact reply "Do you want to reschedule or contact staff?" to the tapping user. A durable hashed event key prevents duplicate replies to repeated callbacks; only pending sends resume after restart, and expired events or ambiguous prior send attempts are not resent. Replying to this question does not automatically reschedule or notify staff. A trusted operator can run the backend-only test sender with a configured approved template and configured test recipient; it is not exposed through the browser API. Patient/guardian identity linking, consent, retention/reporting policy, appointment messages, and further action-button handling require a separate approved workflow. |
| R-26 | WhatsApp button-click records must retain Indian local time using Asia/Kolkata. | **Implemented** | The backend writes aware Asia/Kolkata timestamps. PostgreSQL keeps the original instants in timestamptz columns and physically stores database-generated `clicked_at_ist` and `recorded_at_ist` local timestamps, with `time_zone` set to Asia/Kolkata. Direct reports use the `_ist` columns without a second conversion. Startup migrates legacy UTC columns transactionally and idempotently, preserving each instant. The Help queue's UTC expiry calculations and all other scheduling timestamps remain unchanged. |

## Assumed parent-response simulation (R-27)

**Implemented for the isolated offline demo, not connected to WhatsApp or Zoho.**
`parent_availability_responses` holds assumed YES/NO replies linked to an appointment's
date and slot. A YES confirms the entire continuous visit without changing its time,
therapist, or branch. An appointment without a response is unchanged. A NO releases
the entire affected visit as `NEEDS_RESCHEDULING`. Each NO input must explicitly state
`visit` (the original visit's time range) or `day` (the whole appointment date);
the demo API retains explicit scope inputs; the connected test adapter now supplies
`day` for Cannot attend according to the confirmed rule above.

Only openings released on the selected Monday–Saturday date may receive new
replacement proposals. They must match the released therapist, therapy, branch,
and slot and pass the existing availability, absence, plan-date, capacity, and
continuous-group constraints. Inactive patients cannot receive new proposals.
Every unrelated saved appointment remains fixed, including no-response visits and
existing appointments later in the week. Replacement appointments are saved as
`PROPOSED`, never assumed to have family acceptance. Separate alternative suggestions
for declined families may use the remaining current week; they are not saved or booked.
The browser generator also limits new appointments to a single day.

Weekly progress is per treatment requirement: target minus completed sessions minus
existing proposed, pending, or confirmed sessions. A cancellation, no-show, or released
appointment does not count as delivered/booked. Unsaved alternatives do not reduce the
remaining count. New solver visits use distinct bundle identifiers so they cannot be
merged with a pinned visit from an earlier run. Replaying a processed reply makes no
additional changes; stale or conflicting replies reject the batch, and a solver failure
rolls back its changes. Covered by `tests/test_parent_responses.py`.

The isolated browser mode (`python app.py --response-demo`, loopback port 8766)
exposes this workflow with a deterministic 80-patient dataset, 12 therapists, two
branches, and three therapies. A button invokes the real scheduling service in a
background worker, with state/result views for original appointments, released slots,
tentative replacements, weekly counts, and unsaved alternatives. Repeated clicks
cannot start another run against consumed replies. Reset creates a new synthetic
database and retains earlier files; it never resets the configured application
database. The demo's 30-second deterministic CP-SAT settings live in `config.py`.
The dataset includes an opening with no eligible replacement, which stays empty.
Covered by `tests/test_parent_response_demo_workspace.py` and the existing
response-service tests.

## Local admin reservations (R-28)

**Implemented in the main replacement drawer and local demo; covered by
`tests/test_admin_reservations.py`, `tests/test_main_ui_reservations.py`, and
`tests/test_parent_response_demo_workspace.py`.**
In Operations → Needs rescheduling → Possible replacements, and for released
calendar appointments, opening the slot shows an admin
patient picker by default. Administrators may use the suggested patient or search
the full patient list. Confirm without messaging reserves the selected slot/visit
immediately as `CONFIRMED`; no WhatsApp or Zoho request is made. Confirm with
messaging remains disabled in the production drawer; the isolated `/test`
released-slot drawer supports **Send offer & wait for confirmation** as described below.

Therapy/assigned-therapist/branch mismatches, patient or therapist availability,
absence, inactive or missing plans, weekly frequency, capacity, conflicting bookings,
and splitting a continuous visit produce warnings rather than a scheduling-policy
block. An explicit Reserve anyway action authorizes saving despite the displayed
warnings. The reservation records the local admin identity and the warnings accepted.
Automatic solver suggestions retain their existing constraints; this override is an
explicit administrator action, not permission for the solver to bypass rules.

A continuous released visit is selected in full by default; an admin may choose one
slot after reviewing grouping warnings. The selected tentative suggestion is confirmed
in place or superseded by the chosen patient's reservation, without moving unrelated
appointments. A slot already reserved by another request must be refreshed; duplicate
submissions of the same request must not create another booking. Invalid references
and stale previews are input/concurrency errors, not clinical-policy restrictions.
An off-plan reservation occupies the saved time and does not satisfy an unrelated
therapy's weekly target. Generation and replacement search account for it the
same way as weekly progress. The assigned-therapist check still applies to every
new solver placement.
For a patient without any requirement row, an inactive zero-frequency technical row
marked `ADMIN_RESERVATION_ONLY` maintains the appointment relationship; it is not an
active clinical plan and creates no solver demand. Local reservations use the
additive `admin_booking_decisions` and `admin_slot_reservations` tables. Alternatives
from the original automatic run are hidden after a manual reservation because their
availability assumptions may be stale. The main scheduler uses its existing configured
database and the same reservation service through reservation-context, preview, and
reserve endpoints. Reserved source rows are hidden from the open-slot list and the
calendar; superseded tentative rows remain in history. Patient cancellations also
offer this workflow. Production admin booking does not send external updates.

### Test replacement offers

**Implemented; covered by `tests/test_flow_test_mount.py`.** In `/test`, choose a
patient for a released slot, review warnings, then use **Send offer & wait for
confirmation**. Any displayed warnings are explicitly overridden by that action.
The replacement, reservation tracking, offer and outgoing queue job commit together;
the visit becomes `PENDING_CONFIRMATION`, not confirmed. Reused solver suggestions
and newly created sessions share one complete-visit offer. Live mode requires a
registered parent recipient; a closed session window queues a waiting offer.
Failed validation leaves the original draft, opening and queue unchanged.
Retries return the original booking and offer, including after a reply, without
resending or resetting the status. Only a valid Yes reply confirms the visit.
Cannot attend immediately records whole-day unavailability, releases the affected
bookings and runs repair; Help enters the existing admin inbox. Delivery receipts never confirm.
Preview mode uses the same queue/reply handling without contacting WhatsApp.
This uses existing test tables and the shared OR constraints. Empty calendar
cells retain the existing without-messaging flow.

### Empty calendar cells

**Implemented; covered by `tests/test_empty_slot_reservations.py`.** Double-clicking
an empty weekly calendar cell (or focusing it and pressing Enter) opens the
same admin patient picker to create a new appointment. The date/time and, in a doctor
column, the doctor are prefilled. The admin selects or changes the doctor, therapy,
and branch, searches the original patient list, reviews warnings, then confirms.
This creates a `CONFIRMED` appointment without messaging or a fabricated cancelled
appointment. Opening the drawer and previewing write nothing. The tracking row points
to the new appointment itself; no additional schema change is required.
If that booking is later cancelled, it returns to the replacement workflow normally.
The 19-table test schema supports the doctor's primary department/branch without
needing extra mapping tables. Missing optional mapping tables grant no additional
capabilities or branch assignments; mismatch warnings still apply. This path is
also covered by `tests/test_flow_test_readiness.py`.

The same weekly-target, continuity, availability, and conflict warnings apply. Doctor
capability/branch mismatches and a date outside the Monday–Saturday week also warn.
Existing appointments hidden by filters still participate in conflict checks; this
flow never cancels or replaces them, including proposed appointments. Concurrent
changes require a fresh preview, and repeating the same confirmation is idempotent.
The admin may explicitly override the displayed scheduling warnings. A new single
session does not automatically create or move the patient's other therapies.

## Existing safety and capacity rules

These rules are already enforced by the scheduling engine, but the manager should confirm that they match real operations.

| ID | Rule | Status |
|---|---|---|
| S-01 | A therapist cannot treat two children at the same time. | **Implemented** |
| S-02 | A child cannot attend two therapies at the same time. | **Implemented** |
| S-03 | There is no separate room constraint. A therapist's one-child-per-slot limit is the practical capacity at each location. | **Implemented**; each location's stored ceiling equals its active therapist count, so it cannot limit bookings before the therapist rule does. |
| S-04 | A therapist who is marked absent cannot be booked during the absence. | **Implemented**; staff can record a full-day therapist absence in the manager interface and affected appointments are released for rescheduling. |
| S-05 | A therapist must be active and recorded as available for the time. | **Implemented** |
| S-06 | A child must be recorded as available for the time. | **Implemented**; the confirmed baseline records every child as available for all configured slots. |
| S-07 | Saved active appointments are kept in place during a new scheduling run. | **Implemented**; this includes proposed, pending, confirmed, completed, and manually changed appointments. |
| S-08 | Appointments use fixed 40-minute periods. | **Implemented** |
| S-09 | The working days are Monday through Saturday; Sunday is excluded. | **Implemented**; confirmed by management. |
| S-10 | If all requests cannot fit, the safe schedule is kept and missed therapies are reported rather than creating clashes. | **Implemented** in Best Effort mode. |

## Manager decisions still needed

### D-01: More than one visit in a day

Example: a child receives Speech at 9:00 and OT at 9:40 as one continuous visit. A 3:00 opening is also available that day.

**Decision needed:** May the child return for a second visit at 3:00, or must every child have no more than one visit per day? The current system allows only one therapy group per child per day.

### D-05: Which later appointments may be pulled forward after a cancellation

Proposed order for choosing a pull-forward replacement in the remaining days of the current Monday–Saturday week:

1. Look at suitable appointments on the next calendar day, then each later day in the current week, continuing only as far as management permits.
2. The child must be available at the empty time and must not have another appointment then.
3. The therapy and qualified therapist must match the empty slot.
4. Keep the child’s continuous therapy group together; do not move just one part and create a waiting gap.
5. Never move an administrator-locked appointment automatically.
6. Decide whether a confirmed family appointment may be moved automatically or only after family approval.
7. Prefer the move that fills the cancellation with the fewest changes to everyone else.

**Decision needed:** Confirm how many future days may be searched, whether confirmed appointments require approval, and whether the system should continue pulling later appointments forward to fill each newly created opening.

## Change-protection order

Unless management decides otherwise, the intended protection order is:

1. Administrator-locked manual appointments — never change automatically.
2. Confirmed appointments — keep unless an authorised person approves a move.
3. Previously saved proposed appointments — keep when unaffected; change only to respond to new data or fill a cancellation.
4. New, unsaved suggestions — may be freely improved.

This protection order is the agreed direction but is **not fully implemented**.

## Location-compatible splitting

When one treatment plan contains therapies from different locations, the scheduler now creates a separate continuous group for each location and places those groups on different days. It no longer rejects all therapies merely because the original treatment plan spans multiple locations.

## Cancellation example

A child scheduled on Thursday cancels. The intended workflow is:

1. Keep every unrelated appointment unchanged.
2. Show children who need the same therapy at the same branch with the same therapist and can attend the newly empty time.
3. Optionally check later appointments in the remaining week for a suitable pull-forward move.
4. Move the child’s complete continuous therapy group forward, subject to the protection rules above.
5. Recheck the schedule for clashes, availability, location capacity, and therapist absence.
6. Present the change for approval and record who approved it.
7. Optionally repeat the process for the new opening created on the later day, if management approves this cascading behavior.

## Zoho test input and daily draft — 22 October 2026

- The authorized read source is only `nidhin.kumar_turnb/test-gokul`, reports
  `Plan_Report` and `All_Consultants`. Import APIs are authenticated and mounted
  only under `/test`; the source job has no direct database connection and no Zoho write path.
- Confirmed test Schedule format: `therapy, sessions/week, duration in months,
  consultant`; `;` or a newline separates requirements. Entered order is retained.
  Consultants are matched by branch, name and consultation type. New branches
  are imported as separate test locations; identical consultant names at different
  branches do not collide. Text preprocessing normalizes Unicode width, spaces,
  tabs, newlines and invisible paste markers. Branch, consultant and therapy
  matching ignores case; Registration ID case is retained. Missing fields,
  ambiguous matches, invalid source IDs and malformed frequencies fail
  validation before any projection changes.
  Delimiter-only empty schedules and durations outside valid calendar dates are rejected.
- Confirmed revision (23 October 2026): Added Time and Modified Time are both IST.
  Each latest plan's Added Time supplies its treatment
  start date directly from its Asia/Kolkata date, without adding another offset.
  Modified Time still selects updates; it does not reset treatment start. The old
  shared `--plan-start`/`planStart` input is accepted only for compatibility and no
  longer overrides source dates. Calendar-month duration and bounds use the shared OR loader.
- The first run fetches all relevant rows before its cutoff. Later runs query
  Added Time OR Modified Time in `[last successful checkpoint, new cutoff)`.
  Criteria use Asia/Kolkata boundaries for both fields. Both are compared as UTC
  instants on import; the saved checkpoint remains UTC. For a 00:00 IST cutoff,
  both Zoho fields use 00:00 IST and the stored checkpoint uses 21:30 UTC on the previous date.
  All report pages must finish before import. Source deltas merge by Creator ID;
  missing rows mean no change, never deletion. Valid empty windows preserve data
  and advance the checkpoint. Data, source cache and checkpoint commit atomically;
  validation failures and stale concurrent runs cannot advance it.
- A checkpoint/source cache using an older time-zone contract requires one explicit
  `--apply --reconcile` pass. It fetches all rows before a frozen cutoff, merges
  by stable IDs without clearing data or deleting absent rows, and commits the
  field-specific time-zone marker/checkpoint with the import. Retained cached
  records are translated from the previous contract; source rows read during
  reconciliation take precedence because the earlier time-zone interpretation
  may have made their cached modification times appear too recent.
  Existing booking/history guards still apply. Incremental API requests must
  declare Added_Time=Asia/Kolkata and Modified_Time=Asia/Kolkata; old clients cannot silently
  advance the checkpoint. Structural failures preserve the old cache and checkpoint.
  Identified patient errors use the 8 November quarantine rules: valid patients
  may update while the checkpoint is retained for replay.
- The user removed the A–H/count/name-prefix fixture limits. Any valid Registration
  ID and any number of consultants may be imported from the authorized test app
  across the branches in that test app. Initial input needs at least one plan and consultant. A
  new or edited plan row for a known Registration ID uses the complete latest
  Added Time plan, with Modified Time breaking creation-time ties. Editing an
  older course does not make it current again. Distinct latest rows with
  both timestamps identical require review. The latest Schedule replaces the
  old current snapshot; old schedules are not unioned. Within the same source
  plan ID, unchanged therapy requirements retain their IDs and progress when
  a new therapy is added. A different source plan ID creates a separate course
  under the same patient (see treatment courses below). An existing source
  ID cannot change its Registration ID.
  Following original rule S-03, each imported branch's capacity equals its active
  consultant count (minimum stored ceiling one when no active staff, with no new
  appointments possible for inactive staff). There is no separate two-room limit.
  The seed-only legacy fixture still starts with two active therapists/capacity two.
- Imported source, scenario and sync metadata use test-only `flow_test_settings`
  records; no production schema changes or additional tables are introduced.
  Repeated imports retain patient/requirement identities within each source plan ID. Historical fixture
  appointments block switching sources until a deliberate fresh start. Frequency,
  order and date/duration changes on the same plan ID update the current requirement in place; saved
  appointments and completed progress keep their IDs and values. Thus 2/week →
  3/week with two existing bookings leaves one outstanding session. History-related
  changes produce deduplicated warnings in the import response and staff inbox.
  Reduced targets do not cancel excess bookings automatically. Removing a therapy
  with history sets future demand to zero and retains its historical assignment
  for fixed-booking validation. New doctor/branch assignments on requirements with
  history remain pending staff review, retaining the existing requirement while
  other valid records import; a warning recurs until that conflict is resolved.
  Consultant capability changes with history and Registration ID mutations
  still require deliberate review. Status-only deactivation preserves history and
  creates a booking-review item without blocking valid imports. Sync never resets
  or rewrites appointment history.
  Per the user's instruction, valid Parent No.
  values from the authorized Zoho test records supply the recipient bindings
  directly; no additional manually configured number pool is required. Imported
  contacts update the transport and webhook routing and survive restart without
  a local recipient file. When a source phone changes, pending offers that have
  not been sent follow the new number and wait for its contact window if needed.
  Sent or uncertain offers retain their original phone binding and continue
  receiving their own replies/receipts; they are not resent automatically.
  Failed imports do not activate proposed contacts. Unregistered arbitrary
  numbers cannot be sent to, and a different family cannot confirm an offer.
- The daily job imports through the running app, so its existing lock also
  serializes imports, saves, replies and worker activity. It prepares tomorrow's
  unsaved draft in India time and skips a Sunday target. The generator opens that
  draft with the existing review/save controls, refreshing against current bookings.
  Only an explicit staff save publishes offers. No sync or timer sends WhatsApp.
- The Azure job launcher defaults to a manual job and requires a successful live
  test import first. `-EnableDailySchedule` configures 00:00 IST (21:30 UTC).
  Deployment and a live test import are still required after these local changes.

Covered by `tests/test_flow_test_zoho.py`, `tests/test_flow_test_zoho_incremental.py`,
`tests/test_flow_test_zoho_contacts.py`,
`tests/test_flow_test_source_rules.py`, `tests/test_flow_test_send_batches.py`,
the shared API/read/reset tests, and a
local Postman HTTP simulation based on masked real test report responses.


## Zoho import and export recovery (28 October 2026)

Implemented locally; covered by `tests/test_zoho_retry.py`,
`tests/test_flow_test_zoho_incremental.py`, `tests/test_flow_test_zoho_export.py`
and the opt-in `tests/test_zoho_http_reliability.py` Postman HTTP checks.

- OAuth refresh, report pages and export schema reads retry connection failures,
  response transport interruptions and HTTP 408/429/500/502/503/504. Each read
  allows four total attempts, with exponential waits starting at one second plus
  jitter, and at most 45 seconds of cumulative waiting for non-429 errors (request
  timeout time is separate). HTTP 429 permits up to 180 seconds of waiting, with
  a 60-second cooldown when Retry-After is absent. Numeric and HTTP-date Retry-After values are respected. A provider
  delay exceeding the remaining budget stops the operation; it is never shortened.
  Permission, validation and malformed JSON failures are not automatically retried.
- A failed fetch never calls the importer with partial pages. Import projections,
  source cache and successful checkpoint still commit atomically. Structural or
  identity/window failures block the import and create a durable staff inbox item
  after rollback. Identified patient errors are quarantined while valid patients
  import; the checkpoint stays put. Staff correct Zoho and replay the same window.
  Corrected quarantine items close automatically. No checkpoint moves past invalid
  data, and quarantined patients receive no new automatic offers. No patient
  record bodies or credentials are logged.
- Exports retain their frozen batch, acknowledged rows and stable Scheduler_Key
  across interruption/restart. Zoho must enforce No duplicate values on that key;
  the existing schema preflight remains mandatory. On an uncertain/rejected write,
  the exporter re-reads Zoho and acknowledges only a single matching key with all
  expected field values and a valid record ID. This also handles a committed write
  whose HTTP response was lost. A missing, mismatched, duplicate or unreadable
  result stops the export with its unfinished batch retained; writes are not
  blindly retried. An explicit rerun re-reads Zoho before creating or updating rows.
  Completion time advances only after every frozen row is acknowledged. Existing
  finalisation, scheduling, messaging and attendance rules remain unchanged.

## Individual portal accounts (implemented; opt-in)

- With `PORTAL_AUTH_ENABLED=true`, staff sign in with an admin-created email/password
  account. Any email domain is supported; there is no public registration or SSO.
- Admins can use all centres and portal functions, create admins and clinician-linked
  doctors, reset passwords, activate/deactivate accounts, and read recent activity.
  The last active admin cannot be deactivated, including concurrent account changes.
- Doctors are view-only and can access only their own Today/Tomorrow sessions in
  India time. The server selects the clinician from the account and checks that the
  clinician remains active. Admin APIs, other schedules, patient directories,
  workflow aliases and the test workspace are denied to doctors.
- The doctor's Schedule tab shows only confirmed and pending-confirmation appointments
  for the selected Today/Tomorrow date. Cancellations and released-for-rescheduling
  appointments appear separately in Cancellations, so a cancelled slot does not
  appear alongside its replacement in the active schedule. Both lists remain scoped
  to the signed-in clinician. This changes presentation only, not appointment status,
  course counts, or booking rules. Covered by `test_doctor_schedule_separates_cancelled_slot_from_its_replacement`.
  A processed parent No therefore appears as "Cancelled" in the doctor's Cancellations
  tab, while retaining the internal needs-rescheduling status. Suggested/proposed
  replacements are excluded from the doctor's active schedule; staff must reserve or
  publish a replacement through the main app before it appears as pending or confirmed.
- Temporary passwords must be replaced at first sign-in. Passwords are hashed;
  sessions expire after 12 hours. Reset, password change or account status changes
  revoke existing sessions. Five failures per account in 15 minutes block sign-in
  temporarily; an independent client-IP limit also applies.
- Account changes, sign-ins and successful portal mutations are attributed to the
  signed-in email. Account setup does not enable messaging or change clinical rules.
- Portal login overrides legacy shared browser keys. Signed provider webhooks and
  direct database jobs remain separate; HTTP automation requires its own machine
  credential and cannot manage user accounts. Production rollout is explicit.
- Covered by `tests/test_portal_auth.py`; setup and rollout are in `PORTAL_LOGIN.md`.
