# Out-of-band emergency stop

`ab_test_scheduler` normally stops unresolved campaigns, but a database outage
cannot be allowed to become an argument for leaving a paid campaign active.
For a known campaign use:

```bash
WB_TOKEN='YOUR_WB_TOKEN' CAMPAIGN_ID='12345678' \
  PYTHONPATH=backend python backend/scripts/emergency_stop_wb.py
```

The command does not use the application database. It sends `adv/v0/stop`, then
re-reads the campaign status until Wildberries reports a non-serving state. A
non-zero exit code means the stop was not confirmed and the incident must be
handled manually in the WB cabinet.
