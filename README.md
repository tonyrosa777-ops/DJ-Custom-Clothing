# DJ's Art Engine
**Client:** DJ's Custom Clothing, Windham NH  
**Built by:** Optimus Business Solutions  

Uploads a customer logo (JPEG/PNG) → vectorizes it → separates by color → returns print-ready transparency film PDFs for screen printing.

---

## Local Setup

### Requirements
- Python 3.11+
- A Vectorizer.ai account (get API credentials at vectorizer.ai)

### Install

```bash
git clone https://github.com/YOUR_REPO/djs-art-engine
cd djs-art-engine
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
```

Open `.env` and fill in your Vectorizer.ai API credentials:

```env
VECTORIZER_API_ID=your_id_here
VECTORIZER_API_TOKEN=your_token_here
```

### Run

```bash
uvicorn main:app --reload
```

Open `http://localhost:8000` in Chrome.

---

## Deploy to Render

1. Push repo to GitHub
2. Go to render.com → New Web Service → Connect repo
3. Render auto-detects `render.yaml`
4. Add environment variables in Render dashboard (copy from `.env`)
5. Deploy — live URL returned in ~2 minutes

---

## How It Works

1. User uploads JPEG or PNG logo
2. File sent to Vectorizer.ai API → returns clean SVG
3. SVG parsed for unique fill colors (delta-E color matching)
4. One grayscale PDF generated per color (black on white, 300 DPI)
5. All PDFs zipped and returned as download
6. User prints each PDF on Epson Artisan 1430 → transparency film → burn to screen

---

## Printer Setup (DJ's Shop)

- **Printer:** Epson Artisan 1430
- **Media:** Transparency films
- **Print settings:** Grayscale, highest quality, actual size (no scaling)
- **Each file:** One color per print job

---

## Support

Contact Anthony at Optimus Business Solutions for any issues.
