# progress.md — DJ's Art Engine
**Last updated:** May 12, 2026  
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

## Bad-Input Bulletproofing (May 2026)

A live test on 2026-05-11 surfaced the real-world failure mode: customer
delivered a 158×150 px HEIC ("HCB Logo") — a thumbnail-sized file ~24× below
print resolution. The pipeline ran cleanly and produced separation PDFs, but
the source had no detail to recover. DJ confirmed this is typical — customers
routinely send thumbnails, email-compressed JPEGs, and phone photos of
existing shirts.

This pass adds three pre-vectorize stages so the pipeline can (a) tell DJ when
the input is too small, (b) automatically upscale low-resolution sources with
AI super-resolution, and (c) get a second opinion from Claude vision on what's
actually in the image before burning a Vectorizer.ai credit. It also adds a
Claude-driven crop step for the "phone photo of a shirt" case, avoiding the
need to host rembg's 170 MB model on Render's 512 MB free tier.

### New pipeline shape
```
intake.decode (now captures original_size + low_resolution flag)
    ↓
[asyncio.gather]
    ├── upscale.maybe_upscale  ← Replicate Real-ESRGAN if needed
    └── quality_check.assess   ← Claude vision verdict
    ↓
QC verdict gates:
    - printable=False → 422 short-circuit with {dj_message, customer_ask}
    - photo_of_object + bbox → PIL crop before vectorize
    ↓
cleanup → vectorize → separate → export → package (unchanged)
```

### Failure UX contract
Every rejection or warning must convert a dead-end into a forwardable email.
The QC tool schema enforces two mandatory fields when an image is rejected:
- **dj_message** — short, specific to *this* image (not boilerplate)
- **customer_ask** — copy-pasteable sentence naming file types and a
  resolution number

The frontend renders the rejection as a dedicated panel with the dj_message
as the banner, the customer_ask in a readonly `<textarea>`, and a one-click
copy button — converting frustration into a 5-second customer email.

### New modules
- `pipeline/upscale.py` — direct httpx → Replicate Real-ESRGAN. Triggers only
  when `long_edge < 1024 AND short_edge < 800`. Caps output at 2048 px long
  edge. 45 s hard timeout. Fails open: any error returns the original image
  unchanged. Avoids the replicate-python SDK due to its known httpx-Proxy
  ImportError on Python 3.13+.
- `pipeline/quality_check.py` — direct httpx → Anthropic Messages API with
  tool-use schema enforcement. Default model `claude-haiku-4-5`. 20 s hard
  timeout. Fails open: returns permissive default verdict on any error.
  Downgrades unprintable verdicts to permissive when Claude returns empty
  `dj_message`/`customer_ask` rather than show DJ an empty banner.

### Extended modules
- `pipeline/intake.py` — `IntakeResult` now carries `original_size` and a
  `low_resolution: "none" | "soft" | "hard"` classification.
- `pipeline/config.py` — new env getters: `get_replicate_token`,
  `get_anthropic_key`, `get_qc_model`, `get_upscale_enabled`.
- `main.py` — new orchestration block runs upscale+QC in parallel before
  cleanup; 422 short-circuit body includes structured rejection fields;
  bbox-based crop step for photo-of-object inputs; per-job
  `cost_estimate=$0.0NNN` log line.
- `static/index.html` — rejection panel with textarea + copy button;
  info-banner style for `X-Stage-Upscaled`; new warning banners for
  low-resolution, upscale-skipped, photo-of-object, and illegible-text.

### New response headers
| Header | Banner | Triggered by |
|---|---|---|
| `X-Warning-LowResolution` | yellow (with dims) | source `min(w,h) < 1000` |
| `X-Stage-Upscaled` | blue (info) | Real-ESRGAN ran successfully |
| `X-Warning-UpscaleSkipped` | yellow | upscale was attempted and failed |
| `X-Warning-PhotoOfObject` | yellow | QC detected photo-of-object input |
| `X-Warning-IllegibleText` | yellow | QC flagged unreadable small text |
| `X-QC-Model` | (debug only) | model used for QC call |

### New env vars
```
REPLICATE_API_TOKEN=         # required for AI upscaling
ANTHROPIC_API_KEY=           # required for Claude vision QC
QC_MODEL=claude-haiku-4-5    # override only if Haiku misclassifies
UPSCALE_ENABLED=true         # kill-switch without unsetting the token
```

