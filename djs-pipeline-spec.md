# DJ's Custom Clothing — Automation Pipeline Spec
**Version:** 1.0  
**Date:** April 2026  
**Builder:** Optimus Business Solutions  
**Client:** DJ's Custom Clothing, Windham NH  

---

## Environment Variables

All secrets and config live in `.env`. Never hardcoded. Never committed to git.

```env
VECTORIZER_API_ID=your_api_id_here
VECTORIZER_API_TOKEN=your_api_token_here
MAX_FILE_SIZE_MB=20
OUTPUT_DPI=300
TEMP_DIR=/tmp/djs-art-engine
```

Claude Code must generate a `.env.example` with all keys present but values blank. The `.env` file must be in `.gitignore`.

---

## Folder Structure

```
djs-art-engine/
├── main.py                  # FastAPI app entry point
├── pipeline/
│   ├── __init__.py
│   ├── vectorize.py         # Vectorizer.ai API call
│   ├── separate.py          # Color identification + separation logic
│   ├── export.py            # PDF generation per color layer
│   └── package.py           # ZIP assembly
├── static/
│   └── index.html           # Single page frontend UI
├── tmp/                     # Runtime temp files — gitignored
├── requirements.txt
├── .env                     # Real secrets — gitignored
├── .env.example             # Keys with blank values — committed
├── .gitignore
├── CLAUDE.md
├── progress.md
└── README.md
```

---

## Output Format Decision

**Format: PDF**

Rationale: DJ prints directly from his Epson Artisan 1430. PDF is the most reliable format for maintaining exact dimensions and resolution through the Windows print dialog. EPS requires Illustrator or Photoshop to open before printing — adds a step. PDF prints directly.

Each separation file is a single-page PDF at exact artwork dimensions, 300 DPI, grayscale.

---

## Color-to-Film Conversion Logic (Critical)

Screenprinting transparency films work on a simple principle: **black blocks UV light during screen exposure, clear lets it through.** This means:

- Where ink should print → **black on the film**
- Where no ink goes → **white (clear) on the film**

This is NOT a standard grayscale conversion. The pipeline must:

```python
# For each color layer:
# 1. Create white canvas (same dimensions as artwork)
# 2. Find all pixels matching this color (within tolerance of delta-E 15)
# 3. Paint those pixels BLACK on the canvas
# 4. Everything else stays WHITE
# 5. Export as grayscale PDF at 300 DPI
# 6. DO NOT invert — black = ink, white = clear, this is correct for positive films
```

**Tolerance handling:**
- Use delta-E color distance (not RGB euclidean) for color matching
- Delta-E threshold: 15 (catches anti-aliasing and compression artifacts)
- White (FFFFFF) and near-white (delta-E < 10 from white) are always excluded from separation list — they are background, not ink colors

**Special case — white ink:**
White ink IS a real color in screenprinting (printed on dark shirts). If the vectorized file contains white as a fill color on a non-white background path, it must be included as a separation layer and labeled "White_INK" to distinguish it from background white.

---

## Problem Statement

85% of DJ's jobs require two outsourced manual steps before a single shirt gets printed:

1. JPEG → EPS via INT (India) — $8–15, 12hr turnaround
2. EPS → Color separated transparency films via contractor — $15, 10min (when available)

Total cost per job: ~$30. Total delay: up to 24 hours. Affects nearly every order.

**Goal:** Replace both outsourced steps with a single web tool. JPEG in. Print-ready color separated transparency files out. No humans in the middle.

---

## System Name

**DJ's Art Engine** — internal tool, not customer-facing

---

## Architecture Overview

```
[Browser UI]
     ↓
[FastAPI Backend — Python]
     ↓              ↓
[Vectorizer.ai]   [Color Separation Engine]
     ↓              ↓
        [ZIP Output]
             ↓
    [DJ downloads + prints]
    [Epson Artisan 1430 → Transparency Films]
```

No database. No auth system (v1). No external CRM. Just upload → process → download.

---

## Tech Stack

