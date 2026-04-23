.PHONY: help setup setup-db scan scan-llm top history serve test clean

help:
	@echo "Common targets:"
	@echo "  make setup       Install dependencies into the active venv"
	@echo "  make setup-db    Initialize the database and apply migrations"
	@echo "  make scan        Run a scan with rule-based extraction (free)"
	@echo "  make scan-llm    Run a scan with LLM extraction (uses Anthropic API)"
	@echo "  make top         Print top-ranked makers"
	@echo "  make history     Show recent scan run history"
	@echo "  make serve       Start the FastAPI + HTMX UI on http://localhost:8000"
	@echo "  make test        Run the test suite"
	@echo "  make clean       Remove the local DB and cache (keeps migrations)"
	@echo ""
	@echo "  For scheduled daily scans, run:  python -m watch_app.cli schedule"

setup:
	pip install -r requirements.txt

setup-db:
	mkdir -p data
	alembic upgrade head

scan:
	python -m watch_app.cli scan

scan-llm:
	python -m watch_app.cli scan --use-llm

top:
	python -m watch_app.cli top

history:
	python -m watch_app.cli history

serve:
	uvicorn watch_app.api.main:app --reload --host 127.0.0.1 --port 8000

test:
	pytest -q

clean:
	rm -f data/watch_app.db
	rm -rf data/cache