### Cost model
| Stage | Service | Cost | When |
|---|---|---|---|
| Upscale | Replicate Real-ESRGAN | ~$0.005 | only when source < 1024×800 |
| QC | Anthropic Haiku 4.5 | ~$0.001-0.005 | every job |
| Vectorize | Vectorizer.ai | 1 credit (~$0.20) | every job that passes QC |

Worst-case low-res job: ~$0.21. Typical job: ~$0.20-0.205.
At 100 jobs/month, marginal cost over the previous pipeline: ~$1/month.

### Explicitly deferred
- LAB k-means quantization (re-evaluate if upscaled photos still hit `complex_design`)
- vtracer local fallback (only if Vectorizer.ai outages become a real failure mode)
- BRISQUE/NIQE numerical quality (Claude QC is a superset; add only for analytics)
- rembg (replaced by Claude bbox + PIL crop; revisit only if bbox misclassifies)
- Vectorizer.ai parameter tuning per QC verdict (cheap follow-up spike)

---

## Demo Recovery + Production-Grade Color Separation (May 12, 2026)

Live demo at DJ's shop on 2026-05-12 surfaced **two distinct crises** that
fed into each other. Both shipped to production today across three commits.

### Crisis 1 — Render OOM during demo (Commit `e09d784`)

5 PM live demo failed with a 502. Render OOM email confirmed: the
**Bulletproof pipeline** commit (`df16183`) had introduced `asyncio.gather`
running Replicate upscale + Claude vision QC **in parallel** on a 512 MB
Render free tier. Combined with the ~150-200 MB import baseline
(pillow + opencv + lxml + reportlab + httpx + slowapi + pillow-heif),
peak memory exceeded the ceiling on a real customer JPEG. Worked locally
(32 GB RAM); died in prod.

Fixes shipped:
- **Serialize upscale + QC.** Changed `asyncio.gather([upscale, qc])` →
  sequential await. QC first (cheap, decides whether to spend Replicate
  + Vectorizer credit on a screenshot-shaped upload). Upscale second,
  only if QC approves. Latency cost: +10-20 s worst case per job; fine
  for an internal tool.
- **Drop intermediate references between stages.** Explicit `del` +
  `gc.collect()` after the JPEG encode (drops `cleaned_image`) and after
  vectorize returns (drops `jpeg_bytes`). `intake_result.image = None`
  after upscale completes when `was_upscaled=True`. Captures
  `pre_upscale_size` as a local before nulling the dataclass slot, so
  bbox rescaling + warning headers still work.
- **PDF render thread pool 4 → 2.** Each thread renders a ~38 MB raster
  at 300 DPI; four in parallel was the other peak memory hotspot the
  initial diagnosis missed.
- **`QC_ENABLED` kill-switch** (mirrors `UPSCALE_ENABLED`). Env-var panic
  switch to skip the Claude QC stage without unsetting `ANTHROPIC_API_KEY`.
  Flip from phone via Render env vars dashboard in ~30 seconds if memory
  goes sideways again on a future build.
- **Vectorizer.ai 5xx response body capture.** Retry path at
  `pipeline/vectorize.py` now captures `response.text[:500]` on 5xx (was
  only logging the status code). Future real Vectorizer failures will be
  diagnosable from Render logs.

**Render plan upgrade** done in parallel by DJ: Free 512 MB → Standard
**2 GB**, $25/mo. Confirmed inside the $97/mo retainer math. Note that
the $7/mo Starter tier is a CPU upgrade only — same 512 MB RAM as Free —
and was rejected on those grounds.

### Crisis 2 — Color separation quality unusable on real logos (Commits `1ef599a`, _next commit_)

Memory fix shipped, then live testing on Standard tier exposed pre-existing
color-quality bugs masked by the OOM:

- **Optimus Business Solutions logo** (clean 4-color flat: orange gradient
  + light gray + dark gray + black) → UI said "4 colors detected" but the
  ORANGE FILM WAS COMPLETELY ABSENT. All 4 films were grayscale.
- **Complete Junk Removal logo** (multi-color shaded illustration) → 10+
  fragmented films, several near-empty, browns split across multiple
  separations that should be one.

Diagnosis spanned three different files. Three root causes:

#### Bug A — Gradient fills silently dropped (Commit `1ef599a`)

