#!/bin/bash

# Start the FastAPI backend in the background (internal port 8000)
echo "Starting FastAPI backend..."
python -m api.main &
API_PID=$!

# Wait for API to be ready
sleep 2

# Start Streamlit on the exposed port
echo "Starting Streamlit web UI..."
streamlit run web/app.py \
    --server.port=8080 \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --logger.level=info

# Keep the script running
wait $API_PID
