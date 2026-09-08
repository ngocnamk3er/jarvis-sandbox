FROM python:3.11-slim

# This host's IPv6 path is broken (see jarvis-backend/Dockerfile) — same fix.
RUN echo "precedence ::ffff:0:0/96  100" >> /etc/gai.conf

# System deps: build toolchain for a few wheels, fonts for matplotlib/pdf
# output, common CLI tools the agent may reach for, and pandoc for
# markdown -> docx/pptx/html conversion (no texlive — PDF stays on
# reportlab/fpdf2, texlive is ~1GB).
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

RUN pip install --no-cache-dir numpy pandas scipy statsmodels pyarrow
RUN pip install --no-cache-dir matplotlib seaborn plotly kaleido
RUN pip install --no-cache-dir scikit-learn xgboost lightgbm nltk
RUN pip install --no-cache-dir requests httpx beautifulsoup4 lxml
RUN pip install --no-cache-dir openpyxl xlrd xlsxwriter python-docx python-pptx reportlab fpdf2 pillow svgwrite
RUN pip install --no-cache-dir jinja2 markdown pyyaml toml python-dateutil pytz regex tqdm rich tabulate
RUN pip install --no-cache-dir yfinance
# uv — fast package manager, available for the agent to install extras ad hoc
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Runs as root: the service sets up a private mount namespace + drops each
# agent command to a per-thread uid (see runner.py). util-linux (unshare /
# setpriv / mount) is in the base image. /data is the emptyDir mount.
RUN mkdir -p /data /workspace \
    # strip setuid/setgid from the shells's attack surface — the sandbox
    # never needs su/mount/passwd/etc. as another user
    && chmod -s /usr/bin/su /usr/bin/mount /usr/bin/umount /usr/bin/passwd \
                /usr/bin/newgrp /usr/bin/gpasswd /usr/bin/chsh /usr/bin/chfn 2>/dev/null || true
ENV DATA_ROOT=/data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
