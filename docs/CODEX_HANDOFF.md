# Wallet Intelligence continuation — 2026-11-06

## Current state

Branch: `codex/wallet-intelligence-continuation`.
HEAD: `fe955bcb99fcb33e4d1321b1802ed4ccefe2e037`, pushed to existing
`feat/explainable-scoring` (PR #78) without force-pushing.
PR #77 merged normally as `3553cd84a4e270e21765c0b9b43b73bcebef1201`.
PR #78 is now based on main (retargeted with REST because the installed gh edit
command's legacy Projects GraphQL query fails). Its ancestry includes all of #77.
No production deployment has been performed by Codex.

## Completed work

Recovered all pushed branches and audited issues #72, #11, #12, #73 and both PRs.
Original PR heads passed lint, test and Docker CI. No reviews were recorded.
GitHub applicable branch rules returned an empty list; the legacy protection endpoint
returned integration-access 403. Normal merge API accepted #77; no admin bypass used.

Confirmed a scoring regression with a failing test: a later tracking exclusion removed
that wallet from a historical score. Eligibility now uses first historical Top-100
appearance independently of current tracking status. The rule is included in the model
fingerprint; an already-registered old model requires a new version name, not overwrite.
Regression test and documentation added. Relevant suites: 100 passed in 39.89 seconds.
Ruff and mypy passed (51 source files). Full local suite is running as noted below.

GitHub progress updates:
- Issue #72 comment 6679276283: merged, deployment and dependent features pending.
- PR #78 comment 6679276603: correction, evidence and remaining requirements.
Neither issue was closed.

## Validation and environment

Local PostgreSQL 16 uses loopback port 5432; disposable database tracker_test.
Development database tracker_dev migrated to 0010. Production schema remains unverified.
Never run integration suites concurrently. Stop local dashboard before connection-limit tests.
After restoring/recreating PostgreSQL, set `ALTER SYSTEM SET jit = off` and reload config
to avoid query compilation overhead in tests.
Full local run log: `/tmp/elysium-full-recovery.log` (verify final result before citing).
New CI run: 42178490530 at commit fe955bc; lint passed, test in progress at last check.

Browser download now reaches cdn.playwright.dev but redirects to storage.googleapis.com,
which is denied. No policy changes were made during this continuation. Local browser tests
remain unavailable until that destination is allowed or a supported browser setup is provided.
PostgreSQL backup clients are also absent locally. Existing remote CI includes both checks.

## Production observation (2026-11-06 07:05 UTC)

Public /api/v1/health reported database ok, indexer heartbeat 1 second, menu heartbeat
3 seconds, latest accepted leaderboard 100 wallets, alert queue 0 pending/0 failed.
Cursor 1287774 versus sampled head 1287681 yielded lag -93. This is inconsistent sampling,
not a verified zero-lag measurement. Historical total, active historical-wallet count,
RPC latency/resource usage and Revival health remain unverified.
No production data, checkpoints, credentials or unrelated services were changed.

## Remaining requirements

PR #77 is merged, not deployed. Cluster-dependent dashboard capabilities remain pending.
PR #78 remains open. Verify new CI before merge and complete remaining correctness review.
Issue #11 includes components absent from the proposed five-component model: consistency,
early-entry, holding time, liquidity-adjusted exits and association penalties. Do not close
it merely because the initial model merges. Event-time replay is not proof of evidence
availability at the original signal time; late-ingested observations need explicit treatment.
Clustering #12 and Consensus v2 #73 remain unimplemented. Preserve v1 unchanged.

## Next exact steps

1. Read final full-suite output and CI run 42178490530; diagnose failures, not bypass them.
2. Complete #78 review, especially evidence availability and issue-scope gaps, then use normal
   merge only after the relevant checks pass. Avoid relabeling reconstructed scores as live knowledge.
3. Implement bounded, versioned clustering with transaction evidence and known-at timestamps,
   public-only memberships, common-exchange safeguards and correction/splitting history.
4. Add separate Consensus v2 using membership, score and cluster evidence available at signal
   time, measured amounts, raw counts and possible independent groups. Preserve v1 output.
5. Before deployment inspect production schema, verified recoverable backups, query plans,
   migration cost, rollback and resource budget through the existing deployment workflow.
   Never reset checkpoints/history, touch Revival, bypass backups or close #70's backfill concern.

## Final local regression result

Full recovered-branch suite completed: **606 passed, 23 skipped, 1 warning in 211.17s**.
Skipped checks are browser tests (Chromium unavailable) and PostgreSQL backup-client coverage.
CI run 42178490530 still had its test job in progress at the last remote check; lint passed.

## Release continuation

PR #78 merged after all exact-head CI passed: 4b22f82cff398f9570b21b29550d0123443ee7f3.
Release branch codex/release-wallet-intelligence merges main into the existing ops history.
Pushed 668c9de to ops: existing read-only tracking-baseline and tracking-verify steps.
Production preflight run: 42181484872. No migration/deploy action requested yet.
Backup run 42159757689 succeeded; encrypted artifact offsite-backup-20261106T035922Z
is unexpired. Full backup verification and current schema output still require log access.
GitHub Actions log downloads redirect to results-receiver.actions.githubusercontent.com,
blocked by current egress policy. Added that domain to the draft, retaining existing hosts;
not applied or published by the agent. Apply it before reading preflight/backup logs and
proceeding with migration. Deployment must recreate compatible tracker/UI images as needed
for schema 0010; web-only deploy does not activate the hourly scoring worker. Preserve
checkpoints and all data, and do not restart Revival. Use the established backup interlock.

## Production release verified — 2026-11-06 08:04 UTC

PRs #77 and #78 are merged and their implementation is now deployed.
First deployment 42184117298 stopped before migration because a running image was no longer
in Docker's image store. Recovery requires an available shared web rollback image and keeps
its tag; no data reset or volume deletion occurred.
Successful deployment: 42184698912, ops commit d162918. Backup gate passed with restore-tested
backup 42159757689 (27 PostgreSQL tables / 252878 rows verified). Schema migrated 0008 -> 0010.
Tracker, UI and web recreated and healthy; dashboard_ro permission checks passed.
Post-release read-only verification: 42185022146, ops commit 45f09e5.
At 08:04 UTC: historical 334, current 100, former 234; sampled scan lag 0, one RPC call,
0.23 seconds for 6 blocks. This is one observation, not a capacity guarantee.
Scoring first run stored 334 results as of 08:00 UTC: sufficient 26, limited 205,
insufficient 103. Model v1 includes historical-top-membership-v2 eligibility fingerprint.
Public /leaderboard and profile JSON return scores and component explanations.
Memory snapshots: tracker 50.39 MiB, UI 55.03 MiB, web 57.11 MiB, PostgreSQL 173.4 MiB.
Revival start time remains 2026-10-16T03:02:20Z, restart count zero.
Current history labels: 332 partial, 2 complete. No claim of complete historical backfill.
Clustering #12, Consensus v2 #73, and broader scoring requirements in #11 remain pending.
Ops changes enforce the backup gate for full deploy, preserve rollback image tags, use a
read-only status check instead of a Telegram test message, and fail on unhealthy services.
These operational changes are on ops; reconcile into main through review before future
main-ref deployment. Do not revert production schema to roll back the website; prefer a
forward fix or a schema-compatible image. Keep score history and checkpoints intact.

## Issue #79 completed — 2026-11-06

PR #80 merged normally as a95fed75083a7bcdebad5321e42ad754f55d6877.
Implementation head b8a2228a12357406585bb65eb4ca2fcd3c6ed46c.
Verified centralized card routes: Kleos /token/<address>, BOXA /token?address=<address>,
ElysPump /#/token/<address>; Montra and unknown venues stay plain labels/null URLs.
Activity uses existing polling only. Unique unread leg IDs update title and toolbar;
background/unfocused/scrolled arrivals remain pending until viewed or acknowledged.
Sound defaults OFF; saved preference requires a new gesture to unlock audio in each page.
Tones coalesce with cooldown and best-effort localStorage coordination across tabs.
Overflow requires reload without prematurely acknowledging missing rows.

CI 42188070639 passed lint/type/audit, all 643 tests (no skips), and Docker end-to-end.
Local full regression before final small refinements: 637 passed, 3 skipped; final Activity
suite: 101 passed using real system Chromium. Deployment guards: 9 passed.
CI disposable PostgreSQL database now disables JIT query compilation, reducing suite runtime
without changing assertions or production configuration. CI tests completed in 159.64s.

Web-only deployment 42188828700 succeeded at ops 7d28e21. Schema 0010 matched;
migrations were skipped and only elysium-web was recreated. No scanner logic changed.
Production /activity and API verified with actual Web Audio unlock, browser-intercepted
synthetic duplicate events (no production writes), title/acknowledgment and mobile rendering.
No page JS errors or mobile horizontal overflow. Desktop/mobile screenshots inspected locally.
Post-release check 42188992120 passed tracker/UI health and observed Revival scan-complete
logs with errors=0. Process start times unchanged before/after: tracker and UI 08:02:25 UTC,
Revival 2026-10-16 03:02:20 UTC; all restart counts zero.

Limitations: unverified venue routes intentionally unavailable; background notifications
follow browser throttling/existing polling cadence; cross-tab sound coordination is best-effort
and storage failures may allow duplicate tones. No desktop/push notification permissions.
Broader clustering/Consensus v2 work remains separate from this completed issue.

## Additional launchpad shortcuts — ready for approval

PR #81: d911dcf370edd51c29a8d3c1987a8ae98cf73057, currently OPEN/CLEAN.
Adds owner-supplied homepage shortcuts for Ascend, Montra, Obol, Volta, Eleos and Elyxa.
API launchpad_url_kind distinguishes platform homepages from verified token URLs.
SSR/live links have identical attributes and descriptive homepage tooltips. Unknown
attribution remains plain text. External inspection is blocked by cloud egress; no token
paths were inferred. Existing Kleos/BOXA/ElysPump token links remain unchanged.
Local Activity suite: 109 passed, with real Chromium. Ruff and mypy pass.
CI 42191139065 lint, test and Docker all passed, verified with fresh API job results.
Prepared ops/activity-more-launchpads branch merges the feature and current ops history.
Read-only baseline 42191262207 passed. Tracker/UI start times remain 08:02:25 UTC,
Revival remains 2026-10-16 03:02:20 UTC; restart counts zero.
Automatic approval review rejected the combined merge-and-web-deploy command before
execution, citing earlier Docker-in-progress status and unclear authorization for the exact
follow-up production rollout. Fresh CI confirms all jobs passed. No merge or deployment
occurred. Obtain explicit approval before retrying the merge and web-only rollout.
Use existing action=web, backup_run=42159757689 (check freshness again before execution),
matching-schema guard skips migrations. Compare bot start times after release.

## Montra/Obol routes and PR #81 released — 2026-11-06 09:40 UTC

The earlier approval blocker is resolved by the owner's explicit instruction to fix the PR,
wait for CI success, merge and deploy. Owner-provided token route examples verify Montra
/token/<address> and Obol /coin/<address>. These now use kind=token (addresses normalized
lowercase). Ascend, Volta, Eleos and Elyxa use the supplied kind=platform homepage shortcuts.
Unknown launchpad attribution remains unlinked. No token routes were inferred for the other four.

PR #81 implementation head 6a71e8632cbda84e97e3aaedf9257ff39da6252f merged normally
as 07b83f665d3cc7cb681cb79b36843bd39c6b1710 after all CI jobs passed.
CI 42195624565: 654 tests passed in 136.91s, lint/mypy/audit and Docker passed.
Local final Activity suite: 112 passed, including real Chromium SSR/live parity for
Montra/Obol token links and Ascend homepage links; exact supplied address unit tests passed.

Web-only deployment 42196380682 (ops 4e390ef) succeeded. Only elysium-web recreated;
schema 0010 matched, migration skipped. Live API and Chromium verified Montra /token/
and Obol /coin/ links, correct link tooltips, mobile layout, and no page JavaScript errors.
Actual Obol example /coin/0xbf85dcfdd9813d16e9c671afa1e1eaa99861fca7 observed in live API.
Preflight 42195765872 and post-release verification 42196646277 both passed. Tracker/UI
healthy; Revival logs show recent scans with errors=0. Start times unchanged before/after:
tracker 08:02:25.462890682 UTC, UI 08:02:25.251302434 UTC, Revival October 16 03:02:20 UTC;
all restart counts zero. Scanner logic, polling and database data were not changed.
