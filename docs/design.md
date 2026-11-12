# LedgerLab design notes

LedgerLab is a local accounting workbench. The payment table, compact totals, and neighboring create/refund forms connect an action with its ledger effect. Journal and reconciliation are complete working views. Styles live in `web/styles.css`; behavior and status text live in `web/app.js` and `web/index.html`.

## Palette and type

| Role | Value |
| --- | --- |
| Canvas / surface / rail | `#f4f6f5` / `#fff` / `#eef2f0` |
| Ink / supporting text / divider | `#1c2929` / `#596463` / `#dce3e0` |
| Action and focus / selected row | `#2856c2` / `#eaf0ff` |
| Input border / placeholder | `#c7d1cc` / `#69746f` |
| Positive / warning / error tag text | `#286044` / `#79500e` / `#9a3530` |

The bundled font is Noto Sans, aliased as `Instrument` in CSS, with a generic sans-serif fallback and `font-display: swap`. Regular and bold TTF files and their OFL license are in `web/assets`. Body text is 15px/1.55; page headings are 30px/1.25, reduced to 27px on phones; panel headings are 17px. Buttons and table rows use 13px; supporting text and labels use 12px. Amounts use tabular numerals. IDs and CSV use `ui-monospace, monospace`.

## Layout and controls

A 205px rail accompanies a workspace capped at 1700px. The payment action pane is 288px wide; reconciliation starts with a 300px CSV pane. At 1150px, the rail becomes 170px and payment forms move below the table in two columns. At 760px, the rail becomes a top region, navigation scrolls horizontally, forms and reconciliation stack, and workspace padding becomes 20px 16px 30px.

Panels are flat white with a 1px divider border and 12px corners. Buttons use 7px corners and a 39px minimum height; inputs use 6px corners and a 41px minimum height. Selected navigation is white with accent text. Selected payment rows use the soft accent. Primary buttons darken on hover; disabled buttons use 0.46 opacity. There are no shadows.

## States, tables, and keyboard

- Native buttons expose view and payment selection with `aria-pressed`. Payment inspection is the named button within a row. Rebuilding rows preserves focus.
- Tables preserve aligned currency columns and scroll inside named, focusable regions. A sideways-scroll hint appears on phones. Reconciliation details may wrap.
- Focus uses a 3px blue outline and 3px offset; table outlines are inset. A focus-visible skip link enters the workspace.
- Pending work disables actions and marks forms `aria-busy`. Errors retain input and use alert notices. Successful results use status notices. Last payment and unconfirmed refund requests have explicit retry actions.
- Empty states explain the next action. Existing data prevents sample seeding. Status tags contain readable text.
- Notices reveal over 0.25s only when reduced motion is not requested. Tables and totals do not animate.

## Provenance

Payments are simulated; no real money moves. Seeding reports eight synthetic payments and two refunds, and the seed control identifies loaded synthetic samples. Sample reconciliation contains intentional discrepancies. Keep those labels, USD units, exact result states, and backend-derived totals visible. Do not present this demo as production processing or historical financial activity.
