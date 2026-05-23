FROM python:3.11-slim

# Install FFmpeg + dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Download Playfair Display font at build time
RUN mkdir -p /fonts && \
    wget -q "https://github.com/google/fonts/raw/main/ofl/playfairdisplay/PlayfairDisplay-Bold.ttf" \
    -O /fonts/PlayfairDisplay-Bold.ttf && \
    wget -q "https://github.com/google/fonts/raw/main/ofl/playfairdisplay/PlayfairDisplay-Regular.ttf" \
    -O /fonts/PlayfairDisplay-Regular.ttf

# Install Python dependencies
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Copy handler
COPY handler.py /handler.py

CMD ["python", "-u", "/handler.py"]
