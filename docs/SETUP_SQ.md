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
| `CTRADER_CLIENT_ID` | openapi.ctrader.com → aplikacioni yt → **Credentials** → Client ID |
| `CTRADER_CLIENT_SECRET` | po aty → Secret |
| `CTRADER_ACCES_TOKEN` | aplikacioni yt → **Playground** → scope **trading** → Get token → Access token |
| `CTRADER_REFRESH_TOKEN` | po aty → Refresh token |
| `CTRADER_ACCOUNT_ID` | numri i llogarisë (ai që sheh në cTrader) ose ctidTraderAccountId |
| `TELEGRAM_BOT_TOKEN` | tokeni që të jep @BotFather |
| `TELEGRAM_CHAT_ID` | numri që të kthen boti kur i shkruan `/start` |
| `LOT_XAUUSD` | p.sh. `0.10` — **lotin e vendos ti** |
| `LOT_BTCUSD` | p.sh. `0.01` |
| `TRADING_MODE` | `paper` në fillim; `live` kur je gati |
| `CONFIRM_LIVE_ACCOUNT` | `YES` vetëm kur do të tregtosh me llogari reale |
| `MCP_TOKEN` | fjalëkalim i gjatë i rastësishëm (min. 24 shkronja/numra) — çelësi me të cilin Gemini hyn te `/mcp` |

**cTrader lidhet me Open API** (5 variablat më lart). MAPEX e gjen vetë nëse llogaria është demo apo live dhe lidhet
te serveri i duhur. Kur tokeni skadon (~30 ditë), MAPEX e rinovon vetë me refresh token-in dhe e ruan në Volume.
Nëse vjen 🔑, merr token të ri nga Playground dhe vendose te Railway.
Rrugë e vjetër (rezervë): `CTRADER_MCP_CONFIG` nga cTrader Web → Remote MCP — përdoret vetëm kur mungojnë variablat e
Open API.

Opsionale: `MAP_SOURCE` (`gemini` = harta vjen nga Gemini, parazgjedhje; `mapex` = MAPEX harton vetë),
`CHAIN_MODE` (vlen vetëm me `MAP_SOURCE=mapex`: (`intraday` = zinxhirë afër çmimit në çdo kill zone; `strict` = GEM1 fjalë për fjalë)), `MAP_MAX_AGE_H` (6 — pas sa orësh skadon harta), `MAX_TRADES_PER_DAY` (3), `MAX_CONSECUTIVE_LOSSES` (3), `DAILY_LOSS_LIMIT_R` (3.0), `MAX_LOT` (1.0),
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
- **Gemini harton (GEM1), MAPEX ekzekuton (GEM2).** Gemini merr çmimet live nga MAPEX dhe i dërgon hartën JSON
  te `/mcp`; MAPEX e kontrollon, e ndjek çdo minutë dhe hyn vetëm kur GEM2 del 100/100.
- Harta skadon pas 6 orësh: Gemini duhet të hartojë para çdo kill zone (orari: pjesa 6). Nëse fillon kill zone me
  hartë të skaduar, të vjen ⏰ në Telegram.
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
/ctrader KONFIGURIMI – vetëm për Remote MCP (me Open API kredencialet ndryshohen te Railway)
/health   – si /status, i shkurtër
```

## 6) Gemini + `/mcp` (Gemini harton, MAPEX ekzekuton)
1. Railway → shërbimi MAPEX → **Settings** → **Networking** → **Public Networking** → **Generate Domain**. Nëse të
   pyet për portin, lëre atë që sugjeron Railway (MAPEX dëgjon te `PORT`). Kopjo domenin, p.sh.
   `mapex-production.up.railway.app`.
2. **Variables** → **New Variable** → `MCP_TOKEN` = fjalëkalim i gjatë i rastësishëm (krijoje me menaxherin e
   fjalëkalimeve, min. 24 shenja). Railway rindez MAPEX vetë.
3. Provë: hap në shfletues `https://<domeni>/health` → duhet të shohësh `"status": "ok"` dhe `"mcp": true`.
4. Në Gemini: shto një konektor / server MCP me adresën
   `https://<domeni>/mcp?key=<MCP_TOKEN>`
   (nëse Gemini ka fushë të veçantë për çelësin: adresa `https://<domeni>/mcp` dhe header
   `Authorization: Bearer <MCP_TOKEN>`).
