FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# WeasyPrint still shells out to Pango (via cffi) for text shaping even though the actual
# page rendering is pure Python now, so libpango/gdk-pixbuf/etc are still required. Installed
# from vendored .deb files (see README "Сборка образа офлайн") instead of apt-get, for the
# same reason pip uses vendored wheels below.
WORKDIR /app

COPY debs ./debs
RUN dpkg -i ./debs/*.deb \
    && rm -rf ./debs /var/lib/dpkg/*-old

COPY requirements.txt .
# Installed fully offline from vendored wheels/ (see README "Сборка образа офлайн") —
# this host's Docker containers can't reliably reach PyPI directly (see README), so build
# reproducibility doesn't depend on that working.
COPY wheels ./wheels
RUN pip install --no-cache-dir --no-index --find-links=./wheels -r requirements.txt \
    && rm -rf ./wheels

COPY bot ./bot
COPY vendor ./vendor

RUN mkdir -p /app/data/reports

CMD ["python", "-m", "bot.main"]
