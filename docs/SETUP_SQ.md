# MAPEX — Instalimi dhe ndezja (vetëm me telefon)

Nuk ke nevojë të dish kod. Çdo hap bëhet nga telefoni, në faqen railway.com dhe në Telegram.

## 1) Railway — krijo shërbimin
1. Hap **railway.com** → **New Project** → **Deploy from GitHub repo** → zgjidh repon **mapex**.
2. Hyr te shërbimi → **Settings**:
   - **Healthcheck Path**: `/health`
   - **Serverless / App Sleeping**: **OFF** (MAPEX duhet të jetë gjithmonë zgjuar)
   - **Branch**: `main`
   - Domain nuk nevojitet (MAPEX nuk pret kërkesa nga jashtë). Gjeneroje vetëm nëse do ta hapësh `/health` në browser.
3. Te kanavaca e projektit → **+ Create → Volume** → lidhe me shërbimin → **Mount path: `/data`**.
   Pa Volume humbet historiku dhe mbrojtja nga urdhrat e dyfishtë pas çdo deploy.

## 2) Variablat (Service → Variables)
| Emri | Vlera |
|---|---|
| `CTRADER_MCP_CONFIG` | cTrader Web → Settings → **Remote MCP** → butoni "Copy configuration" → ngjite **të gjithë bllokun** këtu (url + headers bashkë) |
| `TELEGRAM_BOT_TOKEN` | tokeni që të jep @BotFather |
| `TELEGRAM_CHAT_ID` | numri që të kthen boti kur i shkruan `/start` |
| `LOT_XAUUSD` | p.sh. `0.10` — **lotin e vendos ti** |
| `LOT_BTCUSD` | p.sh. `0.01` |
| `TRADING_MODE` | `paper` në fillim; `live` kur je gati |
| `CONFIRM_LIVE_ACCOUNT` | `YES` vetëm kur do të tregtosh me llogari reale |

**cTrader kërkon vetëm një variabël: `CTRADER_MCP_CONFIG`.** Nëse Railway të sugjeron edhe `CTRADER_MCP_URL` dhe
`CTRADER_MCP_TOKEN`, fshiji me ✕ (ose lëri bosh) — MAPEX e nxjerr vetë url-në dhe tokenin nga konfigurimi i plotë.
Alternativë: butoni "Copy token" → vetëm tokeni te `CTRADER_MCP_CONFIG` (url-ja standarde përdoret automatikisht).

Opsionale: `MAX_TRADES_PER_DAY` (3), `MAX_CONSECUTIVE_LOSSES` (3), `DAILY_LOSS_LIMIT_R` (3.0), `MAX_LOT` (1.0),
`TP1_CLOSE_PCT` (50, lejohet 50–80), `MAX_SPREAD`, `NOTIFY_EXITS` (`true` nëse do mesazh edhe kur mbyllet tregtia).

Simbol pa `LOT_…` nuk tregtohet kurrë. Lot më i madh se `MAX_LOT` refuzohet në nisje.

**Tokeni i cTrader është çelësi i llogarisë.** Mos e ngjit askund tjetër veç Railway-t ose komandës `/ctrader` te
boti (boti e fshin mesazhin menjëherë). Konfigurimi vlen vetëm për llogarinë e zgjedhur në cTrader Web — nëse ndërron
llogari, kopjoje sërish.

## 3) Telegram
1. Hap **@BotFather** → `/newbot` → jepi një emër → merr **tokenin** → vendose te Railway si `TELEGRAM_BOT_TOKEN`.
2. Hap botin tënd → **Start** → shkruaj `/start` → boti të kthen **Chat ID** → vendose te Railway si `TELEGRAM_CHAT_ID`.
3. Pas redeploy duhet të vijë mesazhi **"🚀 MAPEX u ndez"** me modalitetin, llogarinë, simbolet dhe lotet.

## 4) Rruga drejt tregtimit real (mos e kapërce)
1. **PAPER** (parazgjedhje): lëre 1–2 javë. Mesazhet vijnë me `📝 PAPER`. Krahasoji me tregun.
2. **DEMO**: hap një llogari demo në cTrader, kopjo konfigurimin e asaj llogarie, vendos `TRADING_MODE=live`.
   Tani urdhrat janë realë por me para virtuale — kjo provon volumin, SL/TP dhe menaxhimin.
3. **LIVE**: kopjo konfigurimin e llogarisë reale, vendos `CONFIRM_LIVE_ACCOUNT=YES`, fillo me lotin më të vogël.
   `/status` duhet të thotë `Modaliteti: LIVE (llogari live)`.

Në çdo hap: `/replay XAUUSD 30` të tregon sa tregti do të kishte hapur sistemi në 30 ditët e fundit dhe rezultatin në R.

## 5) Përdorimi i përditshëm
- Nuk ke asgjë për të bërë. MAPEX harton (GEM1) pas çdo mbylljeje H1 dhe ekzekuton (GEM2) çdo minutë.
- Kur hyn në treg, të vjen mesazhi me çmimin, lotin, SL dhe TP.
- `/stop` ndal hyrjet e reja në çdo moment · `/flat` pastaj `/flat yes` mbyll gjithçka të MAPEX · `/resume` rifillon.
- Nëse vjen 🛑 ose 🔑, MAPEX ka ndaluar vetë: rregullo arsyen dhe shtyp `/resume`.

### Komandat
```
/status   – gjendja: modaliteti, harta, bias, zonat, pozicionet, mbrojtjet
/map      – përmbledhja e hartës (Liquidity Intelligence Brief)   p.sh. /map BTCUSD
/stop     – ndal hyrjet e reja
/resume   – rifillo
/flat     – mbyll të gjitha pozicionet e MAPEX (kërkon /flat yes)
/trades 7 – tregtitë e 7 ditëve të fundit me rezultatin në R
/replay XAUUSD 30 – testo sistemin mbi 30 ditë histori (vetëm raport, pa urdhra)
/ctrader KONFIGURIMI – rinovo tokenin (mesazhi fshihet automatikisht)
/health   – si /status, i shkurtër
```

## 6) Probleme të shpeshta
| Shenja | Zgjidhja |
|---|---|
| "healthcheck failed" | Healthcheck Path duhet `/health`; shiko Deploy Logs |
| 🔑 tokeni skadoi | cTrader Web → Remote MCP → kopjo → dërgo `/ctrader KONFIGURIMI` te boti |
| S'hap asnjë tregti | normale: kërkohet 100/100 + kill zone; shiko `/status` dhe `/replay` |
| Mesazhe "PAPER" edhe pse vendose live | mungon `CONFIRM_LIVE_ACCOUNT=YES` ose tokeni është i llogarisë demo |
| Urdhri dështoi | lexo mesazhin 🛑, kontrollo lotin, spread-in dhe orarin e tregut |
| ⚠️ Pa Volume | Railway → + Create → Volume → Mount path `/data` |
| ⛔ loti refuzohet (… MAX_LOT …) | vendos lot ≤ `MAX_LOT`, ose rrit `MAX_LOT` nëse e do vërtet atë lot |
| ⛔ çmimet nuk u dekoduan | dërgo foto të mesazhit (përmban çmimin e marrë) — MAPEX nuk tregton atë simbol |
