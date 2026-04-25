# progress.md — DJ's Art Engine
**Last updated:** April 2026  
**Status:** 🟡 IN PROGRESS  

---

## Build Phases

### Phase 0 — Discovery ✅
- [x] On-site meeting at DJ's shop — Windham NH
- [x] Workflow documented (JPEG → EPS → color sep → transparency film)
- [x] Pain points confirmed (INT outsourcing + contractor dependency)
- [x] Software confirmed (Epson Artisan 1430, Photoshop, open to Illustrator)
- [x] Pricing agreed: $1,500 setup + $97/mo
- [x] Spec written (djs-pipeline-spec.md)
- [x] CLAUDE.md written
- [x] Project files initialized

---

### Phase 1 — Backend Pipeline ⬜
- [ ] Repo initialized + pushed to GitHub
- [ ] FastAPI app scaffolded (main.py)
- [ ] Folder structure created per spec
- [ ] `.env` + `.env.example` created
- [ ] `requirements.txt` written with pinned versions
- [ ] `pipeline/vectorize.py` — Vectorizer.ai integration
- [ ] `pipeline/separate.py` — SVG parsing + color extraction
- [ ] `pipeline/export.py` — per-color PDF generation (black on white, 300 DPI)
- [ ] `pipeline/package.py` — ZIP assembly + temp file cleanup
- [ ] End-to-end pipeline test on DJ's actual logo (locally)
- [ ] Output PDFs verified correct (correct colors, correct orientation)

**GATE: Do not proceed to Phase 2 until DJ has printed the output files on his Artisan 1430 and confirmed they burn correctly to screen.**

---

### Phase 2 — Frontend UI ⬜
- [ ] `static/index.html` built (vanilla HTML/CSS/JS)
- [ ] Drag and drop upload working
- [ ] Progress states wired (Uploading → Vectorizing → Separating → Ready)
- [ ] Color preview grid rendering per separation layer
- [ ] ZIP download triggered on button click
- [ ] Warning banner for complex designs (>6 colors)
- [ ] Error state with Photopea fallback link
- [ ] Tested in Chrome on Windows

---

### Phase 3 — Deployment ⬜
- [ ] `render.yaml` created
- [ ] App deployed to Render
- [ ] Environment variables set in Render dashboard
- [ ] Live URL tested end to end
- [ ] `README.md` written (local setup + deploy instructions)
- [ ] URL sent to DJ
- [ ] DJ completes first real job using the tool unassisted

---

### Phase 4 — Validation ⬜
- [ ] DJ processes 5 real customer jobs through the tool
- [ ] Output films produce clean screen burns on all 5
- [ ] No intervention from Anthony required
- [ ] Invoice sent ($1,500 setup fee)
- [ ] Monthly retainer ($97/mo) payment method confirmed
- [ ] Retro written → saved to Obsidian vault

---

## Go-Live Verification Checklist

Walk through this against the live Render URL before handing the tool to DJ. Each item maps to a behavior already implemented in the codebase.

### Deploy plumbing
- [ ] Render build succeeds (no pip install errors in build log)
- [ ] `VECTORIZER_API_ID` and `VECTORIZER_API_TOKEN` set in Render dashboard env vars
- [ ] `https://<render-url>/health` returns `{"status":"ok"}`
- [ ] `https://<render-url>/` serves the HTML frontend (drag-drop visible)
- [ ] Static CSS/JS load (page is styled, dropzone interactive)

### Intake (all 6 formats accepted)
- [ ] JPEG upload processes successfully
- [ ] PNG upload processes successfully
- [ ] HEIC upload (from iPhone) processes successfully
- [ ] WebP upload processes successfully
- [ ] BMP upload processes successfully
- [ ] PDF upload (single-page logo PDF) processes successfully

### Cleanup stage
- [ ] A full-color logo stays in color (check uvicorn log: `classified as color`)
- [ ] A black-on-white logo collapses to grayscale (check log: `classified as monochrome`)

