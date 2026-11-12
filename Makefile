.PHONY: install lint test test-pg run sample demo dashboard migrate sql secrets

install:      ## install runtime + dev dependencies
	pip install -r requirements-dev.txt
lint:
	ruff check src tests scripts streamlit_app.py
test:
	pytest --cov=trendpulse --cov-report=term-missing
test-pg:      ## needs TRENDPULSE_TEST_DATABASE_URL
	pytest -m integration
run:          ## one real pipeline run (needs YOUTUBE_API_KEY)
	trendpulse run
sample:       ## one run against the offline simulator
	trendpulse run --sample
demo:         ## 48h of SAMPLE history on a simulated clock, then the dashboard
	trendpulse simulate --cycles 96 && streamlit run streamlit_app.py
dashboard:
	streamlit run streamlit_app.py
migrate:
	trendpulse migrate
sql:          ## regenerate migrations/001_initial_schema.sql
	python scripts/export_sql_migrations.py
secrets:
	python scripts/check_secrets.py
