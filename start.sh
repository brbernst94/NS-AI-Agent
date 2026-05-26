#!/bin/bash
# SERVICE_ROLE controls what this container runs:
#   api        → FastAPI backend only   (set on the 'web' Railway service)
#   streamlit  → Streamlit frontend only (set on the 'streamlit' Railway service)
#   (unset)    → both, for local development

if [ "$SERVICE_ROLE" = "api" ]; then
    echo "Starting FastAPI backend..."
    exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}"

elif [ "$SERVICE_ROLE" = "streamlit" ]; then
    echo "Starting Streamlit web UI..."
    exec streamlit run web/app.py \
        --server.port="${PORT:-8501}" \
        --server.address=0.0.0.0 \
        --server.headless=true \
        --logger.level=info

else
    # Local dev: run both
    echo "Starting FastAPI backend..."
    uvicorn api.main:app --host 0.0.0.0 --port 8000 &
    echo "Starting Streamlit web UI..."
    exec streamlit run web/app.py \
        --server.port=8501 \
        --server.address=0.0.0.0 \
        --server.headless=true \
        --logger.level=info
fi