5. Krijo një **Gem**: gemini.google.com → Gems → New Gem.
   - **Name:** `MAPEX GEM1 Mapper`
   - **Description:** `Harton XAUUSD/BTCUSD me GEM1 mbi të dhënat live të MAPEX dhe ia dërgon hartën ekzekutorit.`
   - **Instructions:** ngjit të gjithë `docs/gemini/GEM_INSTRUCTIONS.md` (përmban edhe GEM1 të plotë dhe oraret ICT).
     Nëse Gemini thotë që teksti është shumë i gjatë: ngjit te Instructions vetëm pjesën para
     `# GEM1 PROMPT (law)`, dhe `docs/source/GEM1.md` ngarkoje te **Knowledge** si skedar.
   - **Save**. Hap Gem-in dhe sigurohu që konektori MAPEX është aktiv.
6. Te Gem-i shkruaj: **@Mapex MAP XAUUSD** (zgjidh Mapex nga lista që del kur shkruan @; pa @Mapex Gemini nuk e
   lidh konektorin). Gemini thërret `gem1_inputs`, pastaj `submit_gem1_map`, pa kërkuar Allow.
   Në Telegram `/status` duhet të tregojë `XAUUSD: harta Gemini HH:MM NY · bias …`.
7. Orari (nëse Gemini lejon veprime të planifikuara), çdo ditë pune: **07:30**, **14:00**, **19:30** ora e Tiranës
   (para London, New York dhe PM Silver Bullet).

⚠️ Adresa me `?key=` është çelës: mos e ndaj me askënd. Nëse të rrjedh, ndrysho `MCP_TOKEN` te Railway dhe te
Gemini. Gemini nuk mund të hapë, ndryshojë apo mbyllë tregti: ai vetëm dërgon hartën.

## 7) Probleme të shpeshta
| Shenja | Zgjidhja |
|---|---|
| "healthcheck failed" | Healthcheck Path duhet `/health`; shiko Deploy Logs |
| 🔑 autorizimi dështoi | kontrollo 5 variablat CTRADER_… te Railway; token i ri nga openapi.ctrader.com → Playground |
| S'hap asnjë tregti | normale: kërkohet 100/100 + kill zone; shiko `/status` dhe `/replay` |
| `/status`: "pret JSON-in e Gemini" | Gemini s'ka dërguar hartë: shkruaj MAP XAUUSD te Gemini (pjesa 6) |
| ⏰ harta e Gemini skadoi / teza u prish | kërkoji Gemini-t hartë të re (MAP XAUUSD) |
| Gemini: 401 / unauthorized | çelësi në adresë nuk është i njëjtë me `MCP_TOKEN` te Railway |
| Gemini: 503 | mungon `MCP_TOKEN` te Railway |
| Mesazhe "PAPER" edhe pse vendose live | mungon `CONFIRM_LIVE_ACCOUNT=YES` ose tokeni është i llogarisë demo |
| Urdhri dështoi | lexo mesazhin 🛑, kontrollo lotin, spread-in dhe orarin e tregut |
| ⚠️ Pa Volume | Railway → + Create → Volume → Mount path `/data` |
| ⛔ loti refuzohet (… MAX_LOT …) | vendos lot ≤ `MAX_LOT`, ose rrit `MAX_LOT` nëse e do vërtet atë lot |
| ⛔ çmimet nuk u dekoduan | dërgo foto të mesazhit (përmban çmimin e marrë) — MAPEX nuk tregton atë simbol |
