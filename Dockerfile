FROM python:3.11-slim

# Install FFmpeg + dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    fonts-dejavu-core \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Use Liberation Serif (comes with fonts-liberation, looks like Times/Playfair)
# Copy to /fonts so handler.py finds it at the expected path
RUN mkdir -p /fonts && \
    cp /usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf /fonts/PlayfairDisplay-Bold.ttf && \
    cp /usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf /fonts/PlayfairDisplay-Regular.ttf

# Install Python dependencies
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Copy handler
COPY handler.py /handler.py

CMD ["python", "-u", "/handler.py"]
