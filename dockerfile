FROM python:3.12-slim

WORKDIR /PRODUCTION_INFERENCE

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api_gateway.py .
COPY src/production_inference ./production_inference

CMD ["uvicorn", "api_gateway:app", "--host", "0.0.0.0", "--port", "8000"]