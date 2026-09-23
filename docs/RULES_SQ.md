# Çfarë bën MAPEX dhe kur tregton (shqip i thjeshtë)

## Në një fjali
MAPEX lexon qirinjtë e llogarisë tënde IC Markets (cTrader), gjen ku janë "stop-at" e mëdha të tregut (likuiditeti),
pret që tregu t'i marrë ato dhe të kthehet me forcë, dhe hyn vetëm kur **të katër fazat** e strategjisë japin
**100 pikë nga 100**. Çdo gjë tjetër = nuk hyn.

## Dy trurët
1. **GEM 1 — Hartuesi (D1 / H4 / H1)**, pas çdo mbylljeje të orës:
   - gjen të gjitha nivelet e likuiditetit (majat/fundet e ditës, javës, muajit, sesioneve, "equal highs/lows", etj.)
     dhe u jep secilës një pikë **LPS 0–100** (sa i rëndësishëm është niveli);
   - nga niveli më i fortë i paprekur vendos **drejtimin** (bias BUY ose SELL) dhe objektivin (DOL);
   - gjen zonat ku institucionet lanë gjurmë (OB, FVG, Breaker, IFVG, RB) **vetëm** nëse ato lindën nga marrja e një
     niveli likuiditeti — zinxhiri shkak-pasojë;
   - zgjedh **CHAIN_A** (niveli burim duhet LPS ≥ 65) dhe **CHAIN_B** (LPS ≥ 50);
   - kontrollon 11 rregulla verifikimi; nëse një dështon, harta **nuk publikohet** dhe nuk tregtohet.
2. **GEM 2 — Ekzekutuesi (M15 / M5 / M1)**, çdo minutë:
   - **P1 (25)** harta ekziston, teza është gjallë, M15 nuk e kundërshton;
   - **P2 (25)** jemi në kill zone (London 02:00–05:00, New York 08:30–11:00, PM Silver Bullet 14:00–15:00 NY),
     jo në drekë (12:00–13:30), jo pas 15:50; tregu ka bërë lëvizjen mashtruese (Judas) dhe objektivi është përpara;
   - **P3 (25)** te zona: merret një nivel (sweep me trup, "Type 7"), vjen displacement, prishet struktura me trup
     qiriri (MSS), lë FVG, çmimi kthehet te mesi i FVG-së;
   - **P4 (25)** qiriri i rikthimit mbron nivelin (trup ≥ 60 %, fitil kundër ≤ 35 %, mbyll në drejtimin tonë).
   - **100/100 → hyn**. Më pak → pret (WAIT), rifillon (RESET), vëzhgon (MONITOR) ose s'ka setup (NO-SETUP).

## Stop Loss dhe Take Profit
- **SL**: pas ekstremit të sweep-it + spread + buffer sigurie. Nëse SL bie mbi një pool likuiditeti (EQH/EQL, H/L
  sesioni, PDH/PDL) → **nuk hyn** (kurth i rrezikshëm). Rreziku duhet të jetë ≥ 4 × spread dhe ≤ 0.5 × ATR e sesionit.
- **TP1** duhet të jetë **≥ 3R** dhe i arritshëm brenda 0.4 × ATR e sesionit; nëse jo → nuk hyn.
- Te TP1: mbyllet **50 %** (TP1_CLOSE_PCT) dhe SL shkon në **breakeven** — kurrë para TP1.
- TP2 (≥ 3.5R) vendoset te brokeri; TP3 (≥ 4.5R) = objektivi i madh i hartës (vetëm informativ).

## Mbrojtjet (nuk i shkel kurrë)
- **Lot-i është yti**: vjen nga `LOT_XAUUSD` / `LOT_BTCUSD`. MAPEX nuk llogarit lot nga balanca, kurrë.
- Asnjë pozicion pa SL: SL/TP dërgohen bashkë me urdhrin, pastaj rregullohen saktë dhe verifikohen; nëse
  verifikimi dështon → pozicioni mbyllet dhe MAPEX ndalet vetë.
- Asnjë urdhër i dyfishtë: çdo setup ka një çelës unik; pas timeout MAPEX **kontrollon**, nuk ridërgon.
- Maksimumi: 3 tregti në ditë, 1 pozicion për simbol, 2 gjithsej; ndalet pas 3 humbjeve radhazi ose −3R në ditë.
- Nuk hyn me spread të lartë, me çmim të vjetër (> 5 s), jashtë orarit të tregut ose afër mbylljes.
- Prek **vetëm** pozicionet me etiketën "MAPEX". Pozicionet e tua nuk i prek kurrë; nëse ti ndryshon SL/TP të një
  pozicioni MAPEX, ai respekton ndryshimin tënd.
- Parazgjedhja është **PAPER**. Llogaria reale kërkon edhe `CONFIRM_LIVE_ACCOUNT=YES`.

## Kur teza vdes
- Mbyllje ditore (D1) me trup përtej nivelit burim (THESIS_ROOT) ose thyerje strukture D1 kundër bias-it →
  zona fshihet dhe harta rillogaritet. Pozicionet e hapura **mbeten** me SL/TP e tyre.
- 3 sweep pa displacement, ose 3 sesione pa arritur zonën → MONITOR: zona nuk jep më hyrje deri në hartë të re.

## Mesazhet që merr
- Vetëm kur **hyn në treg** (çmimi, loti, SL, TP, zona, pikët 100/100) dhe **alarmet kritike** (🛑 ndalim automatik,
  🔑 token i skaduar, ⚠️ pozicion pa SL). Asgjë për WAIT/RESET/MONITOR — ato shihen me `/status`.
