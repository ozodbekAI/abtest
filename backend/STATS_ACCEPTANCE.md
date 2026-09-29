# AB Test — Statistics / Attribution Acceptance Contract

This patch implements the latest agreed starting methodology from the 2026-09-28 Sergey clarification.

## Reconciliation contract

- Poll `fullstats` every 180 seconds by default.
- Do not complete the stage before 600 seconds have elapsed after confirmed pause.
- A settled stage requires 3 consecutive equal snapshots of views, clicks and spend, with no less than 180 seconds between snapshots.
- Stop reconciliation at 1800 seconds. If the counters do not settle, set `data_unstable`; retain uncertainty and allow the experiment lifecycle to continue rather than inventing attribution.
- While reconciliation is running, do not install the next photo and do not resume the campaign.

## Attribution contract

- Baseline is the cumulative campaign snapshot at the beginning of the stage.
- Final stage totals are taken from the final post-pause snapshot.
- Delta between baseline and final snapshot is attributed to the stage photo, including increments observed while the campaign remains paused.
- During the first transition window after the next photo starts, deltas that cannot safely be attributed are retained as `unallocated` rather than guessed.
- Raw provider snapshots are appended to the audit trail. Stage correction history is persisted in `media_state.stats_correction_history`.

## Winner contract

Winner is allowed only when:

1. every candidate variant meets the configured impression minimum (default 300);
2. every candidate variant is confirmed installed;
3. two-sided two-proportion significance is < 0.05 (95% confidence);
4. legacy minimum CTR gap of 0.35 percentage points is also satisfied;
5. the leader remains ahead under the configured worst-case allocation of all unallocated impressions/clicks.

Otherwise the result remains undetermined and the API exposes an explicit decision reason.

## Pilot

Run the controlled 60-minute pilot with the campaign paused:

```bash
python scripts/run_stats_settlement_pilot.py --test-id <ID> --minutes 60 --output pilot_stats.jsonl
```

The script is read-only for WB campaign/media state. It records provider status and raw `fullstats` snapshots every three minutes. Use its output to calibrate the 10–30 minute bounds.

## Separate-campaign alternative

The following tool calculates the deterministic budget portion already known by the application without inventing provider-specific values:

```bash
python scripts/evaluate_separate_campaign_option.py --photos 2 --views-per-variant 300 --cpm-rub 300
```

Minimum provider deposit rules, campaign start latency and remaining-budget settlement/refund semantics are intentionally reported as `UNKNOWN` until measured against the current WB contract/live pilot.
