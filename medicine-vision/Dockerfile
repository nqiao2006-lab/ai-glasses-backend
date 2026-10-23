FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*
COPY . .
RUN python restore_models.py
RUN python -m pip install -r requirements-api.txt && python -m pip check
RUN python verify_bundle.py
RUN python -c "import paddle, paddleocr, fastapi, uvicorn; from pathlib import Path; assert Path('models/paddlex/official_models/PP-OCRv6_medium_det/inference.pdiparams').stat().st_size > 1000000; assert Path('models/paddlex/official_models/PP-OCRv6_medium_rec/inference.pdiparams').stat().st_size > 1000000"
CMD ["python", "deploy_vision.py"]
