FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=5050 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

WORKDIR /app

# Install system audio and build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    libportaudio2 \
    portaudio19-dev \
    alsa-utils \
    curl \
    gcc \
    g++ \
    make \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

# Copy source code
COPY src/ ./src/

# Create persistent directories
RUN mkdir -p /root/.local/share/jarvis /root/.config/jarvis

EXPOSE 5050

CMD ["python", "src/server_app/web_server.py"]
