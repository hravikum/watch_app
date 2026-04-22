# Watch App

A tool for discovering up-and-coming independent watchmakers — the kind of makers
who might become nominees, finalists, or winners of prizes like the Louis Vuitton
Watch Prize for Independent Creatives.

## How it works

The app is a pipeline: **Sources → Extraction → Resolution → Scoring → Surfacing.**

- **Sources** pull mentions of makers from curated places (specialist watch media,
  forums, awards, social). Each source yields `Mention` objects.
- **Extraction** pulls maker names out of article text (rule-based now; LLM-backed
  behind a `--use-llm` flag).
- **Resolution** matches mentions to canonical `Maker` records (fuzzy name matching
  + aliases).
- **Scoring** computes signals per maker and ranks them under a chosen preset
  (`rising_star` or `hidden_gem`).
- **Surfacing** is the FastAPI + HTMX web dashboard and the CLI.

Phase 0 (this commit) ships the entire pipeline end-to-end with a dummy source,
so every layer is wired and testable before we add real scrapers.

## Setup

Requires Python 3.12 (not 3.14 — some dependencies lag).

```bash
# One-time setup
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create .env from the template, fill in your Anthropic API key if you want LLM
cp .env.example .env

# Initialize the DB
make setup-db

# Run a scan with the dummy source
make scan

# See the top-ranked makers in the CLI
make top

# Start the web UI at http://localhost:8000
make serve
```

## CLI

```bash
watch-app setup-db              # Create DB and apply migrations
watch-app scan                  # Run all sources, extract, score
watch-app scan --use-llm        # Same, but use Claude for extraction/enrichment
watch-app top                   # Print top 20 makers
watch-app top --preset hidden_gem
watch-app maker "Simon Brette"  # Show everything we know about one maker
watch-app serve                 # Start the web UI
```

## Project layout

```
watch_app/
├── sources/        # One module per source (RSS, forums, awards, social)
├── extraction/     # Named-entity extraction and maker resolution
├── llm/            # Anthropic client wrapper + content-hash response cache
├── scoring/        # Signal calculators and ranking
├── storage/        # SQLAlchemy models and DB setup
├── pipeline/       # Orchestrator that glues the layers together
├── api/            # FastAPI routes
├── web/            # Jinja templates + static assets (HTMX frontend)
└── cli.py          # Typer CLI entry point
```

## Roadmap

- **Phase 0 (done):** scaffolding, dummy pipeline end-to-end, DB, LLM wiring, minimal UI.
- **Phase 1:** real sources — GPHG awards, Monochrome RSS, Hodinkee, SJX, Fratello, Worldtempus.
- **Phase 2:** community sources (Reddit, WatchUSeek) and the full dashboard with filters.
- **Phase 3:** social sources (Instagram, YouTube via seed lists), novelty/velocity scoring.

## License

TBD.
