# DECISIONS — MAPEX

Every choice the GEM prompts or the spec files leave open, and every deviation, is recorded here. Ids are
referenced from the code (`DECISIONS D-xx`). GEM1/GEM2 in `docs/source/` stay the source of truth.

## Tooling and sources

- **D-01 Plugins available: none.** In this build environment `ListPlugins` returned nothing, `claude plugin list`
  → "No plugins installed", `claude plugin marketplace list` → "No marketplaces configured", and a catalog search
  for agent-skills / ponytail / graphify / ruflo found none of them. Per SKILL.md Phase 0 step 4d the build continued
  without them; no `.claude/settings.json` was written (marketplace source values must come from the environment,
  never from memory). Their disciplines were applied by hand: spec → plan → TDD → review, simplest correct code,
  stdlib first. When the plugins become available, add them in `.claude/settings.json` from the Claude Code docs.
- **D-02** `help.ctrader.com` is blocked by this sandbox's network policy. The official
  `spotware/ctrader-skills` repository was read instead (`remote-http-server.md`, `known-quirks.md`,
  `self-healing-playbook.md`, `trader-workflows.md`, `symbol_precision_table.json`). Nothing was vendored.
- **D-03** Source conflict: `remote-http-server.md` calls every price field pipettes, while quirk Q-K19 and
  `broker-execution.md` say order DTO prices are display floats. Order DTOs are sent as display floats and
  band-checked (`decode.price_field`). Position/deal prices returned by the server are decoded adaptively
  (`decode.decode_position_price`: whichever interpretation lands inside `PRICE_BANDS`), so either encoding is safe.
- **D-40** MCP SDK `mcp==2.2.0` (client only): `mcp.Client(streamable_http_client(url, http_client=
  httpx2.AsyncClient(headers={"Authorization": "Bearer …"})))`. Tests use an in-process `MCPServer` fake that
  reproduces the documented quirks (Q-R1, Q-R4, Q-R7, Q-R8, Q-R10).
- **D-43** Docker: the build sandbox has no Docker daemon, so the image build was replaced by the clean-venv
  equivalent (`uv sync --frozen --no-dev` in an empty venv, service started with no secrets, `GET /health` → 200).
  CI (`.github/workflows/ci.yml`) builds the image on every push.
- **D-44** ruflo ADR tools are not available; the three architectural decisions are recorded here as D-45…D-47.

## Architecture (ADR equivalents)

- **D-45 Order protocol.** `MARKET_RANGE` (fallback `MARKET` only when the server rejects the order type) with
  relative SL/TP in points → `amend_position` with both absolute legs → read-back verification → on failure one
  more amend, then close + kill switch. A timeout is reconciled by `comment == setup_key`, never resent.
- **D-46 Thesis-state persistence.** The executor is a pure function; per-zone state lives in `zones`, keyed by the
  stable `zone_key` (symbol|tf|zone_type|formed_at|direction). Zones missing from a new map are archived.
- **D-47 Paper/live/replay parity.** One `pipeline` (mapper + executor + persistence) and one `TradeManager`; only
  the venue object differs (`LiveVenue` / `PaperVenue`). Replay runs the same objects with a simulated clock.

## GEM1 mapper

- **D-04** Absolute "price units" converted to percentages exactly as `shared-primitives.md` §1.
- **D-05** `session_ATR` uses the `current_session` label windows (Asia 19–02, London 02–07, New York 07–19 NY),
  last 10 completed occurrences; M15 history is 1500 bars so 10 occurrences exist. Fallback 4 × ATR14(H1).
- **D-06** Calendar pools (PMH/PML, PWH/PWL, PDH/PDL of the last 3 days) are registered on D1 only; session highs/lows
  (completed windows only, D-51) on H1 only — the same level repeated on three timeframes would fake confluence.
- **D-07** GEM1 lists every swing as a pool but gives no multiplier for a plain swing; a swing with no other type
  uses 0.62 (the lowest swing-like type, Range Liquidity).
- **D-08** Dealing-range fallback when no confirmed D1 major swing exists: extremes of the last 60 D1 bars.
- **D-09** An untouched pool within 0.5 × session_ATR has status `BEING_TARGETED`; the +10 UNTOUCHED bonus still
  applies (it is untouched).