| Layer | Tool | Why |
|---|---|---|
| Backend | Python + FastAPI | Simple, fast, async-friendly |
| Vectorization | Vectorizer.ai API | Best raster-to-vector API available |
| Color Separation | Pillow + OpenCV | Proven image processing, full control |
| Output generation | ReportLab or PIL | PDF per color layer |
| Frontend | Single HTML file | No framework needed for v1 |
| Hosting | Railway or Render | Simple Python app deploy, $5–7/mo |
| File handling | Temp files + ZIP | No storage needed, process and discard |

---

## User Flow (DJ's Perspective)

```
1. DJ opens the tool URL in any browser
2. Clicks "Upload Logo" — selects JPEG from his computer or phone
3. Optionally types job name (ex: "Prime Butcher - Denny Mikes")
4. Clicks "Process"
5. Tool shows progress: Vectorizing... Separating Colors... Ready
6. Tool shows preview: thumbnail of each color layer with color name label
7. DJ clicks "Download ZIP"
8. ZIP contains one PDF per color, named by color (Red.pdf, Green.pdf, etc.)
9. DJ opens each PDF, prints on Epson Artisan 1430 to transparency film
10. Done — straight to screen burning
```

Total time from upload to download: **under 60 seconds.**

---

## Backend — Step by Step

### Step 1 — Receive Upload
```
POST /process
- Accept: multipart/form-data
- Fields: file (JPEG/PNG), job_name (optional string)
- Validate: file type, file size (max 20MB)
- Save to: /tmp/{uuid}/original.jpg
```

### Step 2 — Vectorize (Vectorizer.ai API)
```
POST https://vectorizer.ai/api/v1/vectorize
- Input: original.jpg
- Output: output.svg (or .eps — confirm with DJ which his printer prefers)
- Mode: "production" for quality, not preview
- Error handling: if vectorization fails → return error with Photopea fallback link
- Save to: /tmp/{uuid}/vectorized.svg
```

**Vectorizer.ai pricing:** ~$0.20–0.40 per image on pay-as-you-go. Still vastly cheaper than $8–15 to INT.

### Step 3 — Color Identification
```python
# Parse SVG paths and extract unique fill colors
# Group paths by color value
# Filter out near-duplicates (within delta-E threshold of 10)
# Sort by coverage area (largest color first)
# Return: list of hex colors + associated paths
```

**Edge cases to handle:**
- White backgrounds — detect and exclude from separation list
- Near-white colors — flag for DJ review
- More than 6 colors — show warning ("Complex design — review before printing")
- Gradients — flatten to nearest solid, flag for DJ

### Step 4 — Generate Separation Files
```python
# For each unique color:
#   - Create new canvas (same dimensions as original)
#   - Render only the paths for that color
#   - Convert to grayscale (screenprinting films are black/clear)
#   - Invert if needed (ink areas should be black on film)
#   - Save as high-res PDF (300 DPI minimum)
#   - Filename: {color_name}_{hex}.pdf
#     ex: Red_CC2222.pdf, White_FFFFFF.pdf
```

### Step 5 — Package and Return
```
- Create ZIP: {job_name}_{timestamp}.zip
- Include: one PDF per color + color_summary.txt
- color_summary.txt lists: color name, hex value, coverage %
- Return ZIP as download response
- Delete /tmp/{uuid}/ after response sent
```

---

## Output File Spec

Each color separation file must be:
- **Format:** PDF (compatible with Epson Artisan 1430 print dialog)
- **Resolution:** 300 DPI minimum
- **Color mode:** Grayscale (black ink on clear film)
- **Size:** Match original artwork dimensions exactly
- **Bleed:** 0.125" on all sides (standard screen printing)

---

## Frontend — Single Page UI

No framework. HTML + vanilla JS. Hosted as static file.

**Elements:**
- Logo / tool name ("DJ's Art Engine")
- Upload dropzone (drag and drop or click to browse)
- Job name text field (optional)
- Process button
- Progress indicator (3 states: Vectorizing / Separating / Ready)
- Color preview grid — one thumbnail per color with label
- Color count badge ("4 colors detected")
- Warning banner if >6 colors or gradient detected
- Download ZIP button
- Photopea fallback link ("Need to edit manually? Open in Photopea →")

**No login. No account. Just the tool.**

---

## Error Handling

