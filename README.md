# ImageSL

**A fully online tool for IHC stain quantification, with an AI assistant that can
recalculate the analysis for you.**

Upload an immunohistochemistry (IHC) slide. ImageSL separates the true
chromogenic stain from background and counterstain **for any stain color**
(color deconvolution + automatic Macenko stain-vector estimation + Otsu
thresholding — the methods used by QuPath/Fiji), quantifies the positive area,
and lets you re-render the image. A built-in Claude assistant can **change the
analysis on request** — tell it what's wrong and it re-runs the measurement.

There is no desktop app and no download — it's a single web page. All analysis
and the Claude API key run on the server.

> ⚕️ ImageSL assists interpretation. It is **not** a clinical diagnosis; results
> must be confirmed by a qualified pathologist.

## The assistant can recalculate

The chat isn't just Q&A. It has tools that re-run the analysis:

| You say | It does |
| --- | --- |
| "You're counting too much background, be stricter" | raises the positivity threshold and re-measures |
| "The target is the blue stain, not the brown" | switches which separated stain is quantified |
| "Ignore the faint areas" | raises the background/tissue cutoff |
| "Make the staining darker and the background white" | re-renders the preview image |

After a tool call the numbers and images on the page update automatically.

## Repository layout

```
ImageSL/
├── server/                  # FastAPI backend (deployed to Railway) — the whole app
│   ├── app.py               # routes: page, /api/analyze, /api/recalculate, /api/appearance, /api/chat
│   ├── ihc/engine.py        # THE analysis: OD → Macenko → deconvolution → Otsu → variants
│   ├── ai/claude_client.py  # Claude vision + agentic chat with recalculation tools
│   ├── web/                 # plain single-page UI (index.html, styles.css, app.js)
│   └── requirements.txt
├── Dockerfile, railway.json, .env.example
└── docs/                    # ARCHITECTURE.md, SECURITY.md, DEPLOY.md
```

## Run it — see GETTING_STARTED

Step-by-step (local + Railway, and how the chatbot is enabled) is in
**[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)**.

Shortest path:

1. Push this repo to GitHub and point a Railway service at it (Dockerfile build).
2. In Railway **Variables**, set `ANTHROPIC_API_KEY` (required for the chatbot).
3. Open the Railway URL, upload a slide, and talk to the assistant.

## One honest note

"Deep vision reasoning pixel-by-pixel" is delivered as: rigorous per-pixel
digital-pathology math (deconvolution + Macenko + Otsu) does the stain/background
separation, and Claude's vision model reasons *on top* to identify the stain and
drive the recalculation tools. An LLM does not segment a gigapixel slide
per-pixel, and ImageSL doesn't pretend it does — the combination is what makes it
work well. Full design in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