`pipeline/separate.py::_extract_fill()` only normalized direct hex
values. When Vectorizer.ai represented a region with a gradient (e.g.,
the orange sun on the Optimus logo: `fill="url(#linearGradient123)"`),
the function returned `None`. The path's color never entered
`fill_counts` so no film was produced for that region. Pipeline set
`gradients_detected=True` warning header but emitted no separation for
the gradient region — silently dropping it.

**Fix:** new `separate.resolve_gradient_refs(svg_bytes)` public helper.
Walks `<linearGradient>`/`<radialGradient>` defs, averages each
gradient's stop colors in RGB, rewrites every `fill="url(#X)"` /
`stroke="url(#X)"` reference (and inline `style="fill:url(...)"`) to the
representative hex. Wired into `main.py` immediately after
`vectorize.vectorize(...)` returns — single point of preprocessing
ensures debug `vectorized.svg`, separation, and per-color PDF rendering
all see the resolved SVG.

**Lossy by design** (documented in code): a yellow-to-orange gradient
collapses to a single mid-orange. Screen-printing presses cannot
reproduce gradients on a single screen anyway, so a flat representative
color is what actually prints. The orange-looks-slightly-off appearance
on the rendered film is expected, not a bug.

V1 scope limitations documented in `_build_gradient_index`:
`xlink:href` gradient inheritance not followed (rare); `stop-opacity`
ignored; nested gradient refs not followed.

#### Bug B — Dedupe threshold too tight (Commit `1ef599a`)

`_DEDUPE_THRESHOLD = 10.0` in separate.py and `_MATCH_THRESHOLD = 15.0`
in color_math.py. At ΔE 10, two browns that look identical on cotton
still survived as separate films. Anti-aliasing pixels along color
boundaries became their own near-empty separations. Cotton + ink
physically cannot reproduce ΔE 10 differences; industry threshold for
"noticeably different in printed work" is ~ΔE 20-25.

**Fix:** bumped both thresholds to **25**. Match threshold tracks dedupe
so cluster members co-render on the same film at render time (a wider
dedupe with a narrower match would let paths fall through the cracks).
Background filtering broken out as a separate `_BACKGROUND_MATCH_THRESHOLD
= 10.0` so we don't over-aggressively delete near-bg logo paths just
because dedupe got wider.

#### Bug C — No hard cap on color count (Commit `1ef599a`)

Even after dedupe, complex shaded illustrations could yield 10-15
distinct color families. Screen printers run 4-8 color jobs. Pipeline
had no logic to enforce a cap.

**Fix:** new `_kmeans_consolidate(canonical_counts, k)` in separate.py.
LAB-space k-means weighted by path count (capped at 100 per row to bound
sample-expansion memory). Cluster representative selection is a
deterministic three-key sort: highest count → closest to centroid in
LAB → lexicographic hex (full determinism across runs). Called after
`_filter_color_pool` when `len(color_pool) > _MAX_PRINT_COLORS = 8`.
No-op when dedupe already brought us below the cap.

#### Bug D — Cleanup misclassifying low-saturation-coverage color logos (_next commit, May 12 evening_)

After 1ef599a deployed, the Optimus logo STILL came back with no orange
and only 2 films (Black + Gray). Render logs surfaced the smoking gun:

```
[INFO] pipeline.cleanup: Cleanup saturation: mean=5.38 high_sat=0.038 ...
[INFO] pipeline.cleanup: Cleanup: image classified as monochrome, converting to grayscale.
```

`pipeline/cleanup.py::_is_effectively_monochrome` was converting the
whole image to grayscale BEFORE vectorize. Decision logic was:
```python
return (mean_sat < 30) and (high_sat_fraction < 0.05)
```
For the Optimus logo, the orange occupies only ~3.8% of non-near-black
pixels (`high_sat_fraction = 0.038`) — just barely below the 5%
threshold. With both conditions true → grayscale conversion → orange
stripped → never reaches vectorize → can't produce an orange film no
matter what the downstream logic does.

The misclassification was geometric: a logo where the *colored region
is small* relative to the canvas (e.g., a brand logo with a small
saturated accent on a white field) sat between 2% and 5% coverage and
got miscategorized as black-on-white.

