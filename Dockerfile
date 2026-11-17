FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir /state && chown 1000:1000 /state
COPY core.py bot.py internet_features.py dtx_counties.json ./
USER 1000:1000
CMD ["python", "bot.py"]
