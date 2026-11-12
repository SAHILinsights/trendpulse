# TrendPulse — one image for both the pipeline and the dashboard.
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN useradd --create-home --uid 10001 trendpulse
COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
RUN pip install -r requirements.txt
COPY config ./config
COPY migrations ./migrations
COPY .streamlit/config.toml ./.streamlit/config.toml
COPY streamlit_app.py ./
RUN mkdir -p /app/data && chown -R trendpulse /app/data
USER trendpulse
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"
# Default: dashboard. Pipeline: docker run ... trendpulse run
CMD ["streamlit", "run", "streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
