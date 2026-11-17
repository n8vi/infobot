FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY goes_api.py maps.py gallery.html ./
USER 1000:1000
EXPOSE 8097
CMD ["python", "goes_api.py", "--root", "/data/EMWIN/Text", "--goes-root", "/data", "--host", "0.0.0.0", "--port", "8097"]
