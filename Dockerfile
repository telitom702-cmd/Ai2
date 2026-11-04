FROM python:3.10-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ffmpeg + ffprobe (ভিডিও/অডিও প্রসেস) এবং বাংলা/হিন্দি সাবটাইটেলের Noto ফন্ট
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
