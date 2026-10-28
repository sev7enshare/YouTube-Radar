FROM python:3.12-slim
WORKDIR /app
COPY radar /app/radar
COPY channels.txt /app/channels.txt
COPY market_channels.txt /app/market_channels.txt
ENV PYTHONUNBUFFERED=1 RADAR_DB=/data/radar.sqlite3 RADAR_LISTEN_HOST=0.0.0.0
VOLUME /data
EXPOSE 8765
CMD ["python", "-m", "radar", "serve"]

