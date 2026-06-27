# Imagem do Orion Cloud (backend multi-inquilino). Stdlib + pg8000 (driver Postgres puro-python, p/ Neon).
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pg8000 pywebpush
# copia tudo da pasta (orion_cloud.py + HTMLs + icones). Robusto: nao quebra se faltar um arquivo da lista.
COPY . ./
ENV PORT=8766
ENV APP_MODE=orion
EXPOSE 8766
CMD ["python", "orion_cloud.py"]
