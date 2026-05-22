FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory for ChromaDB and SQLite
RUN mkdir -p data

# Expose ports for API and Streamlit
EXPOSE 8000 8501

# Run startup script
CMD ["sh", "start.sh"]
