#!/usr/bin/env bash
# Approved maintenance steps for the VPS, run over SSH by the deploy workflow
# (action=maint, steps=a,b,c). Every step names its exact targets; there are no globs
# outside fixed directories, no `docker system prune`, no volume removal, and the live
# databases are never touched. Each step prints what it did and the disk before/after.
#
#   ssh vps 'bash -s -- pause-revival-backup-timer,stop-old-website' < scripts/vps-maint.sh
set -uo pipefail
export LC_ALL=C
STEPS="${1:?steps}"
REPO="$(cd "${2:-elysium-wallet-tracker}" 2>/dev/null && pwd || true)"   # code bundle uploaded by the workflow
CADDYFILE=/opt/gmgn-revival-radar/deploy/Caddyfile
CADDY_C=gmgn-revival-radar-caddy-1
CADDY_BAK="$CADDYFILE.pre-elysium"
LIVE_DB=/opt/gmgn-revival-radar/data/revival_radar.db
KEEP_DAILY=/root/onchainflow-backups/daily/daily-20261101T190022807644Z.db   # newest copy stays

say() { printf '\n=== %s\n' "$*"; }
disk() { df -B1 --output=used,avail / | awk 'NR==2{printf "disk: used %.2f GB, free %.2f GB\n", $1/1e9, $2/1e9}'; }
open_by_any_process() {  # 0 if any process holds a file under $1 open
  find /proc/[0-9]*/fd -lname "$1*" 2>/dev/null | grep -q .
}
del() {  # del <path>: remove one approved file or directory, refusing anything unexpected
  local p="$1"
  case "$p" in
    "$LIVE_DB"|"$LIVE_DB"-wal|"$LIVE_DB"-shm|"$KEEP_DAILY") echo "REFUSED (protected): $p"; return 1 ;;
    /root/onchainflow-backups/*|/root/onchainflow-releases|/root/onchainflow-stage|/opt/gmgn-revival-radar-backups|/opt/gmgn-revival-radar/data/revival_radar.before-upgrade-*) ;;
    *) echo "REFUSED (not in the approved list): $p"; return 1 ;;
  esac
  [ -e "$p" ] || { echo "already gone: $p"; return 0; }
  if open_by_any_process "$p"; then echo "SKIPPED (open by a process): $p"; return 1; fi
  local sz; sz=$(du -sb "$p" 2>/dev/null | cut -f1)
  rm -rf --one-file-system -- "$p" && awk -v b="$sz" -v p="$p" 'BEGIN{printf "deleted %8.2f GB  %s\n", b/1e9, p}'
}
healthy_bots() {
  echo "containers:"; docker ps --format '  {{.Names}}: {{.Status}}' | sort
  echo "revival bot, last scan lines:"; docker logs --since 15m gmgn-revival-radar-radar-1 2>&1 | grep -E 'scan complete' | tail -2 | sed 's/^/  /'
  echo "elysium tracker:"; docker exec elysium-wallet-tracker python -m agora_tracker --health 2>&1 | tail -1 | sed 's/^/  /'
}

disk
for step in ${STEPS//,/ }; do
  case "$step" in
    pause-revival-backup-timer)
      say "pause onchainflow-backup.timer (reversible: systemctl enable --now onchainflow-backup.timer)"
      systemctl disable --now onchainflow-backup.timer 2>&1
      systemctl reset-failed onchainflow-backup.service 2>/dev/null || true
      systemctl is-enabled onchainflow-backup.timer 2>&1; systemctl is-active onchainflow-backup.timer 2>&1
      ;;
    stop-old-website)
      say "stop the old website (container kept for rollback: docker start gmgn-revival-radar-web-1)"
      docker stop gmgn-revival-radar-web-1 2>&1
      systemctl disable --now onchainflow-health.timer 2>&1
      echo "web: $(docker inspect -f '{{.State.Status}}' gmgn-revival-radar-web-1 2>&1)"
      echo "health timer: $(systemctl is-enabled onchainflow-health.timer 2>&1)/$(systemctl is-active onchainflow-health.timer 2>&1)"
      echo "public site now: $(curl -s -o /dev/null -w '%{http_code}' -m 10 https://onchainflow.app/ || echo '?') (502 expected until the dashboard cutover)"
      healthy_bots
      ;;
    delete-copies)
      say "delete approved copies of the revival database (live DB and newest daily copy are protected)"
      [ -f "$LIVE_DB" ] || { echo "live DB missing: aborting"; exit 1; }
      [ -f "$KEEP_DAILY" ] || { echo "newest daily copy missing: aborting"; exit 1; }
      for f in daily-20261026T190024108651Z daily-20261027T190010508862Z daily-20261028T190000745550Z \
               daily-20261029T190009985200Z daily-20261030T190001060258Z daily-20261031T190015537144Z; do
        for suffix in .db .db-shm .db-wal; do del "/root/onchainflow-backups/daily/$f$suffix"; done
      done
      del /root/onchainflow-backups/daily/daily-20261102T190001246454Z.db-journal
      rm -f /root/onchainflow-backups/daily/daily-20261101T190022807644Z.db-shm /root/onchainflow-backups/daily/daily-20261101T190022807644Z.db-wal  # empty side files from the audit
      del /root/onchainflow-backups/release-20261012T130446Z
      del /root/onchainflow-releases
      del /root/onchainflow-stage
      del /opt/gmgn-revival-radar-backups
      for f in /opt/gmgn-revival-radar/data/revival_radar.before-upgrade-2026100*Z.db; do
        [ -e "$f" ] && del "$f"
      done
      echo "remaining in daily/:"; ls -la /root/onchainflow-backups/daily/
      echo "live DB intact: $(stat -c '%s bytes, modified %y' "$LIVE_DB")"
      healthy_bots
      ;;
    prune-build-cache)
      say "docker builder prune (build cache only; images, containers and volumes untouched)"
      docker builder prune -f 2>&1 | tail -1
      ;;
    remove-old-image-tags)
      say "remove superseded revival image tags (current :latest and v2-cb3401f rollback images kept)"
      for img in gmgn-revival-radar-web:website-release gmgn-revival-radar-radar:website-release \
                 gmgn-revival-radar-radar:before-website gmgn-revival-radar:before-market-report-20261010T033424Z \
                 gmgn-revival-radar:before-market-report-20261010T032731Z gmgn-revival-radar:before-helius-20261009T164648Z \
                 gmgn-revival-radar:audit-5b0f6df gmgn-revival-radar:rollback-dd14651-20261008; do
        docker image inspect "$img" >/dev/null 2>&1 || { echo "not present: $img"; continue; }
        docker rmi "$img" 2>&1 | tail -1
      done
      ;;
    cap-journald)
      say "cap the systemd journal at 200 MB"
      mkdir -p /etc/systemd/journald.conf.d
      printf '[Journal]\nSystemMaxUse=200M\nSystemKeepFree=2G\n' > /etc/systemd/journald.conf.d/50-size-cap.conf
      systemctl restart systemd-journald
      journalctl --vacuum-size=200M 2>&1 | tail -1
      journalctl --disk-usage
      ;;
    install-revival-health)
      say "install the Revival scan-health monitor (read-only; replaces the retired onchainflow-health)"
      [ -f "$REPO/scripts/revival-health.py" ] && [ -f "$REPO/deploy/systemd/revival-health.timer" ] \
        || { echo "code bundle missing under $REPO"; exit 1; }
      install -d -m 0755 /opt/elysium-ops
      install -m 0755 "$REPO/scripts/revival-health.py" /opt/elysium-ops/revival-health.py
      install -m 0644 "$REPO/deploy/systemd/revival-health.service" /etc/systemd/system/revival-health.service
      install -m 0644 "$REPO/deploy/systemd/revival-health.timer" /etc/systemd/system/revival-health.timer
      systemctl daemon-reload
      echo "scan_runs columns and newest rows (no secrets in this table):"
      python3 - "$LIVE_DB" <<'PY'
import sqlite3, sys, urllib.parse
c = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(sys.argv[1]), uri=True, timeout=5)
c.execute("PRAGMA query_only=ON")
print("  columns:", [r[1] for r in c.execute("PRAGMA table_info(scan_runs)")])
for r in c.execute("SELECT id, started, finished, duration_seconds, processed, errors, sent, sources_ok FROM scan_runs ORDER BY id DESC LIMIT 3"):
    print("  ", r)
PY
      echo "dry evaluation:"; python3 /opt/elysium-ops/revival-health.py --no-alert --json </dev/null || true
      echo "telegram delivery check:"; python3 /opt/elysium-ops/revival-health.py --test-telegram </dev/null || true
      systemctl enable --now revival-health.timer 2>&1
      systemctl start revival-health.service 2>&1 || true
      echo "timer: $(systemctl is-enabled revival-health.timer)/$(systemctl is-active revival-health.timer)"
      systemctl list-timers revival-health.timer --no-pager 2>&1 | head -3
      journalctl -u revival-health.service -n 5 --no-pager 2>&1 | sed 's/^/  /'
      echo "old website health timer: $(systemctl is-enabled onchainflow-health.timer 2>&1)/$(systemctl is-active onchainflow-health.timer 2>&1) (must stay disabled)"
      ;;
    probe-kleos-pro)
      say "read-only probe: Kleos new RPC / site domain (.pro) vs the current endpoints"
      rpc() { curl -s -m 15 -H 'content-type: application/json' -d "$2" "$1" || true; }
      hexn() { python3 -c 'import json,sys
try: print(int(json.loads(sys.stdin.read())["result"],16))
except Exception: print("n/a")'; }
      for u in https://rpc.thekleos.pro/rpc https://rpc.thekleos.xyz/rpc https://rpc-live.boxa.club https://rpc-live.elyspump.com; do
        cid=$(rpc "$u" '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' | hexn)
        t0=$(date +%s.%N)
        head=$(rpc "$u" '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}' | hexn)
        dt=$(python3 -c "import time;print(round(time.time()-$t0,3))")
        for n in 20 50; do
          body=$(python3 -c "import json;print(json.dumps([{'jsonrpc':'2.0','id':i,'method':'eth_blockNumber','params':[]} for i in range($n)]))")
          ok=$(rpc "$u" "$body" | python3 -c 'import json,sys
try:
    d=json.loads(sys.stdin.read()); print(sum(1 for x in d if isinstance(x,dict) and "result" in x) if isinstance(d,list) else "reject")
except Exception: print("reject")')
          eval "b$n=$ok"
        done
        echo "  $u chainId=$cid (want 99901) head=$head latency=${dt}s batch20_ok=$b20 batch50_ok=$b50"
      done
      echo "  (heads within a few blocks of each other => latest-state node; hours behind => batch-confirmed)"
      tok=$(docker exec elysium-postgres psql -U tracker -d tracker -X -A -t -c "SELECT token FROM trades ORDER BY id DESC LIMIT 1" 2>/dev/null | head -1)
      for u in "https://www.thekleos.pro/" "https://www.thekleos.pro/token/$tok" "https://thekleos.pro/token/$tok" "https://www.thekleos.xyz/token/$tok"; do
        r=$(curl -s -o /dev/null -m 15 -w '%{http_code} final=%{url_effective} ssl=%{ssl_verify_result}' -L "$u" || echo fail)
        echo "  site ${u%$tok}<token> -> $r"
      done
      ;;
    activity-filter-probe)
      say "Activity HYPE filter probe (read-only, via the dashboard container's loopback port)"
      docker exec -i elysium-web python - <<'PY' 2>&1 | sed 's/^/  /'
import json, urllib.error, urllib.parse, urllib.request
from decimal import Decimal
B = "http://127.0.0.1:8100/api/v1/activity"
def get(**q):
    try:
        with urllib.request.urlopen(B + "?" + urllib.parse.urlencode(q), timeout=20) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None
base = get(range="7d", limit=100)[1]
print("unfiltered 7d: total", base["total"]["count"], "capped", base["total"].get("capped"))
for lo, hi in (("0.5", None), ("5", None), ("1", "10"), ("100", None)):
    q = dict(range="7d", limit=100, min_hype=lo)
    if hi: q["max_hype"] = hi
    st, j = get(**q)
    amts = [Decimal(i["amount_hype"]) for i in j["items"]]
    ok = all(Decimal(lo) <= a and (hi is None or a <= Decimal(hi)) for a in amts) and \
         all(i["basis"] in ("verified", "estimated") for i in j["items"])
    print(f"min={lo} max={hi}: status {st} total {j['total']['count']} page {len(amts)} bounds+basis {'OK' if ok else 'VIOLATION'} echo {j['filters']['min_hype']}/{j['filters']['max_hype']}")
for bad in ({"min_hype": "-1"}, {"min_hype": "1e3"}, {"min_hype": "10", "max_hype": "5"}, {"max_hype": "x'--"}):
    print("invalid", bad, "->", get(range="7d", **bad)[0])
PY
      ;;
    tracking-verify)
      say "tracking verification (read-only): reconciliation report, scan_stats, resources, 429s"
      docker exec elysium-wallet-tracker python -m agora_tracker --config /config/config.yaml --tracking-report 2>&1 | tail -60
      docker exec -i elysium-postgres psql -U tracker -d tracker -X -A -F ' | ' <<'SQL' 2>&1 | sed 's/^/  /'
\echo -- alembic
SELECT version_num FROM alembic_version;
\echo -- scan_stats (public aggregate)
SELECT left(value::text, 900) FROM service_state WHERE key = 'scan_stats';
\echo -- heartbeat age (s), cursor
SELECT extract(epoch FROM now() - to_timestamp((value->>'v')::bigint))::int FROM service_state WHERE key = 'heartbeat';
SELECT block_number, updated_at FROM indexer_checkpoints ORDER BY updated_at DESC LIMIT 1;
SQL
      docker stats --no-stream --format '  {{.Name}}: cpu {{.CPUPerc}} mem {{.MemUsage}}' 2>&1 | sort
      echo "host: $(nproc) vCPU; $(free -m | awk '/Mem:/{printf "%d MB used of %d MB, %d MB available", $3, $2, $7}'); load $(cut -d' ' -f1-3 /proc/loadavg)"
      echo "tracker log, last 20 min: 429s=$(docker logs --since 20m elysium-wallet-tracker 2>&1 | grep -c '429') warnings=$(docker logs --since 20m elysium-wallet-tracker 2>&1 | grep -c WARNING) errors=$(docker logs --since 20m elysium-wallet-tracker 2>&1 | grep -c ERROR)"
      docker logs --since 20m elysium-wallet-tracker 2>&1 | grep -E "WARNING|ERROR|scan mode|full scan|tracking " | tail -15 | cut -c1-220
      ;;
    activity-release-verify)
      say "Activity web release: read-only bot health checks"
      healthy_bots
      docker exec elysium-ui python -m agora_tracker --health-ui
      for c in elysium-wallet-tracker elysium-ui gmgn-revival-radar-radar-1; do
        docker inspect "$c" --format '{{.Name}} started={{.State.StartedAt}} restarts={{.RestartCount}}'
      done
      ;;
    wallet-release-verify)
      say "Wallet Intelligence release verification (read-only)"
      docker exec -i elysium-postgres psql -U tracker -d tracker -X -v ON_ERROR_STOP=1 <<'SQL'
SELECT version_num FROM alembic_version;
SELECT version, quality, count(*), max(as_of) FROM wallet_scores GROUP BY 1,2 ORDER BY 1,2;
SELECT version, config->>'eligibility' AS eligibility FROM score_versions;
SELECT count(*) AS historical, count(*) FILTER (WHERE status='active') AS active FROM wallet_enrollment WHERE first_top_at IS NOT NULL;
SQL
      docker exec elysium-wallet-tracker python -m agora_tracker --config /config/config.yaml --tracking-report
      docker logs --since 10m elysium-ui 2>&1 | grep -E 'scores:|scoring|ScoringConfigError' | tail -10
      docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} memory={{.MemUsage}}'
      for c in elysium-wallet-tracker elysium-ui elysium-web gmgn-revival-radar-radar-1; do
        docker inspect "$c" --format '{{.Name}} started={{.State.StartedAt}} restarts={{.RestartCount}}'
      done
      ;;
    tracking-baseline)
      say "tracking baseline (read-only): revision, resources, scanner state, snapshot quality"
      for c in elysium-wallet-tracker elysium-ui elysium-web elysium-postgres; do
        docker inspect "$c" --format '  {{.Name}} image-created={{.Created}} started={{.State.StartedAt}} restarts={{.RestartCount}}' 2>&1 | cut -c1-200
      done
      echo "alembic head in the database: $(docker exec elysium-postgres psql -U tracker -d tracker -X -A -t -c 'SELECT version_num FROM alembic_version')"
      echo "code in the tracker image has: $(docker exec elysium-wallet-tracker sh -c 'ls /app/agora_tracker/migrations/versions 2>/dev/null | grep -c "^0"') migrations"
      docker stats --no-stream --format '  {{.Name}}: cpu {{.CPUPerc}} mem {{.MemUsage}}' 2>&1 | sort
      echo "host: $(nproc) vCPU; $(free -m | awk '/Mem:/{printf "%d MB used of %d MB, %d MB available", $3, $2, $7}'); load $(cut -d' ' -f1-3 /proc/loadavg)"
      docker exec -i elysium-postgres psql -U tracker -d tracker -X -A -F ' | ' <<'SQL' 2>&1 | sed 's/^/  /'
\echo -- scanner cursor vs newest indexed block, heartbeat age (s)
SELECT (SELECT block_number FROM indexer_checkpoints WHERE name = 'indexer') cursor_block,
       (SELECT updated_at FROM indexer_checkpoints ORDER BY updated_at DESC LIMIT 1) cursor_updated,
       (SELECT extract(epoch FROM now() - to_timestamp((value->>'v')::bigint))::int FROM service_state WHERE key = 'heartbeat') heartbeat_age_s;
\echo -- rpc_heads (hosts only)
SELECT left(value::text, 400) FROM service_state WHERE key = 'rpc_heads';
\echo -- leaderboard snapshots by status / completeness
SELECT status, complete, count(*), min(row_count), max(row_count), min(accepted_count), max(accepted_count) FROM leaderboard_snapshots GROUP BY 1, 2 ORDER BY 1, 2;
\echo -- snapshot cadence (median seconds between accepted snapshots)
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)::int median_gap_s, max(gap)::int max_gap_s FROM (SELECT extract(epoch FROM captured_at - lag(captured_at) OVER (ORDER BY captured_at)) gap FROM leaderboard_snapshots WHERE status IN ('ok','imported')) g;
\echo -- database size and the biggest tables
SELECT pg_size_pretty(pg_database_size('tracker')) db_size;
SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid)) size FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 8;
\echo -- indexed block times: recent rate (blocks seen per minute are sparse; use the cursor delta instead)
SELECT count(*) trades_last_24h FROM trades WHERE block_time > now() - interval '24 hours';
SQL
      echo "-- tracker log: tracking-set changes and RPC trouble, last 6 h (no URLs)"
      docker logs --since 6h elysium-wallet-tracker 2>&1 | grep -E "tracking [0-9]+ wallets|rate-limited|RPC error|getLogs .* splitting|lagging" | sed -E 's#https?://[^ )]+#<url>#g' | tail -12 | sed 's/^/  /'
      echo "-- backups"
      ls -1t /root/onchainflow-backups/daily 2>/dev/null | head -3 | sed 's/^/  daily: /'
      ls -1t /root/onchainflow-backups 2>/dev/null | head -6 | sed 's/^/  dir: /'
      ;;
    scan-density)
      say "chain-wide Transfer volume and fine OR-list limits (read-only; a handful of requests per endpoint)"
      docker exec -i elysium-postgres psql -U tracker -d tracker -X -A -t -c "SELECT address FROM wallets LIMIT 5" > /tmp/bench-wallets.txt 2>/dev/null
      for spec in "https://rpc-live.boxa.club 0.3" "https://rpc-live.elyspump.com 0.5" "https://rpc.thekleos.pro/rpc 8" "https://rpc.thekleos.xyz/rpc 8"; do
        set -- $spec
        python3 "$REPO/scripts/scan_bench.py" --rpc "$1" --wallets /tmp/bench-wallets.txt --only density --pause "$2" --budget 20 2>&1 | sed 's/^/  /'
      done
      rm -f /tmp/bench-wallets.txt
      ;;
    scan-bench)
      say "bounded RPC benchmark of the getLogs strategy (read-only, stdlib client, <=120 requests per endpoint)"
      docker exec -i elysium-postgres psql -U tracker -d tracker -X -A -t -c "SELECT DISTINCT e.address FROM leaderboard_snapshot_entries e JOIN leaderboard_snapshots s ON s.id=e.snapshot_id WHERE s.status IN ('ok','imported') ORDER BY 1" > /tmp/bench-wallets.txt 2>/dev/null
      echo "wallet list: $(wc -l < /tmp/bench-wallets.txt) addresses (not printed)"
      python3 "$REPO/scripts/scan_bench.py" --rpc https://rpc.thekleos.pro/rpc --rpc https://rpc.thekleos.xyz/rpc \
        --rpc https://rpc-live.boxa.club --wallets /tmp/bench-wallets.txt --budget 120 2>&1 | sed 's/^/  /'
      rm -f /tmp/bench-wallets.txt
      ;;
    launch-report)
      say "launch intelligence: paper-trading results and launch-watcher state (read-only)"
      docker exec elysium-wallet-tracker python -m agora_tracker --config /config/config.yaml --launch-report 2>&1 | sed 's/^/  /'
      ;;
    trace-token)
      say "read-only on-chain trace of one token (public chain data; wallets limited to the public leaderboard)"
      T=$(head -1 "$REPO/deploy/TRACE_TOKEN" 2>/dev/null | tr 'A-F' 'a-f' | tr -d '[:space:]')
      [[ "$T" =~ ^0x[0-9a-f]{40}$ ]] || { echo "deploy/TRACE_TOKEN must hold one 0x token address"; continue; }
      echo "token: $T"
      PSQL="docker exec -i elysium-postgres psql -U tracker -d tracker -X -A -F |"
      echo "-- what our tracker knows (public_api views only)"
      $PSQL -c "SELECT 'profile', launchpad, name, symbol, launched_at FROM public_api.token_profiles WHERE address='$T'" 2>&1 | sed 's/^/  /'
      $PSQL -c "SELECT 'metadata', symbol, decimals FROM public_api.token_metadata WHERE address='$T'" 2>&1 | sed 's/^/  /'
      $PSQL -c "SELECT 'market', captured_at, source, price_hype, mcap_hype, vol_24h_hype, buys_24h FROM public_api.token_market_snapshots WHERE token='$T' ORDER BY captured_at DESC LIMIT 3" 2>&1 | sed 's/^/  /'
      echo "  trades by public wallets (time | kind | confidence | HYPE | wallet | tx):"
      $PSQL -c "SELECT block_time, kind, confidence, round(hype_wei/1e18, 4), left(wallet, 10)||'..', tx_hash FROM public_api.trades WHERE token='$T' ORDER BY id LIMIT 25" 2>&1 | sed 's/^/    /'
      HU=$($PSQL -t -c "SELECT hype_usd FROM public_api.hype_usd_rates ORDER BY observed_at DESC LIMIT 1" 2>/dev/null | tr -d '[:space:]')
      TXS=$($PSQL -t -c "SELECT tx_hash FROM public_api.trades WHERE token='$T' ORDER BY id LIMIT 8" 2>/dev/null | tr -d ' ')
      $PSQL -t -c "SELECT p.launchpad, x.tx_hash FROM (SELECT launchpad, address, row_number() OVER (PARTITION BY launchpad ORDER BY launched_at DESC NULLS LAST) rn FROM public_api.token_profiles WHERE launchpad IS NOT NULL) p JOIN LATERAL (SELECT tx_hash FROM public_api.trades t WHERE t.token=p.address ORDER BY id DESC LIMIT 1) x ON true WHERE p.rn <= 3" 2>/dev/null | tr '|' ' ' > /tmp/known-venues.tsv
      echo "  known-launchpad fingerprints: $(wc -l < /tmp/known-venues.tsv) sample txs; HYPE/USD ${HU:-n/a}"
      ARGS=""; for h in $TXS; do ARGS="$ARGS --tx $h"; done
      echo "-- on-chain trace"
      python3 "$REPO/scripts/trace_token.py" "$T" --rpc https://rpc.thekleos.pro/rpc --rpc https://rpc.thekleos.xyz/rpc \
        --rpc https://rpc-live.boxa.club $ARGS --known /tmp/known-venues.tsv --hype-usd "${HU:-0}" 2>&1 | sed 's/^/  /'
      rm -f /tmp/known-venues.tsv
      ;;
    audit-early-plays)
      say "read-only base-rate audit: early smart-money buys vs outcome (public_api only; aggregates + public tokens)"
      docker exec -i elysium-postgres psql -U tracker -d tracker -X <<'SQL' 2>&1 | sed 's/^/  /'
\pset footer off
\pset null '-'
CREATE TEMP VIEW base AS
WITH buys AS (SELECT t.id, t.token, t.wallet, t.block_time, t.block_number, t.hype_wei, t.qty, t.confidence
              FROM public_api.trades t WHERE t.kind = 'BUY'),
first_any AS (SELECT token, min(block_time) AS first_at, count(*) AS sm_buys, count(DISTINCT wallet) AS sm_wallets
              FROM buys GROUP BY token),
first_px AS (SELECT DISTINCT ON (b.token) b.token, b.block_time AS px_at,
                    (b.hype_wei / 1e18) / (b.qty / power(10::numeric, coalesce(m.decimals, 18))) AS px
             FROM buys b LEFT JOIN public_api.token_metadata m ON m.address = b.token
             WHERE b.confidence = 'high' AND b.hype_wei > 0 AND b.qty > 0
             ORDER BY b.token, b.block_time, b.id),
outc AS (SELECT f.token, max(s.price_hype) AS max_px,
                (array_agg(s.price_hype ORDER BY s.captured_at DESC))[1] AS last_px, count(*) AS n_snap
         FROM first_px f JOIN public_api.token_market_snapshots s
           ON s.token = f.token AND s.captured_at >= f.px_at AND s.price_hype > 0
         GROUP BY f.token)
SELECT a.token, coalesce(p.symbol, md.symbol) AS sym, p.launchpad, p.launched_at, a.first_at, a.sm_buys,
       a.sm_wallets, extract(epoch FROM a.first_at - p.launched_at) / 60 AS delay_min, f.px,
       o.n_snap,
       CASE WHEN o.max_px / f.px BETWEEN 1e-4 AND 1e4 THEN o.max_px / f.px END AS max_x,  -- outside: unit/decimals mismatch
       CASE WHEN o.max_px / f.px BETWEEN 1e-4 AND 1e4 THEN o.last_px / f.px END AS now_x,
       (o.max_px / f.px NOT BETWEEN 1e-4 AND 1e4) AS bad_units
FROM first_any a
LEFT JOIN public_api.token_profiles p ON p.address = a.token
LEFT JOIN public_api.token_metadata md ON md.address = a.token
LEFT JOIN first_px f ON f.token = a.token
LEFT JOIN outc o ON o.token = a.token;

\echo == 1. launchpads seen by Agora (all tokens, not only ones we traded)
SELECT coalesce(launchpad, '(none)') AS launchpad, count(*) AS tokens, min(launched_at) AS first_launch,
       max(launched_at) AS last_launch, count(*) FILTER (WHERE launched_at > now() - interval '24 hours') AS last_24h
FROM public_api.token_profiles GROUP BY 1 ORDER BY 2 DESC;

\echo == 2. outcome after the FIRST smart-money buy, by how soon after launch it came
\echo    (x = Agora price at/after the buy / price the wallet paid; max is sampled every ~10 min so it understates peaks)
SELECT CASE WHEN launched_at IS NULL THEN 'z launch unknown' WHEN delay_min < 5 THEN 'a <5m'
            WHEN delay_min < 30 THEN 'b 5-30m' WHEN delay_min < 120 THEN 'c 30m-2h'
            WHEN delay_min < 1440 THEN 'd 2-24h' ELSE 'e >24h' END AS first_buy_after_launch,
       count(*) AS tokens, count(max_x) AS with_outcome,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY max_x)::numeric, 2) AS median_max_x,
       round(100.0 * avg((max_x >= 2)::int)) AS pct_max_2x, round(100.0 * avg((max_x >= 10)::int)) AS pct_max_10x,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY now_x)::numeric, 2) AS median_now_x,
       round(100.0 * avg((now_x < 0.5)::int)) AS pct_now_below_half
FROM base GROUP BY 1 ORDER BY 1;

\echo == 3. same, by launchpad
SELECT coalesce(launchpad, '(unindexed)') AS launchpad, count(*) AS tokens, count(max_x) AS with_outcome,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY max_x)::numeric, 2) AS median_max_x,
       round(100.0 * avg((max_x >= 10)::int)) AS pct_max_10x,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY now_x)::numeric, 2) AS median_now_x,
       round(100.0 * avg((now_x < 0.5)::int)) AS pct_now_below_half
FROM base GROUP BY 1 ORDER BY 2 DESC;

\echo == 4. does a cluster (>=3 distinct smart wallets buying) predict more? (any time, first 30 min)
SELECT (sm_wallets >= 3) AS three_plus_wallets, (delay_min < 30) AS within_30m, count(*) AS tokens, count(max_x) AS with_outcome,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY max_x)::numeric, 2) AS median_max_x,
       round(100.0 * avg((max_x >= 10)::int)) AS pct_max_10x,
       round(100.0 * avg((now_x < 0.5)::int)) AS pct_now_below_half
FROM base WHERE launched_at IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2;

\echo == 5. best and worst outcomes (public tokens)
SELECT sym, launchpad, to_char(launched_at, 'MM-DD HH24:MI') AS launched, round(delay_min::numeric, 1) AS delay_min,
       sm_wallets, round(max_x::numeric, 1) AS max_x, round(now_x::numeric, 2) AS now_x, token
FROM base WHERE max_x IS NOT NULL ORDER BY max_x DESC LIMIT 12;
SELECT sym, launchpad, to_char(launched_at, 'MM-DD HH24:MI') AS launched, round(delay_min::numeric, 1) AS delay_min,
       sm_wallets, round(max_x::numeric, 2) AS max_x, round(now_x::numeric, 3) AS now_x, token
FROM base WHERE now_x IS NOT NULL ORDER BY now_x LIMIT 8;

\echo == 6b. bundle wallets (in >=5 same-block >=3-wallet buys): are their early buys a better signal?
CREATE TEMP VIEW bundlers AS
WITH b AS (SELECT token, block_number, array_agg(DISTINCT wallet) AS ws FROM public_api.trades
           WHERE kind = 'BUY' GROUP BY 1, 2 HAVING count(DISTINCT wallet) >= 3)
SELECT w AS wallet FROM b, unnest(ws) w GROUP BY w HAVING count(*) >= 5;
SELECT (EXISTS (SELECT 1 FROM public_api.trades t JOIN bundlers u ON u.wallet = t.wallet
                WHERE t.token = base.token AND t.kind = 'BUY' AND t.block_time <= base.first_at + interval '10 minutes'))
         AS bundler_in_first_10m,
       count(*) AS tokens, count(max_x) AS with_outcome,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY max_x)::numeric, 2) AS median_max_x,
       round(100.0 * avg((max_x >= 2)::int)) AS pct_max_2x, round(100.0 * avg((max_x >= 10)::int)) AS pct_max_10x,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY now_x)::numeric, 2) AS median_now_x,
       round(100.0 * avg((now_x < 0.5)::int)) AS pct_now_below_half
FROM base WHERE delay_min < 30 GROUP BY 1 ORDER BY 1;
SELECT count(*) AS bundle_wallets FROM bundlers;
\echo == 6c. independent wallets (bundle wallets count as one) in the first 30 min after launch
WITH ind AS (
  SELECT b.token, count(DISTINCT CASE WHEN u.wallet IS NULL THEN t.wallet ELSE 'BUNDLE' END) AS n_ind
  FROM base b JOIN public_api.trades t ON t.token = b.token AND t.kind = 'BUY'
    AND t.block_time <= b.launched_at + interval '30 minutes'
  LEFT JOIN bundlers u ON u.wallet = t.wallet
  WHERE b.launched_at IS NOT NULL GROUP BY b.token)
SELECT CASE WHEN n_ind >= 3 THEN '3+' ELSE n_ind::text END AS independent_wallets_30m, count(*) AS tokens,
       count(max_x) AS with_outcome,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY max_x)::numeric, 2) AS median_max_x,
       round(100.0 * avg((max_x >= 2)::int)) AS pct_max_2x, round(100.0 * avg((max_x >= 10)::int)) AS pct_max_10x,
       round(100.0 * avg((now_x < 0.5)::int)) AS pct_now_below_half
FROM ind JOIN base USING (token) GROUP BY 1 ORDER BY 1;
SELECT count(*) FILTER (WHERE bad_units) AS excluded_unit_mismatch FROM base;
\echo == 6d. no look-ahead: ENTER at the first verified buy after the 3rd independent wallet arrives (<=30 min after launch)
CREATE TEMP VIEW sig_out AS
WITH bt AS (SELECT t.token, t.wallet, t.block_time, t.id, t.hype_wei, t.qty, t.confidence,
                   CASE WHEN u.wallet IS NULL THEN t.wallet ELSE 'BUNDLE' END AS ident
            FROM public_api.trades t LEFT JOIN bundlers u ON u.wallet = t.wallet WHERE t.kind = 'BUY'),
firsts AS (SELECT DISTINCT ON (token, ident) token, ident, block_time, id FROM bt ORDER BY token, ident, block_time, id),
ranked AS (SELECT token, block_time, row_number() OVER (PARTITION BY token ORDER BY block_time, id) AS rn FROM firsts),
sig AS (SELECT r.token, r.block_time AS sig_at FROM ranked r JOIN base b USING (token)
        WHERE r.rn = 3 AND b.launched_at IS NOT NULL AND r.block_time <= b.launched_at + interval '30 minutes'),
entry AS (SELECT DISTINCT ON (s.token) s.token, s.sig_at,
                 (t.hype_wei / 1e18) / (t.qty / power(10::numeric, coalesce(m.decimals, 18))) AS px
          FROM sig s JOIN bt t ON t.token = s.token AND t.block_time >= s.sig_at AND t.confidence = 'high'
            AND t.hype_wei > 0 AND t.qty > 0
          LEFT JOIN public_api.token_metadata m ON m.address = s.token ORDER BY s.token, t.block_time, t.id)
SELECT e.token, e.sig_at, max(sn.price_hype) / e.px AS mx,
       (array_agg(sn.price_hype ORDER BY sn.captured_at DESC))[1] / e.px AS nw
FROM entry e JOIN public_api.token_market_snapshots sn ON sn.token = e.token AND sn.captured_at >= e.sig_at
  AND sn.price_hype > 0 GROUP BY 1, 2, e.px;
SELECT count(*) AS tokens,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM o.sig_at - b.launched_at) / 60)::numeric, 1)
         AS median_signal_min_after_launch,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY mx)::numeric, 2) AS median_max_x,
       round(100.0 * avg((mx >= 2)::int)) AS pct_max_2x, round(100.0 * avg((mx >= 10)::int)) AS pct_max_10x,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY nw)::numeric, 2) AS median_now_x,
       round(100.0 * avg((nw < 1)::int)) AS pct_now_below_entry, round(100.0 * avg((nw < 0.5)::int)) AS pct_now_below_half
FROM sig_out o JOIN base b USING (token) WHERE mx BETWEEN 1e-4 AND 1e4;
SELECT b.sym, b.launchpad, round((extract(epoch FROM o.sig_at - b.launched_at) / 60)::numeric, 1) AS signal_min,
       round(mx::numeric, 2) AS max_x, round(nw::numeric, 2) AS now_x
FROM sig_out o JOIN base b USING (token) WHERE mx BETWEEN 1e-4 AND 1e4 ORDER BY mx DESC;

\echo == 6. same-block buys by >=3 distinct smart wallets (bundles: likely ONE operator, not independent signals)
WITH b AS (SELECT token, block_number, array_agg(DISTINCT left(wallet, 8) ORDER BY left(wallet, 8)) AS ws
           FROM public_api.trades WHERE kind = 'BUY' GROUP BY 1, 2 HAVING count(DISTINCT wallet) >= 3)
SELECT array_to_string(ws, ',') AS wallet_group, count(*) AS bundles, count(DISTINCT token) AS tokens
FROM b GROUP BY ws ORDER BY 2 DESC LIMIT 10;
SQL
      ;;
    audit-activity-fx)
      say "read-only audit for the Activity monitor and USD market cap (aggregates only)"
      docker exec -i elysium-postgres psql -U tracker -d tracker -X <<'SQL' 2>&1 | sed 's/^/  /'
\pset footer off
\echo -- trades by kind / confidence
SELECT kind, confidence, count(*) AS n FROM trades GROUP BY 1,2 ORDER BY 1,2;
\echo -- trades: rows, null block_time, time span, table + index sizes
SELECT count(*) AS rows, count(*) FILTER (WHERE block_time IS NULL) AS null_time, min(block_time) AS first, max(block_time) AS last, pg_size_pretty(pg_total_relation_size('trades')) AS total_size FROM trades;
SELECT indexrelname AS index, pg_size_pretty(pg_relation_size(indexrelid)) AS size FROM pg_stat_user_indexes WHERE relname = 'trades' ORDER BY 1;
\echo -- one tx with several legs?
SELECT count(*) AS txs_with_multiple_legs FROM (SELECT tx_hash FROM trades GROUP BY tx_hash HAVING count(*) > 1) x;
\echo -- leaderboard snapshots: does the stored payload carry hypeUsd?
SELECT count(*) AS snapshots, min(captured_at) AS first, max(captured_at) AS last, count(*) FILTER (WHERE raw_payload ? 'hypeUsd') AS with_hypeusd, count(*) FILTER (WHERE status = 'ok') AS ok FROM leaderboard_snapshots;
SELECT min((raw_payload->>'hypeUsd')::numeric) AS min_rate, max((raw_payload->>'hypeUsd')::numeric) AS max_rate, (SELECT (raw_payload->>'hypeUsd')::numeric FROM leaderboard_snapshots WHERE raw_payload ? 'hypeUsd' ORDER BY captured_at DESC LIMIT 1) AS latest_rate FROM leaderboard_snapshots WHERE raw_payload ? 'hypeUsd';
SELECT jsonb_typeof(raw_payload->'hypeUsd') AS hypeusd_type, count(*) FROM leaderboard_snapshots WHERE raw_payload IS NOT NULL GROUP BY 1;
\echo -- snapshot cadence (seconds between consecutive ok snapshots)
SELECT round(avg(d)) AS avg_s, round(max(d)) AS max_s, count(*) AS gaps_over_900s FROM (SELECT extract(epoch FROM captured_at - lag(captured_at) OVER (ORDER BY captured_at)) AS d FROM leaderboard_snapshots WHERE status = 'ok') g WHERE d IS NOT NULL AND (d > 900 OR true);
\echo -- market snapshots
SELECT count(*) AS rows, count(DISTINCT token) AS tokens, count(DISTINCT captured_at) AS rounds, min(captured_at) AS first, max(captured_at) AS last, count(*) FILTER (WHERE mcap_hype IS NULL) AS null_mcap, pg_size_pretty(pg_total_relation_size('token_market_snapshots')) AS size FROM token_market_snapshots;
\echo -- keyset query plan on the public view (newest 50)
EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON) SELECT id, block_time FROM public_api.trades ORDER BY block_time DESC NULLS LAST, id DESC LIMIT 50;
EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON) SELECT count(*) FROM public_api.trades WHERE block_time >= now() - interval '7 days';
SQL
      echo "container health flags:"
      docker ps --format '    {{.Names}}: {{.Status}}' | sort
      echo "tracker --health: $(docker exec elysium-wallet-tracker python -m agora_tracker --health 2>&1 | tail -1)"
      echo "ui image vs latest: $(docker inspect -f '{{.Image}}' elysium-ui | cut -c8-19) vs $(docker image inspect -f '{{.Id}}' elysium-wallet-tracker:latest | cut -c8-19)"
      ;;
    caddy-show)
      say "Caddy: structure of the live Caddyfile (long token-like strings redacted)"
      ls -la "$CADDYFILE" "$CADDY_BAK" 2>&1 | sed 's/^/  /'
      docker inspect -f '  container: {{.State.Status}}, network={{.HostConfig.NetworkMode}}' "$CADDY_C" 2>&1
      docker inspect -f '{{range .Mounts}}  mount: {{.Source}} -> {{.Destination}} (rw={{.RW}}){{"\n"}}{{end}}' "$CADDY_C" 2>&1
      sed -E 's/[A-Za-z0-9+\/=_-]{24,}/<redacted>/g' "$CADDYFILE" | sed 's/^/  | /'
      ;;
    caddy-backup)
      say "Caddy: back up the live Caddyfile (never overwrites an existing backup)"
      if [ -e "$CADDY_BAK" ]; then echo "backup already exists: $CADDY_BAK"; else cp -p "$CADDYFILE" "$CADDY_BAK" && echo "created $CADDY_BAK"; fi
      sha256sum "$CADDYFILE" "$CADDY_BAK" | sed 's/^/  /'
      ;;
    caddy-switch|caddy-rollback)
      # caddy_apply <source-file>: validate it, write it IN PLACE (the file is a Docker bind
      # mount: replacing the inode, e.g. with `sed -i` or mv, would leave the container reading
      # the old content), confirm the container sees it, then reload (graceful, no downtime).
      caddy_apply() {
        docker exec -i "$CADDY_C" caddy validate --config /dev/stdin --adapter caddyfile < "$1" 2>&1 | tail -3 \
          && [ "${PIPESTATUS[0]}" = 0 ] || { echo "validation FAILED: live file untouched"; return 1; }
        cat "$1" > "$CADDYFILE" || return 1
        cmp -s "$1" "$CADDYFILE" && docker exec "$CADDY_C" sh -c 'cat /etc/caddy/Caddyfile' | cmp -s - "$1" \
          || { echo "container does not see the new file"; return 1; }
        docker exec "$CADDY_C" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -3
      }
      site_ok() {  # public HTTPS check from this host: status, real content, valid certificate
        local code body
        code=$(curl -s -o /tmp/.cf_body -w '%{http_code}' -m 20 https://onchainflow.app/) || return 1
        echo "  https://onchainflow.app/ -> $code (certificate verified by curl)"
        [ "$code" = 200 ] && grep -q "Elysium" /tmp/.cf_body
      }
      if [ "$step" = caddy-rollback ]; then
        say "Caddy ROLLBACK: restore $CADDY_BAK"
        [ -f "$CADDY_BAK" ] || { echo "no backup file"; exit 1; }
        caddy_apply "$CADDY_BAK" && echo "restored; upstream lines now:" && grep -nE 'reverse_proxy' "$CADDYFILE" | sed 's/^/  /'
        echo "  (the old website container is stopped: start it with 'docker start gmgn-revival-radar-web-1' for :8000)"
        continue
      fi
      say "Caddy cutover: onchainflow.app upstream 127.0.0.1:8000 -> 127.0.0.1:8100"
      [ -f "$CADDY_BAK" ] || { echo "run caddy-backup first"; exit 1; }
      for path in /healthz / /tokens /smart-money /early /scanner; do
        code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "http://127.0.0.1:8100$path" || echo 000)
        echo "  pre-check :8100$path -> $code"; [ "$code" = 200 ] || { echo "dashboard not ready: aborting before any change"; exit 1; }
      done
      n=$(grep -cE 'reverse_proxy.*:8000' "$CADDYFILE" || true)
      [ "$n" = 1 ] || { echo "expected exactly one ':8000' reverse_proxy line, found $n: aborting"; exit 1; }
      tmp=$(mktemp); sed -E '/reverse_proxy/ s/:8000/:8100/' "$CADDYFILE" > "$tmp"
      diff <(sed -E 's/[A-Za-z0-9+\/=_-]{24,}/<redacted>/g' "$CADDYFILE") <(sed -E 's/[A-Za-z0-9+\/=_-]{24,}/<redacted>/g' "$tmp") | sed 's/^/  diff /'
      if caddy_apply "$tmp"; then
        sleep 3
        if site_ok; then echo "CUTOVER OK"; else
          echo "post-check FAILED: rolling back automatically"; caddy_apply "$CADDY_BAK"; site_ok || true; rm -f "$tmp"; exit 1
        fi
      else
        echo "apply failed: restoring the backup"; caddy_apply "$CADDY_BAK" || true; rm -f "$tmp"; exit 1
      fi
      rm -f "$tmp"
      healthy_bots
      ;;
    *)
      echo "unknown step: $step"; exit 2 ;;
  esac
done
say "after"; disk
