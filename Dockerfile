# GeoLoupe in a container. By default it serves the gallery of open public data.
#
#   docker build -t geoloupe .
#   docker run --rm -p 8000:8000 geoloupe                        # the gallery → http://localhost:8000
#   docker run --rm -p 8000:8000 -v "$PWD/data:/data" geoloupe \
#       geoloupe serve /data/config.yaml --host 0.0.0.0           # your own data
#
# Released images: ghcr.io/salmanmohebi/geoloupe:<version> (.github/workflows/image.yml). In a
# RenkuLab session the server finds its path prefix in RENKU_BASE_URL_PATH (docs/renkulab.md).
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
# caches in /tmp: writable for any user id the container runs as (RenkuLab sets its own)
ENV XDG_CACHE_HOME=/tmp/.cache
# The GDAL inside the rasterio and pyogrio wheels needs libexpat, which slim images do not have.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 geoloupe
COPY . /tmp/src
RUN pip install "/tmp/src[all]" && rm -rf /tmp/src
USER geoloupe
WORKDIR /home/geoloupe
EXPOSE 8000
CMD ["geoloupe", "gallery", "--host", "0.0.0.0", "--port", "8000"]
