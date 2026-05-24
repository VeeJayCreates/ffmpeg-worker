FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /fonts && \
    cp /usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf /fonts/PlayfairDisplay-Bold.ttf && \
    cp /usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf /fonts/PlayfairDisplay-Regular.ttf && \
    find /usr/share/fonts -name "NotoColorEmoji*" -exec cp {} /fonts/ \; 2>/dev/null || true

COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

COPY handler.py /handler.py

CMD ["python", "-u", "/handler.py"]
