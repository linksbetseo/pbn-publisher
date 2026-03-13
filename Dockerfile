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

# ARG to bust Docker layer cache on every deploy
ARG CACHEBUST=1
COPY frontend/ ./frontend/
RUN mkdir -p backend/frontend_dist && \
    cd frontend && npm run build && cp -r dist/* ../backend/frontend_dist/

# Install Python deps
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/

# Re-copy frontend build (COPY backend/ above overwrites frontend_dist)
RUN cd frontend && cp -r dist/* ../backend/frontend_dist/

EXPOSE 8080

CMD cd backend && python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
