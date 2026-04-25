# CLAUDE.md — DJ's Art Engine
**Project:** DJ's Custom Clothing Automation Pipeline  
**Builder:** Optimus Business Solutions  
**Client:** DJ's Custom Clothing, Windham NH  
**Stack:** Python + FastAPI + Vectorizer.ai + Pillow/OpenCV + Single-page HTML frontend  
**Deployed to:** Render (or Railway)

---

## What This Project Is

A lightweight internal web tool for a screen printing shop. DJ uploads a customer's logo (JPEG or PNG), the system vectorizes it and separates it into individual color layers, and returns a ZIP of print-ready PDF transparency films — one per color.

This replaces two outsourced manual steps that currently cost DJ $30/job and up to 24 hours of turnaround time.

**This is not a SaaS product (v1). It is a single-user internal tool. No auth, no database, no multi-tenancy.**

---

## The Pipeline (Read This First)

Every request follows exactly this path:

```
JPEG/PNG upload
    ↓
Vectorizer.ai API → clean SVG
    ↓
Color extraction (unique fill colors from SVG paths)
    ↓
Per-color film generation (black on white, 300 DPI PDF)
    ↓
ZIP assembly → download
```

Do not deviate from this pipeline order. Do not add steps. Do not add a database. Do not add user accounts. Keep it simple.

---

## Non-Negotiables

- **No hardcoded secrets.** All API keys and config in `.env`. Always.
- **No permanent file storage.** Process files in `/tmp/{uuid}/`, delete after response sent.
- **PDF output only.** Not EPS, not PNG, not TIFF. PDF. DJ prints directly from the Windows print dialog.
- **300 DPI minimum** on all output files. Never lower.
- **Delta-E color matching** for color identification — not RGB euclidean distance. Use colormath library or equivalent.
- **Black on white = correct.** Where ink prints → black. Background → white. Do not invert. Do not use standard grayscale conversion.
- **White background detection.** Pure white and near-white (delta-E < 10 from #FFFFFF) are excluded from separation list unless they appear as explicit fill paths on a non-white background (white ink on dark shirts).

---

## Folder Structure

```
djs-art-engine/
├── main.py                  # FastAPI app + route definitions
├── pipeline/
│   ├── __init__.py
│   ├── vectorize.py         # Vectorizer.ai API integration
│   ├── separate.py          # SVG parsing + color identification
│   ├── export.py            # Per-color PDF generation
│   └── package.py           # ZIP assembly + cleanup
├── static/
│   └── index.html           # Single page UI — no framework
├── tmp/                     # Runtime only — gitignored
├── requirements.txt         # All dependencies pinned
├── .env                     # Real secrets — gitignored
├── .env.example             # Keys with blank values — committed
├── .gitignore
├── CLAUDE.md                # This file
├── progress.md              # Build progress tracker
└── README.md                # Setup + deployment instructions
```

Do not create files outside this structure without a clear reason.

---

## Environment Variables

```env
VECTORIZER_API_ID=
VECTORIZER_API_TOKEN=
MAX_FILE_SIZE_MB=20
OUTPUT_DPI=300
TEMP_DIR=/tmp/djs-art-engine
```

Read from `.env` using `python-dotenv`. Never access `os.environ` directly — always use the config loader.

---

## Key Dependencies

```
fastapi
uvicorn
python-dotenv
httpx              # async HTTP for Vectorizer.ai calls
pillow             # image processing
opencv-python      # color analysis
svgpathtools       # SVG path parsing
colormath          # delta-E color distance calculations
reportlab          # PDF generation
python-multipart   # file upload handling
```

Pin all versions in `requirements.txt`. Do not use unpinned dependencies.

---

## Error Handling Rules

Every pipeline step must have explicit error handling. No silent failures.

| Failure Point | Behavior |
|---|---|
| Bad file type | 400 response: "Please upload a JPEG or PNG" |
| File too large | 400 response: "File must be under 20MB" |
| Vectorizer.ai API error | 502 response + log full error + return Photopea fallback URL |
| 0 colors detected | 422 response: "No colors detected — try a higher resolution image" |
| 1 color detected | 200 response with warning flag in JSON: "only_one_color": true |
| >10 colors detected | 200 response with warning flag: "complex_design": true |
| Any unhandled exception | 500 response: log full traceback, return generic error message |

Photopea fallback URL format:
```
https://www.photopea.com/
```
Return this URL in the error response so the frontend can display it as a manual fallback link.

---

## Frontend Rules

- Single HTML file at `static/index.html`
- No framework. No npm. No build step. Vanilla HTML + CSS + JavaScript only.
- Served by FastAPI as a static file
- Must work in Chrome on Windows — DJ's operating environment
- Drag and drop upload OR click to browse — both must work
- Progress states: Uploading → Vectorizing → Separating Colors → Ready
- Color preview grid: show a small thumbnail of each separated layer before download
- Download button triggers ZIP download directly — no redirect, no new tab
- If API returns a warning flag — show a yellow banner above the download button
- If API returns an error — show red banner with Photopea fallback link
- Mobile responsive is nice to have, not required for v1

---

## Deployment Target

**Render** (render.com) — free tier acceptable for v1, upgrade to $7/mo instance if cold starts are a problem.

`render.yaml` must be included in the repo for one-click deploy.

Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

---

## What NOT to Build

Do not build any of the following unless explicitly instructed:

- ❌ User authentication or login
- ❌ Database or persistent storage
- ❌ Job history or order logging
- ❌ Email delivery of output files
- ❌ Customer-facing upload portal
- ❌ Embroidery digitizing (DST/PES/EMB) — different problem
- ❌ Invoice or quote generation
- ❌ Multi-tenant or SaaS features
- ❌ Admin dashboard

These are v2 features. Keep v1 focused.

---

## Definition of Done

The build is complete when:

- [ ] JPEG upload → ZIP download works end to end
- [ ] Output PDFs are 300 DPI grayscale, black on white
- [ ] Color count matches actual colors in test logo
- [ ] All error states return correct responses
- [ ] `.env.example` committed with all keys blank
- [ ] `README.md` contains local setup instructions and Render deploy steps
- [ ] `progress.md` updated to COMPLETE
- [ ] DJ can operate the tool without any guidance from Anthony

---

## Contact

**Anthony** — Optimus Business Solutions  
Builder and point of contact for all technical decisions.  
If something in this spec is ambiguous — make the conservative choice and leave a `# TODO: confirm with Anthony` comment.
