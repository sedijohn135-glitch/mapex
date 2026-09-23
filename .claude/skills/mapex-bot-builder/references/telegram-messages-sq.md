# Telegram — MAPEX (shqip, `parse_mode=HTML`, çdo vlerë dinamike e escape-uar)

Rregull: njoftim **vetëm kur MAPEX hyn në treg**, plus alarme kritike që ndalojnë tregtimin. Asnjë mesazh për
WAIT / RESET / MONITOR / NO-SETUP (ato shkojnë vetëm në `events` dhe te `/status`).

## Hyrje në treg (mesazhi kryesor)

```
🟢 <b>MAPEX HYRI — BUY XAUUSD</b>
Hyrja: <b>5654.77</b> · Lot: <b>0.10</b>
SL: <b>5651.20</b> (−3.57)
TP1: 5665.90 (3.1R) · TP2: 5678.40 (6.4R)
Zona: CHAIN_A H4 FVG @ 5653.90
Root: SSL @ 5640.10 (LPS 82) · Sweep #2 (BODY)
P1 25 · P2 25 · P3 25 · P4 25 = 100
Sesioni: NY Killzone 08:47 · Model: DRO
Llogaria: LIVE · ID: MPX-0917-XAU-03
```
Për SELL: 🔴 dhe "SELL". Në modalitet letër, titulli fillon me `📝 PAPER — ` dhe rreshti i fundit thotë `PAPER`.

Nëse `NOTIFY_EXITS=true` (jo si parazgjedhje): `✅ TP1 — mbyllje 50% dhe SL në breakeven · MPX-…` ·
`🏁 Dalje: TP2 (+6.4R)` · `🛑 Dalje: SL (−1R)`.

## Alarme kritike (gjithmonë aktive)

```
🔑 <b>TOKENI I CTRADER SKADOI</b> — MAPEX nuk hap tregti.
cTrader Web → Settings → Remote MCP → kopjo konfigurimin → dërgoje këtu: /ctrader KONFIGURIMI
```
```
🛑 <b>MAPEX U NDAL AUTOMATIKISht</b>
Arsyeja: {reason}
Pozicionet ekzistuese mbeten me SL/TP. Rifillo me /resume.
```
Arsye: `verifikimi i urdhrit dështoi` · `volumi nuk përputhet` · `3 humbje radhazi` · `limiti ditor −3R` ·
`çmimet nuk u dekoduan` · `gabim baze të dhënash`.
```
⚠️ <b>POZICION PA SL</b> — {symbol} #{position_id}. Kontrollo manualisht.
```
```
🚀 MAPEX u ndez · Modaliteti: {LIVE|PAPER} · Llogaria: {demo|live} · Simbolet: XAUUSD 0.10 · BTCUSD 0.01
Mbrojtjet: max 3 tregti/ditë · max 1 pozicion/simbol · ndalim pas 3 humbjeve
```
(një herë në nisje; nëse `TRADING_MODE=live` por llogaria është live dhe `CONFIRM_LIVE_ACCOUNT≠YES`, mesazhi thotë
qartë se po punon në PAPER.)

## Komandat (vetëm nga `TELEGRAM_CHAT_ID`)

```
/status   – gjendja: modaliteti, harta aktive, bias, zonat, pozicionet, mbrojtjet
/map      – përmbledhja e hartës aktuale (Liquidity Intelligence Brief)
/stop     – ndal hyrjet e reja
/resume   – rifillo
/flat     – mbyll të gjitha pozicionet e MAPEX (kërkon /flat yes)
/trades 7 – tregtitë e 7 ditëve të fundit me rezultatin në R
/replay XAUUSD 30 – testo sistemin mbi 30 ditë histori (vetëm raport, pa urdhra)
/ctrader KONFIGURIMI – rinovo tokenin (mesazhi fshihet automatikisht)
/health   – si /status, i shkurtër
```

`/status` shembull:
```
Modaliteti: LIVE (llogari live) · Kill switch: OFF
XAUUSD: harta 09:00 NY · bias BUY · CHAIN_A GAP (sweep 2) · CHAIN_B WATCH
BTCUSD: harta 09:00 NY · bias SELL · CHAIN_A WATCH
Pozicione: 1 (XAUUSD BUY 0.10 @5654.77, SL 5651.20, TP 5678.40)
Sot: 1 tregti · 0 humbje radhazi · −0.0R
Të dhënat: 1s · spread XAU 0.18 · BTC 24
```
