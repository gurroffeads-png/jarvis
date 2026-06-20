# Imagem do Orion Cloud (backend multi-inquilino). Stdlib + pg8000 (driver Postgres puro-python, p/ Neon).
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pg8000 pywebpush
# so o necessario pra nuvem (nada de audio/desktop)
COPY orion_cloud.py orion_app.html orion_site.html orion_logo.png orion_icon.png orion.ico ./
ENV PORT=8766
EXPOSE 8766
CMD ["python", "orion_cloud.py"]
