FROM python:3.11-slim

# This host's IPv6 path is broken (see jarvis-backend/Dockerfile) — same fix.
RUN echo "precedence ::ffff:0:0/96  100" >> /etc/gai.conf

# System deps: build toolchain for a few wheels, fonts for matplotlib/pdf
# output, common CLI tools, and pandoc for markdown -> docx/pptx/html
# conversion (no texlive — PDF stays on reportlab/fpdf2, texlive is ~1GB).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ \
        fonts-dejavu-core fonts-liberation \
        curl wget git jq unzip pandoc \
        libxml2-dev libxslt-dev libjpeg-dev libpng-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Split into small layers so a network blip on one group doesn't force
# re-downloading everything; generous timeout/retries for a flaky connection.
ENV PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

# The full data-analysis + document-generation toolchain, baked in. The agent
# CANNOT install anything at runtime (pip/uv removed below, rootfs read-only,
# no network egress) — this list is the whole environment, so keep it complete.
RUN pip install --no-cache-dir numpy pandas scipy statsmodels pyarrow
RUN pip install --no-cache-dir matplotlib seaborn plotly kaleido
RUN pip install --no-cache-dir scikit-learn xgboost lightgbm nltk
RUN pip install --no-cache-dir requests httpx beautifulsoup4 lxml
RUN pip install --no-cache-dir openpyxl xlrd xlsxwriter python-docx python-pptx reportlab fpdf2 pillow svgwrite
RUN pip install --no-cache-dir jinja2 markdown pyyaml toml python-dateutil pytz regex tqdm rich tabulate
RUN pip install --no-cache-dir yfinance

# Pre-download the common NLTK corpora so nltk is usable offline.
ENV NLTK_DATA=/usr/local/share/nltk_data
RUN python -m nltk.downloader -d "$NLTK_DATA" \
        punkt punkt_tab stopwords wordnet omw-1.4 \
        averaged_perceptron_tagger averaged_perceptron_tagger_eng

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Lock the environment down. Every agent pod runs read-only, non-root, with no
# network egress; the only writable paths are the /workspace and /tmp emptyDirs.
#   * strip pip / uv / ensurepip so `pip install` can't even be attempted
#   * cache dirs (matplotlib, fontconfig, …) point at /tmp so a read-only
#     rootfs is fine and `ls /workspace` stays clean
#   * strip setuid/setgid bits — nothing ever needs to act as another user
RUN set -eux; \
    useradd --uid 1000 --gid 0 --create-home --home-dir /home/sandbox --shell /bin/bash sandbox; \
    python -m pip uninstall -y pip 2>/dev/null || true; \
    rm -rf /usr/local/lib/python3.11/ensurepip; \
    rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.11 \
          /usr/local/bin/uv /usr/local/bin/uvx; \
    mkdir -p /workspace; \
    find / -xdev -type f -perm /6000 -exec chmod -s {} + 2>/dev/null || true

ENV HOME=/workspace \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/tmp/.mplconfig \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config

USER 1000

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
