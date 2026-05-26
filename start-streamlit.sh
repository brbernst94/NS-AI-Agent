#!/bin/bash
# Runs only the Streamlit frontend — used by the Railway 'streamlit' service.
echo "Starting Streamlit web UI..."
exec streamlit run web/app.py \
    --server.port="${PORT:-8501}" \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --logger.level=info
