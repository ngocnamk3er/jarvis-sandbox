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

# The image runs unprivileged (uid 1000, primary group 0 — the "arbitrary
# uid" pattern). Both roles run as this user:
#   * orchestrator: only needs the k8s API + outbound HTTP
#   * agent: the pod itself is the sandbox, so the command runs as this same
#     unprivileged uid with every capability dropped
# site-packages + /usr/local/bin are made group-writable (+ setgid on dirs) so
# the agent's `pip install <pkg>` still works without --user.
RUN useradd --uid 1000 --gid 0 --create-home --home-dir /home/sandbox --shell /bin/bash sandbox \
    && mkdir -p /workspace && chgrp 0 /workspace && chmod g+rwXs /workspace \
    && chgrp -R 0 /usr/local/lib/python3.11/site-packages /usr/local/bin \
    && chmod -R g+rwX /usr/local/lib/python3.11/site-packages /usr/local/bin \
    && find /usr/local/lib/python3.11/site-packages -type d -exec chmod g+s {} + \
    # strip every setuid/setgid bit — nothing here ever needs to act as another
    # user (no su / login / mount / cron / ping-as-root)
    && find / -xdev -type f -perm /6000 -exec chmod -s {} + 2>/dev/null || true

ENV HOME=/home/sandbox \
    PYTHONDONTWRITEBYTECODE=1

USER 1000

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
