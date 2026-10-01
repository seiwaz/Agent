# SP2L Console — design system (source of truth)

Produced with the `ui-ux-pro-max` skill (2026-09-26): `--design-system "fintech trading monitoring
dashboard real-time data-dense dark" --density 9 --motion 2 --variance 3`, plus `--domain style`,
`--domain color` and `--domain chart` searches.

| Aspect | Decision | Source |
|---|---|---|
| Style | **Data-Dense Dashboard**: 12-col grid, 8px gap, 12px card padding, 12–14px tables, sticky headers, 36px rows. The generator's "Exaggerated Minimalism" suggestion was rejected: its oversized type and whitespace contradict density 9 for an ops console. | style domain, result 1 |
| Palette (dark, default) | bg `#0F172A`, card `#222735`, muted `#272F42`, muted-fg `#94A3B8`, border `#334155`, fg `#F8FAFC`, primary `#F59E0B`, accent `#8B5CF6`, destructive `#EF4444` | color domain "Fintech/Crypto" |
| Palette (light) | bg `#F8FAFC`, card `#FFFFFF`, border `#CBD5E1`, fg `#0F172A`, primary `#B45309` (amber darkened to meet 4.5:1) | derived for AA |
| Status | PASS `#26A69A` / FAIL `#EF5350` (dark); `#047857` / `#B91C1C` (light); always paired with a text label and an SVG icon, never color alone | chart + ux "Color Only" |
| Typography | Fira Code (numerics, headings), Fira Sans (text), with system mono/sans fallbacks | typography result |
| Chart | Lightweight Charts candlesticks: bull `#26A69A` filled, bear `#EF5350` hollow (colorblind-safe), ≤ 500 candles, with a data-quality strip and a table fallback | chart domain |
| Motion | subtle only (≤ 200ms), `prefers-reduced-motion` respected; no decorative animation | dial motion 2 |
| Anti-patterns | light mode default, color-only status, emoji icons, horizontal page scroll (tables scroll inside their card) | generator + ux |

**Hard rule (UI-02):** the frontend renders backend truth only. No thresholds, gate math or state derivation lives in `web/`; `tests/ui/test_no_logic_in_web.py` enforces it.

---

## v2 — human-first trading dashboard (2026-09-27, supersedes the rows above where they differ)

Driven by the owner's UX redesign brief and `ui-ux-pro-max` (`--design-system "trading dashboard
fintech calm professional data dense" --density 7`, `--domain typography "fintech banking
professional clean readable tabular numbers"`, `--domain ux "empty state status indicator
progressive disclosure dashboard"`). The generator again proposed Fira Code headings; rejected,
because the brief asks to reduce the developer-console look.

| Aspect | Decision |
|---|---|
| Information architecture | 5 sections: Dashboard (now), Candidates (why accepted/rejected), History (what happened), Analytics (performance; counterfactual separate), Diagnostics (technical health). Hash routes `#/dashboard`, `#/candidates/<key>`, … |
| Dashboard order | status tiles (Market data / Strategy / Shadow / Real trading) → current blocker → chart + side column (position, strategy, market data) → active setup (only when one exists) → Shadow |
| Palette (dark) | bg `#111418`, surface `#181c22`, surface-2 `#1f252d`, border `#2a313b`, text `#e7e9ec`, text-2 `#9ba4b0`; ok `#4fb393` (muted teal), bad `#e27a70` (muted coral), warn `#ddaa55` (amber), info `#74a7da` (muted blue) |
| Palette (light) | bg `#f4f5f7`, surface `#ffffff`, text `#1b222c`, ok `#1d7a5b`, bad `#b3443a`, warn `#8f5e0f`, info `#2b62a5` (AA on white) |
| Tones | ok = healthy/ready · warn = attention/degraded · bad = genuine fault/unsafe · info = in progress · neutral = waiting/inactive. Always icon + text, never color alone |
| Typography | IBM Plex Sans 15px body / 16–22px headings, sentence case; IBM Plex Mono only for prices, quantities, PnL, timestamps and IDs |
| Vocabulary | human labels first (backend `presentation.py`), exact internal code in tooltip / "Internal codes" line / technical `<details>` |
| Time | viewer's local time zone everywhere (chart axis included), labelled in the footer; UTC in tooltips |
| Responsive | 1440: 2.2fr/1fr chart row; ≤1100: single column, side cards 2-up; ≤900: status 2×2, nav full-width; ≤640: bottom navigation (5 items, 52px targets), 2-col flow, tables scroll inside their own container only |
| Verification | `tests/ui/test_responsive.py`: states A–J × 1440/768/390 in real Chrome, no page overflow, no card overlap/overflow, no collapsed card; screenshots in `docs/ui-screenshots/` |