**Fix:** added a **third saturation signal** + tightened the existing
moderate-coverage threshold. New decision logic:
```python
return (
    mean_sat < 30
    and high_sat_fraction < 0.02    # was 0.05
    and strong_sat_fraction < 0.003  # NEW: catches small unambiguous color regions
)
```
`strong_sat_fraction` counts pixels with saturation > 180 (out of 255)
— anything that strongly saturated is intentional color, not JPEG
chroma noise. Even a 0.3% pocket of strongly-saturated pixels is enough
evidence to force color-mode processing. Synthetic test verified: a 1%
orange region on white + black text correctly classifies as color;
pure black-on-white correctly classifies as monochrome.

### Verification on Render (Standard 2 GB)

- e09d784: pipeline runs without OOM on real customer images
- 1ef599a: Optimus logo color count went 4 (e09d784, all grayscale) → 2
  (1ef599a, still no orange). Dedupe fix working but the cleanup bug
  prevented gradient code from ever firing. Junk Removal logo color
  count went 14 → 4 (proves k-means and dedupe both functional on the
  case where cleanup correctly kept RGB).
- Cleanup fix (next commit): expect Optimus logo to produce 4 films
  with one named Orange/Yellow at ~`#FFA100` capturing the sun crescent
  region.

### New env vars

```
QC_ENABLED=true              # kill-switch for Claude QC stage (Bug-1 fix)
```

### New / changed constants (for future tuning)

| Constant | Old | New | Where |
|---|---|---|---|
| `_DEDUPE_THRESHOLD` | 10.0 | 25.0 | `pipeline/separate.py` |
| `_BACKGROUND_MATCH_THRESHOLD` | (used `_DEDUPE_THRESHOLD`) | 10.0 | `pipeline/separate.py` (new) |
| `_MAX_PRINT_COLORS` | (no cap) | 8 | `pipeline/separate.py` (new) |
| `_KMEANS_WEIGHT_CAP` | — | 100 | `pipeline/separate.py` (new) |
| `_MATCH_THRESHOLD` | 15.0 | 25.0 | `pipeline/color_math.py` |
| `_SATURATION_COVERAGE_THRESHOLD` | 0.05 | 0.02 | `pipeline/cleanup.py` |
| `_SATURATION_STRONG_FRACTION_THRESHOLD` | — | 0.003 | `pipeline/cleanup.py` (new) |
| `_PDF_THREAD_POOL_SIZE` | 4 | 2 | `main.py` |

### Render plan

| Plan | Memory | $/mo | Status |
|---|---|---|---|
| Free | 512 MB | $0 | Insufficient for current pipeline (OOMs on real images) |
| Starter | 512 MB | $7 | **Trap — same RAM as Free, CPU upgrade only** |
| **Standard** | **2 GB** | **$25** | **Current. Comfortable inside $97/mo retainer** |
| Pro | 4 GB | $85 | Overkill at current volume (30-40 jobs/mo) |

### Explicitly deferred (still)

- Halftone simulation for gradient regions (current flat-fill collapse is
  good enough; halftone is a v2 enhancement when DJ asks)
- Manual color palette override in UI (user picks the 6 colors before
  the films render) — k-means picks for v1
- xlink:href gradient inheritance — rare in Vectorizer.ai output

---

## Notes & Decisions Log

| Date | Decision | Reason |
|---|---|---|
| Apr 2026 | PDF output over EPS | DJ prints directly from Windows print dialog — no Illustrator needed |
| Apr 2026 | Delta-E color matching over RGB euclidean | More perceptually accurate, handles JPEG compression artifacts better |
| Apr 2026 | No database in v1 | Single user tool, no persistence needed, keeps build fast |
| Apr 2026 | Render for hosting | Simple Python deploy, reliable enough for internal tool |
| May 2026 | Render Standard ($25/mo, 2 GB) over Starter ($7/mo, 512 MB) | Starter has the same RAM as Free — pure CPU upgrade. Real-ESRGAN + Claude QC need the headroom. Cost still inside the $97/mo retainer. |
| May 2026 | Resolve SVG gradients to flat representative hex pre-separation | Vectorizer.ai emits `fill="url(#X)"` for shaded regions; without resolution the entire region drops out of the films. Lossy by design — screens can't reproduce gradients anyway. |
| May 2026 | Add `strong_sat_fraction` (sat > 180) as a third monochrome-classification signal | Mean + moderate-coverage thresholds alone miss logos where the colored region is geometrically small (e.g., a small orange accent on a white field). |
| May 2026 | k-means cap at 8 colors after Delta-E dedupe | Industry max for screen-printing jobs. Dedupe alone can leave 10-15 colors on shaded illustrations. |
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
