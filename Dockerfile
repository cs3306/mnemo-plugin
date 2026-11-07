# syntax=docker/dockerfile:1
# Same OS release the tests ran on (Ubuntu 24.04: pandoc 3.1, TeX Live 2023, Python 3.12).
#   docker build -t mnemo-plugin .                      # full: + XeLaTeX and Chinese fonts (PDF, LaTeX)   ~1.5 GB
#   docker build --build-arg WITH_TEX=0 -t mnemo-plugin:slim .   # Word / PowerPoint / Excel / images only
FROM ubuntu:24.04
ARG WITH_TEX=1
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv pandoc fontconfig ca-certificates \
 && if [ "$WITH_TEX" = "1" ]; then \
      apt-get install -y --no-install-recommends texlive-xetex texlive-latex-recommended texlive-latex-extra \
        texlive-fonts-recommended texlive-lang-chinese lmodern fonts-noto-cjk; \
    fi \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir . && rm -rf /src

RUN mkdir /data && chown 65532:65532 /data      # schedules.json lives here; mount a volume over it
ENV DATA_DIR=/data
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PLUGIN_ADDR=0.0.0.0:8090
USER 65532:65532
WORKDIR /tmp
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4)" || exit 1
CMD ["python", "-m", "mnemo_plugin"]
