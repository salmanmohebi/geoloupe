# A public GeoLoupe gallery on Hugging Face Spaces (Docker Space, free CPU hardware).
# Copy this folder into a new Space; it installs GeoLoupe from PyPI.
FROM python:3.12-slim
# The GDAL inside the rasterio and pyogrio wheels needs libexpat, which slim images do not have.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 user
USER user
ENV PATH=/home/user/.local/bin:$PATH PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN pip install --user "geoloupe[all]"
WORKDIR /home/user
EXPOSE 7860
CMD ["geoloupe", "gallery", "--host", "0.0.0.0", "--port", "7860"]
