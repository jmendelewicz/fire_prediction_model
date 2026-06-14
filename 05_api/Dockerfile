# AlertaFuego Predictions API — container for Hugging Face Spaces (Docker SDK)
# HF Spaces expose port 7860 by default.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
# Optional: bake a sample so the Space serves something before Databricks auth
COPY predictions_ui.sample.json ./predictions_ui.cache.json

ENV AUTO_REFRESH_ON_START=0
EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
