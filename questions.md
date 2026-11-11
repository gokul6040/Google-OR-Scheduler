# Open Questions for Management

This file contains only decisions that still need management confirmation. When a decision is made, move it into `rules.md` and update the application and tests in the same change.

## Scheduling decisions

22. **Thanal attendance/export statuses:** The destination offers Scheduled,
    Completed, Cancelled and Recreate. What should NO_SHOW and
    NEEDS_RESCHEDULING mean there? Until confirmed, hold these export rows for
    staff review. Assumed acceptance remains a local/WhatsApp detail and an
    already confirmed appointment exports as Scheduled. Do not equate a missed
    attended session with a merely scheduled future appointment.

17. **First partial operating week:** If the first run starts midweek, should the
    existing weekly prescription be reduced for that first week? Until decided,
    the prescription remains unchanged and only available remaining days can be
    booked. That partial startup week does not create an assumed full-week
    carryover deficit. No pre-start missed therapy is inferred.

1. **More than one visit in one day:** After a child finishes one continuous visit, may the child return later that day for another visit at the same location? Current behavior allows only one generated visit per day.

2. **Confirmed appointment changes:** May the system move a confirmed appointment automatically when filling a cancellation, or must it always ask an administrator/family first? Recommended: always require approval.

## Absence and manual-change decisions

13. **Manual lock duration:** Should an administrator's manual change remain locked until explicitly unlocked? Recommended: yes.

14. **Automatic changes and family approval:** Which changes initiated by the system
    require family confirmation before saving? Explicit local admin reservations
    without messaging are confirmed immediately, with warning overrides permitted
    by R-28; that decision does not authorize automatic changes by the solver.
    Separately, R-31 now records the user's approved manual-cutoff treatment of
    sent unanswered offers; that does not resolve the policy for other automatic changes.

15. **Source plan/patient deactivation:** Which Zoho field or explicit action means
    a patient or plan has stopped, and what should staff do with existing future
    bookings? Missing rows in an incremental pull are not deletions. Do not infer
    cancellation or erase completed progress from a missing source record.

16. **Main source adoption:** Confirm the actual production report/form link names,
    Schedule/registration Phone scalar or nested API shape, both source time zones, and
    the stable mapping between existing main consultant IDs and Creator IDs. The
    opt-in main adapter blocks a duplicate-name/different-ID consultant import;
    adopting those identities requires a reviewed data migration, not a therapist
    reassignment or relaxed scheduling constraint. The legacy flat forms are not
    assumed to match the newer Scheduler export contract.

## WhatsApp opt-out and contact decisions

18. **Unsubscribed child's visits:** When a parent replies `unsubscribe`, should the
    child stay scheduled with staff confirming by phone, or stop being scheduled?
    Current behaviour: visits stay scheduled; only messages are held.

19. **Unsubscribe confirmation:** Should the app reply once to confirm, for example
    "You've been unsubscribed. Reply subscribe to start again"? Current behaviour: no reply.

20. **Foreign parent numbers:** Should numbers with another country code be accepted
    when Zoho stores them with `+` and a country code? Current behaviour: flagged for staff.

21. **Night-time reminders:** A late offer (for example a substitute sent at 10:30 PM)
    is reminded one hour after sending, at any time of day, as agreed. Should there be
    quiet hours (for example no reminders after 9 PM)? Current behaviour: no quiet hours.