- **D-10** GEM1 1B omits RB: RB scores 10 type points and ranks after IFVG in the 1C type priority.
- **D-11** LTH when the D1 structure is undetermined: sign of the 60-bar net change.
- **D-12** Weekly profile (informational): Reversal = week extreme made Tue/Wed and a later close back through the
  week open; Expansion = bias-side extreme set in the last 24 h and week open within 25 % of the range from the
  opposite extreme; else Range.
- **D-13** Bias: "D1 narrative" = last closed D1 up-close above the previous D1 midpoint (bullish) / mirrored; the
  "both LTH and ITH oppose → no map" rule is applied always (it is also Step 8's first check).
- **D-14** BPR and VI are not chain candidates (the `key_zones.zone_type` enum has no value for them); they are
  listed in `intraday_keylevels`. Chain candidates: OB, BB, FVG, IFVG, RB.
- **D-15** IFVG is a candidate while respected; its `unmitigated` score is 0 (it was tapped by definition).
- **D-16** 1A linkage requires the pool's **first** touch to happen in one of the 2 bars before the displacement.
- **D-17** Breaker: an OB closed through by an opposite displacement leg; breaker direction = the breaking
  displacement; anchor = its CISD open (spec text).
- **D-18** Step 8 "tp1/tp2 within reach": both beyond the zone in the bias direction and tp1 not beyond tp2. The
  session-ATR reachability that gates money is enforced by GEM2 Layer 5.
- **D-19** ORG/AOR are RTH-index concepts → not produced for XAUUSD/BTCUSD. NDOG/NWOG only for XAUUSD (BTC trades
  24/7). The daily discount wick CE is reported in `liquidity_profile` and the map meta (the `static_anchors` schema
  has no key for it and GEM1 forbids new keys).
- **D-48** NY-midnight open = first M15 bar at/after 00:00 NY; weekly open = first H1 bar at/after Sunday 18:00 NY.
- **D-49** Mapper inputs are cut to the spec sizes: D1 250, H4 250, H1 300, M15 1500, W1 60, MN1 24 closed bars.
- **D-52** "Old swing" = major swing older than 5 trading days (weekdays).
- **D-53** Algo-magnet touches are counted on the pool's own timeframe with `level_tol(tf)`.
- **D-55** `suggested_pearl`: TURTLE_SOUP when the root sweep was wick-only, CRT when body; GEM2 labels an entry
  SILVER_BULLET when it falls in an SB window.
- **D-39** Map retention: the last 72 maps per symbol (plus the published one).

## GEM2 executor

- **D-20** MSS = the first M1 body close beyond the last M5 swing of the leg into the raid, within 5 sweep-TF bars
  after the raid (an M5 body close implies an M1 one). The displacement evidence is Module 6 (within 3 bars of the
  sweep) plus the P4.2 footprint.
- **D-21** Raid pools: M5/M15 strict swings, EQH/EQL, completed session highs/lows (incl. NY Lunch) of today and
  yesterday, PDH/PDL; within 0.1 × session_ATR of the zone (or inside). A sweep is counted once even when its M15
  bar closes later.
- **D-22** Entry FVG = the largest M1 FVG in the P4.2 window; retest = an M1 touch of its CE within 15 M1 bars;
  defense on the retest bar or the next one, otherwise `anchor_not_defended`.
- **D-23** THESIS_ANCHOR "still unmitigated" (mandatory RESET condition 4) = no M15 body close beyond the zone's far
  edge since the zone was first seen (trading into the zone is the expected contact).
- **D-24** Module 5 P1/P2 use D1 closes after the zone was first seen; the structural level is the map's D1 dealing
  range low (buy) / high (sell).
- **D-25** GEM2 swaps the Level-3 codes in its Format A template. LAYER 4 (numeric/scoring rules rank above output
  templates in GEM2's own precedence) is used: 3001 = HTF structural break, 3002 = D1 close past THESIS_ROOT.
- **D-26** MONITOR lasts until a map with a different structure hash (bias + zone keys + roots) is published.
  CONFIRMED is terminal for a zone_key (one trade per setup).
- **D-27** When P1/P2 are not full at confirmation → NO-SETUP, the zone returns to WATCH, sweep_count kept.
- **D-28** Dangerous trap: only live (not yet taken) pools of kinds EQH/EQL, session H/L, PDH/PDL count; the pool
  that was just raided is taken liquidity.
- **D-29** TP2 = next liquidity objective beyond TP1 with ≥ 3.5R; SD −2.5 only when no such objective exists.
  TP3 = `final_lrlr_objective` when ≥ 4.5R. Server TP = TP2 if present, else TP1.
- **D-30** SMT is not implemented (confluence only, it cannot gate): always `NO SMT VISIBLE`.
- **D-36** A CONFIRMED whose defense bar closed more than 120 s ago (catch-up after downtime) is never traded.
- **D-37** Level-3 INVALIDATED never closes open positions: SL/TP stand (spec).
- **D-56** WAIT is emitted only when P1–P3 are full; earlier progress (RAID/SHIFT/GAP) is internal.
- **D-57** Layer 9 counter-bias diagnosis: `diagnosed_trap` is `NONE` in Format A (no screenshots to diagnose a trap
  path); the reason carries the P4 code (e.g. `tp_constraints_not_met`, `dangerous_trap`).

## Broker, guards, service

- **D-31** Volume step for the TP1 partial: 100 cents XAUUSD (0.01 lot), 1 cent BTCUSD. When 50 % cannot be
  expressed (minimum lot) no partial close is sent; breakeven still follows TP1.
- **D-32** After the owner's own SL/TP are adopted (60 s after MAPEX's last amend), MAPEX stops managing that
  position (no partial, no breakeven) — it never fights the owner.
- **D-33** Daily loss = sum of today's negative R; the consecutive-loss counter resets at the 17:05 NY roll. Each
  automatic trip requires `/resume`.
- **D-34** Quote fresh = fetched ≤ 5 s ago, broker timestamp not frozen (> 120 s) and not skewed > 30 s.
- **D-35** A token whose environment cannot be decoded is treated as live for the confirmation gate.
- **D-41** `/start` answers anyone with their own chat id only (needed for setup); all other commands only from
  `TELEGRAM_CHAT_ID`. The `/ctrader` message is deleted immediately.
- **D-42** Replay uses bid = M1 close and a fixed spread (XAUUSD 0.20, BTCUSD 15).
- **D-50** Partial fills are volume mismatches: close + kill switch (broker-execution §3.7/§5).
- **D-58** A configuration sent with `/ctrader` is stored in the service's own SQLite (`kv`, on the owner's Railway
  volume) so it survives restarts; it is never logged, never shown (masked `eyJw…AB12`), and the Telegram message
  carrying it is deleted.
