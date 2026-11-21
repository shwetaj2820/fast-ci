FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY coverage_map.json.gz /app/coverage_map.json.gz
COPY selector.py /app/selector.py
RUN chmod -R 755 /app
USER nobody
CMD ["python", "/app/selector.py"]
