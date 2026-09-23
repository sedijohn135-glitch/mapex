
# Rregulla të detyrueshme për këtë projekt (MAPEX bot)

Ti ke të instaluar këto plugins:
• agent-skills (Osmani)
• ponytail
• graphify
• ruflo

RREGULLA TË FORTA:
1. Gjithmonë përdor mentalitetin e ponytail (zgjidhja më e thjeshtë dhe minimale e mundshme). Mos e mbingarko kodin.
2. Kur fillon një detyrë të re, së pari përdor skill-et e agent-skills (plan → build → review).
3. Përdor graphify kur duhet të kuptosh strukturën e projektit.
4. Përdor ruflo kur ke nevojë për planifikim më kompleks.
5. Mos i harxhosh tokens duke bërë gjithçka manualisht. Gjithmonë prefero skills-et e pluginsave.
6. Unë nuk di kod. Prandaj ti duhet të zgjedhësh dhe të përdorësh skills-et automatikisht, pa ma kërkuar mua.

Çdo herë që fillon punë, vepro sipas këtyre rregullave.

## Harta e skill-eve
- Plugins-at aktivizohen në repo te `.claude/settings.json` (Faza 0 e skill-it). Nëse ndonjë s'ngarkohet, shkruaje te `docs/DECISIONS.md` dhe vazhdo pa të.
- Burimi i kërkesave: `.claude/skills/mapex-bot-builder/` (SKILL.md → references/), dhe `docs/source/GEM1.md`, `GEM2.md`.
- Detyrë e re: `agent-skills:spec` → `agent-skills:plan` → `agent-skills:build`.
- Mapper/executor: `agent-skills:test-driven-development` (test para kodit, fixtures me qirinj).
- Urdhrat te brokeri, mbrojtjet, sekretet: `agent-skills:doubt-driven-development` + `agent-skills:security-and-hardening`.
- API të jashtme (cTrader, MCP SDK, Railway): `agent-skills:source-driven-development`.
- Test që dështon: `agent-skills:debugging-and-error-recovery`.
- Para commit-it: `agent-skills:review` + `ponytail:ponytail-review`. Para deploy: `agent-skills:ship`.
- Sesion i ri mbi kod ekzistues: `graphify:graphify`. Vendime arkitekturore: `ruflo-adr:adr-create` (vetëm nëse ruflo është aktiv).

## Mbrojtje nga harxhimi i tokens
- Nëse një plugin kërkon server/mjet që s'është aktiv, anashkaloje menjëherë.
- Mos lexo skedarë të mëdhenj të plotë: grep + intervale rreshtash.
- Mos shto librari pa nevojë. Mos krijo `railway.json`.
- Mos më pyet për zgjedhje teknike: vendos, shkruaje te `docs/DECISIONS.md`, vazhdo.

## Ligjet e projektit (mos i shkel kurrë)
- Tregti hapet VETËM kur P1+P2+P3+P4 = 100 dhe të gjitha mbrojtjet kalojnë.
- Asnjë pozicion pa SL. Asnjë urdhër i dyfishtë. Asnjë retry i verbër pas timeout.
- Lot-i vjen nga variablat e Railway. Kodi nuk llogarit kurrë lot, rrezik apo balancë.
- MAPEX prek vetëm pozicionet me label "MAPEX".
- Parazgjedhja është `TRADING_MODE=paper`; llogaria live kërkon edhe `CONFIRM_LIVE_ACCOUNT=YES`.
- GEM1 dhe GEM2 janë ligj: çdo rregull i tyre duhet të ekzistojë në kod dhe në `docs/TRACEABILITY.md`.
- Telegram: vetëm hyrjet në treg + alarmet kritike, në shqip.
- Puna përfundon në `main`.
