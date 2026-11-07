# PAPER-only Telegram pilot — prepared, not deployed

This is an operator review artifact for #82. PR #84 was merged externally without a
recorded independent security review. That does not authorize a pilot or restart.
The follow-up hardening PR, exact image digest, findings and rollback must be reviewed
independently first. Obtain explicit owner authorization for the maintenance window.
No signer, approval sender or broadcaster exists; the shipped PAPER flag remains OFF.

## Scope and isolation

Phase 1 replaces **only the existing UI container**, with a separately copied UI config
and persistent private journal. It uses the existing IntelBot getUpdates consumer; do not
start another UI instance, change the webhook, scale the UI or use a production token
in a test consumer. Scanner/tracker, notifier outbox, PostgreSQL, public dashboard and
Revival remain unchanged. The mount override adds no services or public ports.

Normal alerts continue from their existing scanner/config. Phase 1 uses private
`/trade TOKEN buy|sell` and `/paper_balances TOKEN`; existing alerts stay unchanged.
Enabling PAPER buttons in those alerts is a **separate phase**: their rendering flag is
loaded by the tracker, so that phase needs its own reviewed config/restart authorization.
Do not silently restart the scanner to make buttons appear.

## Preflight evidence required from operator/reviewer

- Independently reviewed exact code/image digest and unresolved-risk disposition from
  `PAPER_REVIEW.md`; explicit owner-approved UI-only window and pilot users/duration.
- Baseline tracker/UI heartbeats, scan lag, outbox pending/dead counts, normal alert delivery,
  PostgreSQL availability and Revival scan health. Record them privately; do not publish
  wallet associations, journal contents, bot tokens or database credentials.
- Save the current UI image digest and configuration permissions. Verify a rollback image
  exists locally. No production migrations or database resets are part of this pilot.
- Prepare a dedicated host directory owned by container UID 10001, mode 0700. Journal and
  backups must be regular 0600 files with one link. Protect ancestors against replacement.
  Keep the path on private local storage, separate from web/scanner mounts and Revival.
- Copy the current configuration into a **separate UI-only directory**. Preserve nontrading
  settings and allowlists. Review only these trading changes:

```yaml
trading:
  paper_enabled: true       # approved pilot UI copy only; shared scanner config stays OFF
  paper_order_path: /data/private-trading/paper.sqlite
  paper_rpc_url: https://rpc.agoradex.app
```

- Set `PAPER_UI_IMAGE` to the reviewed immutable image digest, and
  `PAPER_UI_CONFIG_DIR` / `PAPER_JOURNAL_DIR` to absolute private host directories.
  Render/inspect the merged compose configuration privately with the same production
  environment. Confirm only UI image/config/journal differ; never print interpolated
  credentials into CI or chat. The local `docker compose ... config -q` validation does
  not start containers.

## Authorized window procedure (do not execute without approval)

1. Pause PAPER if any previous pilot exists. Preserve the journal. If backing up a live
   SQLite file, use SQLite's backup API; do not raw-copy a database while it is writing.
   Validate the backup with `PRAGMA integrity_check`, protect it as 0600 and rehearse
   opening a private copy with the reviewed image. No production PostgreSQL action.
2. Recreate **only the existing UI service** with the reviewed image and override:
   `docker compose -f docker-compose.yml -f deploy/paper-pilot.compose.yaml up -d --no-deps --no-build ui`.
   This is the single authorized restart; no compose-wide up/down/build or deploy workflow.
3. Confirm exactly one UI/getUpdates consumer, no 409 conflict and fresh UI heartbeat.
   Confirm scanner/outbox/PostgreSQL/Revival baselines remain within approved thresholds.
4. Allowlisted pilot user sets `/paper_wallet PUBLIC_ADDRESS`; wallet ownership remains
   unverified. Test private balance reads, supported V2 BUY, conditional SELL without
   allowance, amount/refreshed quote/replay/expiry/cancel/pause and private `/orders`.
   Use a custom amount/percentage if real balance or impact caps refuse presets.
5. Check conditional labels: no actual fill, no approval sent, no successful SELL simulation
   or gas estimate when allowance is missing. Test unknown/non-V2 tokens refuse, with the
   existing verified DEX link retained. Never grant allowance to make the pilot pass.
6. Verify an approved UI restart retains private wallet/history but invalidates old intent
   and confirmation callbacks. Recheck regular alerts and both bots throughout the window.

## Stop and rollback

Stop criteria: missing UI/scanner heartbeat, increasing alert failures/lag beyond the
approved baseline, duplicate consumer, private data in a group/public view, unbounded
resource/RPC use, corrupted/insecure journal or a misleading execution/simulation label.

Pause PAPER immediately; cancellation/history remain available. Disable PAPER in the
UI-only config and recreate only UI with the saved reviewed rollback digest and config.
Preserve the journal/backup for investigation; do not delete, truncate or reset records.
Both versions use the same additive PAPER tables; validate compatibility on a backup
before the window. Restoring/replacing a journal requires the UI stopped and a fresh
journal instance because hardening pins its file identity. Recheck all baselines.

No production deploy/restart, live funds, keys, approvals or broadcasts were performed
while preparing this plan. Stage C remains a separate independently reviewed issue #85.
