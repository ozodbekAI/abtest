# Final audit-closure patch notes

This patch is based on the latest backend source and implements the remaining source-level items from the Sergey clarification and the latest photo/recovery findings.

## Implemented

1. 3/10/30 minute statistics reconciliation with 3 stable snapshots.
2. Persisted reconciliation state for restart/resume.
3. `data_unstable` after the 30-minute ceiling without silently inventing attribution.
4. Stage delta attribution from baseline to final post-pause snapshot.
5. Transition-window unallocated accounting after a new photo starts.
6. Raw provider snapshot audit trail + persisted correction history.
7. 95% two-sided two-proportion winner test.
8. Minimum 300-impression and 0.35pp CTR-gap guards retained.
9. Worst-case stress test for unallocated impressions/clicks.
10. Explicit winner decision reasons and API diagnostics.
11. Verified-photo requirement for winner.
12. Pending restore replay after worker restart/crash.
13. Restore verification uses previous/shadow media bytes to avoid false-positive confirmation.
14. Scheduler safety path finishes both campaign stop and media restore for active stale/failed campaigns.
15. 60-minute raw-statistics pilot tool.
16. Separate-campaign deterministic budget analysis tool that does not invent provider contract values.
17. Regression tests for the above behavior.

## Still requires live/operational evidence

- Wildberries provider behavior for budget API and delayed fullstats;
- real 60-minute pilot calibration;
- real CDN/storefront propagation;
- cross-process crash/recovery under real external mutations;
- final independent 64-requirement acceptance using the resulting evidence.

These are intentionally not marked CLOSED by code alone.