### Color separation
- [ ] Simple 2-color logo → exactly 2 PDFs in ZIP
- [ ] Prime Butcher (Denny Mike's) → correct 4 colors (Red/Green/Blue/White_INK if dark bg)
- [ ] White-on-dark logo → `White_INK_FFFFFF.pdf` appears as a separation layer
- [ ] Logo on white background → white is NOT in the separation list
- [ ] Complex logo (>10 colors) → `X-Warning-Complex-Design: 1` header present
- [ ] Single-color logo → `X-Warning-Only-One-Color: 1` header present

### Output PDFs (open each in Chrome's PDF viewer)
- [ ] All PDFs are grayscale (not color)
- [ ] Ink areas render black, everything else pure white (no inverted film)
- [ ] Page size = original artwork dimensions + 0.25" total on each axis (0.125" bleed each side)
- [ ] PDFs print at 300 DPI from Windows print dialog on Artisan 1430
- [ ] Filenames follow `{ColorName}_{HEX}.pdf` pattern (e.g. `Red_CC2222.pdf`)
- [ ] ZIP contains `color_summary.txt` with name/hex/coverage per color

### Error paths
- [ ] Upload a `.txt` → red banner "Please upload a JPEG, PNG, HEIC, WebP, BMP, or PDF"
- [ ] Upload a 30 MB file → red banner "File must be under 20MB"
- [ ] Temporarily set bad Vectorizer creds → red banner with Photopea fallback link visible and clickable

