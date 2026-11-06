# Open Questions for Management

This file contains only decisions that still need management confirmation. When a decision is made, move it into `rules.md` and update the application and tests in the same change.

## Scheduling decisions

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
