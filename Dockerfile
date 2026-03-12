FROM python:3.11-slim

# Install Node.js
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Build frontend
COPY frontend/package*.json ./frontend/
RUN cd frontend && npm ci

# cache bust frontend: 2026-03-12-v3
COPY frontend/ ./frontend/
RUN mkdir -p backend/frontend_dist && \
    cd frontend && npm run build && cp -r dist/* ../backend/frontend_dist/

# Install Python deps
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend (cache bust: 2026-03-12z)
COPY backend/ ./backend/

EXPOSE 8080

CMD cd backend && python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