### Temp hygiene
- [ ] Per-job dir under `TEMP_DIR` is deleted after the ZIP finishes downloading
  (Render's filesystem is ephemeral, but this confirms `BackgroundTask(package.cleanup, ...)` fires)

### Performance
- [ ] Typical logo completes upload → download in under 60 seconds
- [ ] Vectorizer.ai step is the slowest phase (expected — 10–30s)

### DJ handoff
- [ ] DJ prints one separation PDF on his Artisan 1430 to transparency film
- [ ] Burned screen from that film exposes cleanly (no pinholes, no grayscale bleed)
- [ ] DJ completes one real customer job end-to-end with zero help from Anthony

---

## Current Blockers

None — ready to build.

---

## Code Review Hardening (Apr 2026)

A ruthless audit of the codebase produced 22 findings (P0–P2). All were
addressed across three commits:

### Commit 1 (P0 critical) — `e0677ce`
- Cleanup pipeline order flipped: denoise FIRST, sharpen LAST. Previous
  order amplified JPEG noise then tried to remove it.
- Coverage pixel counting moved from Python `sum()` to `np.count_nonzero`.
- Background detection extended from `<rect>` first-child only to any
  `<rect>`/`<path>`/`<polygon>` with bbox ≥98% of viewBox. Fixes silent
  `White_INK` breakage on dark-shirt logos when Vectorizer.ai emits a
  `<path>` background instead of a `<rect>`.
- Basic-auth middleware (`DJ_BASIC_USER`/`DJ_BASIC_PASS`) gating every
  non-`/health` route. Auth is bypassed when env values are blank.
- slowapi rate limiter at 10 req/min per IP on `/process`.
- Content-Length pre-check before reading the request body — prevents
  500 MB malicious uploads from OOMing the Render free tier.
- Intake `_MAX_DIMENSION` 8000 → 3000; PDF render DPI 300 → 150.

### Commit 2 (P0 UX + P1) — `529d920`
- Frontend stepper replaced with a single spinner showing real elapsed
  seconds. The previous fake `setTimeout` stages were misleading DJ when
  Vectorizer.ai stalled.
- SVG parsed exactly once per request (was N+1 times). New
  `mutate_parsed_svg_for_color` deepcopies the cached tree per color.
- Cleanup `_denoise` caps input at 1500 px (downscale → denoise →
  upscale) and skips entirely on images smaller than the template
  window — handles 1×1 favicon edge case.
- Stroke colors now collected as paint sources alongside fills; gradient
  paints (`url(#…)`) detected and surfaced via `X-Warning-Gradients`.
- Stroke-only fallback: logos with no fills but stroked paths use the
  stroke colors as the separation list (`X-Warning-StrokeOnly`) instead
  of returning a 422 error.
- `vectorize.py` retry policy: real `etree.fromstring` parse validation,
  3 attempts max, exponential backoff (2 s → 8 s) on 5xx + network
  errors, `Retry-After` header (capped 30 s) honored on 429.
- `coverage_error` flag in `ColorLayer` + `color_summary.txt`. Render
  failures now show "ERROR" instead of misleading 0.00 % coverage.

### Commit 3 (polish + P1 remainder) — `28edc0d`
- Intake → cleanup → vectorize chain now passes a `PIL.Image` instead of
  re-encoding JPEG between every module. JPEG is encoded EXACTLY once
  via `intake.encode_jpeg` immediately before the API call.
- Per-color PDFs render in parallel via `ThreadPoolExecutor` (4 workers).
  Output order preserved against coverage-sorted layer list.
- Multi-page PDF detection in intake → `X-Warning-MultiPage` header →
  frontend banner "only the first page was processed".
- Startup orphan-temp cleanup (`@app.on_event("startup")`): nukes any
  job dirs in `TEMP_DIR` older than 24 h.
- 500-error message now includes a concrete support phone number
  (placeholder `+1 (603) 555-0000` — replace after first live test).
- Silent `except: pass` around EXIF orientation replaced with a logged
  warning so iPhone photo regressions become diagnosable.
- `.gitattributes` added (`* text=auto eol=lf`) — ends CRLF warnings.
- `ANTHROPIC_API_KEY` reserved (blank) in `.env.example` for the future
  quality-check feature.

### New response headers (frontend listens for these)
| Header | Banner | Triggered by |
|---|---|---|
| `X-Color-Count` | populates badge | always |
| `X-Color-Names` / `X-Color-Hexes` | populates color grid | always |
| `X-Warning-Only-One-Color` | yellow | exactly 1 separation color |
| `X-Warning-Complex-Design` | yellow | >10 separation colors |
| `X-Warning-Gradients` | yellow | gradient fill/stroke detected in SVG |
| `X-Warning-StrokeOnly` | yellow | logo had only strokes (no fills) — stroke colors used |
| `X-Warning-MultiPage` | yellow | uploaded PDF had >1 page; only page 1 processed |

### New env vars
```
DJ_BASIC_USER=     # username for basic auth (blank = auth disabled)
DJ_BASIC_PASS=     # password for basic auth (blank = auth disabled)
ANTHROPIC_API_KEY= # reserved — future quality-check feature
```

---

## Notes & Decisions Log

| Date | Decision | Reason |
|---|---|---|
| Apr 2026 | PDF output over EPS | DJ prints directly from Windows print dialog — no Illustrator needed |
| Apr 2026 | Delta-E color matching over RGB euclidean | More perceptually accurate, handles JPEG compression artifacts better |
| Apr 2026 | No database in v1 | Single user tool, no persistence needed, keeps build fast |
| Apr 2026 | Render for hosting | Simple Python deploy, $7/mo, reliable enough for internal tool |
| Apr 2026 | $1,500 + $97/mo | Family friend rate — market rate for other shops is $297/mo |
| Apr 2026 | Photopea as fallback | Free, browser-based, no install — replaces Photoshop for manual edge cases |

---

## Test Logos

Use these real DJ customer logos to validate the pipeline before deployment:

- [ ] Prime Butcher (Denny Mike's) — the 4-color job discussed in discovery
- [ ] One additional logo with white ink on dark background (white ink edge case)
- [ ] One logo with >6 colors (complex design warning test)
- [ ] One clean simple 2-color logo (baseline happy path)

---

## V2 Backlog (Do Not Build Now)

- Job history log with searchable reorder lookup
- Customer-facing upload portal (customers submit their own art)
- Email delivery of output ZIP
- Embroidery digitizing module
- Automated invoice generation
- Pricing calculator
- Multi-shop SaaS version at $297/mo