| Error | Response |
|---|---|
| Bad file type | "Please upload a JPEG or PNG" |
| File too large | "File must be under 20MB" |
| Vectorizer.ai fails | "Vectorization failed — open in Photopea to process manually" + link |
| 0 colors detected | "Couldn't detect colors — try a higher quality image" |
| Only 1 color detected | Show result but warn "Only 1 color found — verify this is correct" |
| >10 colors detected | "Too many colors detected — this may be a photo, not a logo" |
| Server error | "Something went wrong — text Anthony at [number]" |

---

## Photopea Fallback

When automation fails or DJ needs manual control:

```
https://www.photopea.com/#{"files":["[EPS_URL]"]}
```

The system saves the vectorized EPS to a temp URL and opens it directly in Photopea. DJ sees his clean vector file already loaded, ready to manually separate if needed. No Photoshop license. No install. Free.

---

## What This Does NOT Do (V1 Scope Limits)

- ❌ Does not handle embroidery digitizing (DST/PES/EMB) — separate problem
- ❌ Does not generate invoices or quotes
- ❌ Does not communicate with customers
- ❌ Does not connect to Epson printer directly
- ❌ Does not store job history (v2 feature)
- ❌ Does not handle complex photographic artwork — logos only

---

## V2 Features (Post-Validation)

- Job history log — every processed logo saved with job name + date
- Reorder lookup — "pull up Prime Butcher from last March"
- Direct email delivery — processed ZIP emails to DJ automatically
- Customer-facing upload portal — customer submits their own artwork
- Embroidery digitizing module (if API becomes available)
- Pricing calculator module

---

## Hosting & Cost

| Item | Cost |
|---|---|
| Render or Railway (backend) | $7/mo |
| Vectorizer.ai API (est. 100 jobs/mo) | $20–40/mo |
| Domain (optional) | $12/yr |
| **Total** | **~$30–50/mo** |

DJ currently pays $30 **per job** in outsourcing. At 10 jobs/week this tool pays for itself in the first two days of every month.

---

## Pricing to DJ

| | Amount |
|---|---|
| Build fee (one-time) | $1,500 |
| Monthly retainer | $297/mo |
| What's included | Tool hosting, API costs covered, maintenance, support |

Framing: *"You're currently paying $30/job to two people. This replaces both of them for $297/month flat. At 10 jobs a week, you break even in the first 3 jobs of the month."*

---

## Build Timeline

| Week | Deliverable |
|---|---|
| Week 1 | Python pipeline working locally — vectorize + separate on DJ's actual logo |
| Week 1 | Demo ready — show DJ the output files, have him test print on his Artisan 1430 |
| Week 2 | Frontend UI built, hosted on Render, accessible via URL |
| Week 2 | Error handling + Photopea fallback wired |
| Week 2 | Hand DJ the URL — he uses it live on real jobs |
| Week 3–4 | Iteration based on real job feedback |

**Demo before full build.** Get the pipeline working on one of DJ's real logos and have him print the output films before building the UI. That's the validation gate.

---

## Validation Criteria (Definition of Done)

The tool is production-ready when:

- [ ] Vectorizer.ai converts DJ's real customer JPEGs cleanly
- [ ] Color separation produces correct number of layers for a known job
- [ ] Output PDFs print cleanly on Epson Artisan 1430 on transparency film
- [ ] DJ burns a screen from the output and the image is clean
- [ ] Whole process takes under 60 seconds
- [ ] DJ can operate the tool without any help from Anthony

**That last one is the real test.**

---

## Notes from Discovery Session

- Epson Artisan 1430 is the printer — most common in the industry
- DJ prints on transparency films
- Currently uses Photoshop — open to switching to Illustrator output if cleaner
- Color separation contractor charges $15/job, 10min turnaround when available — not reliable
- INT (India) charges $8–15/job for EPS conversion, 12hr turnaround
- DJ estimates this affects ~85% of all jobs
- 4-color job shown as example: Red, Green, Blue, White — each gets its own film
- Invoicing too complex to automate in v1 — too many pricing variables
- Phone call transcription: mentioned but dismissed by DJ — not a priority
- Front desk lady handles phones + embroidery — not a technical user
- DJ open to switching from Photoshop to Illustrator workflow if output is better
