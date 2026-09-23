# Udhëzues instalimi & ndezje (bazë për `docs/SETUP_SQ.md`) — vetëm me telefon

## 1) Railway
1. railway.com → **New Project** → **Deploy from GitHub repo** → zgjidh repon e MAPEX-it.
2. Shërbimi → **Settings**: Healthcheck Path `/health`; "Serverless / App Sleeping" **OFF**; Branch `main`.
   (Domain nuk nevojitet — MAPEX nuk pret kërkesa nga jashtë. Gjeneroje vetëm nëse do të hapësh `/health` nga browseri.)
3. Kanavaca → **+ Create → Volume** → lidhe me shërbimin → **Mount path `/data`**. Pa këtë humbet historiku dhe
   mbrojtja nga urdhrat e dyfishtë pas çdo deploy.

## 2) Variablat (Settings → Variables)
| Emri | Vlera |
|---|---|
| `CTRADER_MCP_CONFIG` | konfigurimi i kopjuar nga cTrader Web → Settings → Remote MCP (butoni "Copy configuration") |
| `TELEGRAM_BOT_TOKEN` | nga @BotFather |
| `TELEGRAM_CHAT_ID` | numri që të kthen boti te `/start` |
| `LOT_XAUUSD` | p.sh. `0.10` — **lotin e vendos ti** |
| `LOT_BTCUSD` | p.sh. `0.01` |
| `TRADING_MODE` | `paper` në fillim; `live` kur je gati |
| `CONFIRM_LIVE_ACCOUNT` | `YES` vetëm kur do të tregtosh llogari reale |
Opsionale: `MAX_TRADES_PER_DAY`, `MAX_CONSECUTIVE_LOSSES`, `DAILY_LOSS_LIMIT_R`, `MAX_LOT`, `TP1_CLOSE_PCT`,
`MAX_SPREAD`, `NOTIFY_EXITS`.

Tokeni i cTrader është çelësi i llogarisë: mos e ngjit askund tjetër veç Railway-t ose komandës `/ctrader` te boti
(boti e fshin mesazhin). Konfigurimi vlen për llogarinë e zgjedhur në cTrader Web — nëse ndërron llogari, kopjoje sërish.

## 3) Telegram
@BotFather → `/newbot` → token → Railway. Hap botin → **Start** → `/start` → merr chat id → vendose te Railway.
Pas redeploy duhet të vijë mesazhi "🚀 MAPEX u ndez".

## 4) Rruga drejt tregtimit real (mos e kapërce)
1. **PAPER** (parazgjedhje): lëre 1–2 javë. Mesazhet vijnë me `📝 PAPER`. Krahaso me tregun.
2. **DEMO**: hap një llogari demo në cTrader, kopjo konfigurimin e asaj llogarie, vendos `TRADING_MODE=live`.
   Tani urdhrat janë realë por me para virtuale — kjo provon volumin, SL/TP dhe menaxhimin.
3. **LIVE**: kopjo konfigurimin e llogarisë reale, vendos `CONFIRM_LIVE_ACCOUNT=YES`, fillo me lotin më të vogël.
   `/status` duhet të thotë `Modaliteti: LIVE (llogari live)`.

Në çdo hap: `/replay XAUUSD 30` të jep sa tregti do të kishte hapur sistemi në 30 ditët e fundit dhe rezultatin në R.

## 5) Përdorimi i përditshëm
- Nuk ke asgjë për të bërë. MAPEX harton (GEM1) çdo mbyllje H1 dhe ekzekuton (GEM2) çdo minutë brenda kill zone-ve.
- Kur hyn në treg, të vjen mesazhi me çmimin, lotin, SL dhe TP.
- `/stop` ndal hyrjet e reja në çdo moment; `/flat yes` mbyll gjithçka; `/resume` rifillon.
- Nëse vjen 🛑 ose 🔑, MAPEX ka ndaluar vetë: rregullo arsyen dhe shtyp `/resume`.

## 6) Probleme të shpeshta
| Shenja | Zgjidhja |
|---|---|
| "healthcheck failed" | Healthcheck Path `/health`; shiko Deploy Logs |
| 🔑 token skadoi | cTrader Web → Remote MCP → `/ctrader KONFIGURIMI` |
| S'hap asnjë tregti | normale: kërkohet 100/100 + kill zone; shiko `/status` dhe `/replay` |
| Mesazhe "PAPER" edhe pse vendose live | mungon `CONFIRM_LIVE_ACCOUNT=YES` ose tokeni është i llogarisë demo |
| Urdhri dështoi | shiko mesazhin 🛑, kontrollo lotin, spread-in dhe orarin e tregut |