- **D-59** `.gitignore` ignores only the root `/data/` directory (the runtime DB), never the `mapex/data/` package.

## After the first real connection (demo account, 2026-09-24)

- **D-60** The first real start-up disabled XAUUSD: the live bid did not decode with the precision-table digits
  (the real server's encoding differs from the ctrader-skills table). Calibration now tries the resolved digits
  first and then the *unique* power of ten that lands the bid inside `PRICE_BANDS` (bands are narrower than 10×, so
  at most one fits; display floats decode with 0). No unique answer still disables the symbol. Trendbars are
  calibrated separately on their last close (their encoding may differ from spot); timestamps may be ISO strings.
- **D-61** Errors that only mention "session" (MCP `Mcp-Session-Id` expiry) are transport errors: the client
  reconnects and retries read tools. Only 401/403 or unauthorised/forbidden/expired/invalid-token texts are auth
  errors. The 🔑 alert now carries the (redacted) server message.
- **D-62** Relative SL/TP points use the calibrated pipette digits (metadata / table default when spot comes as
  display floats). After a fill, the SL distance the broker actually applied is compared with the intended one; a
  power-of-ten mismatch is stored as `points_digits:<SYMBOL>` and used from the next order on (the current one is
  exactified by the amend + read-back as always). `MAX_SLIPPAGE_POINTS` keeps its price value when the digits change.
- **D-63** A symbol whose start-up calibration failed on a transient error (e.g. "Session not found") stays disabled
  only until the next heartbeat (5 min), which calibrates it again and sends "✅ <SYMBOL> u aktivizua". Read tools
  that hit a session error are retried up to 5 times on a new session. Mutations are still sent once: a session
  error on `create_order` goes to reconciliation by comment, never to a resend.
- **D-64** One MCP session per call, opened and closed inside the calling task. The Railway logs showed the server
  dropping idle sessions (repeated "Session termination failed: 404") and a shared session closed from another loop
  cancelling uvicorn (anyio cancel scopes belong to the task that opened them). The tools list is read once per
  token. The cost is one handshake per call, well inside the rate limits at MAPEX's call rate.
- **D-65** Second round of Railway logs (per-call sessions live):
  - **String timestamps.** `get_trendbars` rejected numeric `fromTimestamp`/`toTimestamp` ("expected string,
    received number"), so no candles loaded and no map was built. Arguments now follow the live schema: a number the
    schema declares as a string is sent as text, and a timestamp is sent as ISO 8601 UTC. It is sent as epoch-ms text
    instead when the schema says milliseconds, or after the server refuses ISO once (reads only, remembered per
    tool). Each timestamp schema is logged at start-up.
  - **Lost sessions.** "Session not found; re-initialize" still came back on brand-new sessions. That is a JSON-RPC
    answer to an unknown session id: the server rejects the request before any tool runs (MCP spec, HTTP 404). It
    is resent on the same session up to 5 times, orders included, which cannot duplicate an order. A timeout is still
    never resent, and reconciliation still adopts, or flags as orphan, any MAPEX position it did not expect.
  - **No session DELETE on exit.** Every DELETE was answered 404.
- **D-66** The owner offered cTrader Open API credentials (client id/secret, access/refresh token, account id) in case
  the Remote MCP cannot do the job. Kept the Remote MCP: every failure seen on Railway had a concrete cause fixed in
  D-64/D-65, and live quotes already flowed. Open API would replace the whole broker layer (protobuf or JSON over
  TCP/WebSocket, token refresh, event stream) and bring new failure modes. It becomes the plan only if candles or
  orders still fail after D-65, as a second connection behind the same `CTraderClient` interface, selected by
  Railway variables. The secrets would then go only into Railway, never into chat or code.
- **D-67** Third round of Railway logs: with the string timestamps accepted, almost every `get_trendbars` still ended
  in "Session not found" after all retries, while quotes kept working. Loading history opens about 80 requests per
  symbol (720 h chunks), and each request had its own new session. So the server saw bursts of sessions, alongside
  the quote, manage and reconcile loops' own sessions, and it drops or evicts sessions under that load. Now one MCP
  session is reused for every request. It is opened, used and closed only by a dedicated owner task, and requests
  go out one at a time through a queue (anyio scopes still never cross tasks, D-64). A lost session is reopened and
  the request resent (D-65). A timeout closes the session and is never resent. Any other service using the same
  cTrader token can still take MAPEX's session; MAPEX then reopens it.
- **D-68** Fourth round of Railway logs (single reused session live): XAUUSD mapped (`valid=True`) and BTCUSD mapped
  (`bias_conflict_htf`), but the start-up history load took ~10 minutes for XAUUSD and ~25 for BTCUSD. It needed
  ~80 ranged `get_trendbars` chunks per symbol (720 h cap), and a run of lost sessions failed two ticks along the
  way.
  - **Start-up history in one request per timeframe.** The live schema offers `count` ("Defaults to now when only
    'count' is provided"; never together with `fromTimestamp`), so each timeframe asks for the newest
    `HISTORY × 1.2` bars in one request. It falls back to 720 h ranges when the schema has no `count`, the server
    refuses it, or the answer is short. Incremental refreshes still use `from`/`to`.
  - **More lost-session retries.** Up to 8 (safe per D-65).
  - **Heartbeat stats.** Every 5 minutes the heartbeat logs `cTrader: {requests, sessions, lost}`, so the logs
    show how often the server drops the session.
