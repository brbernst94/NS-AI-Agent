#!/bin/bash

# Start the FastAPI backend in the background
echo "Starting FastAPI backend..."
python -m api.main &
API_PID=$!

# Wait for API to be ready
sleep 2

# Start Streamlit
echo "Starting Streamlit web UI..."
streamlit run web/app.py \
    --server.port=${PORT:-8501} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --logger.level=info

# Keep the script running
wait $API_PID
