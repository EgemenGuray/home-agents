# CLI workspace (S1.4.2): shell, Python for data work, Playwright Chromium for headless sites.
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble
RUN pip install --no-cache-dir pandas openpyxl httpx && useradd -m -u 10001 agent
USER agent
WORKDIR /home/agent/work
CMD ["sleep", "infinity"]
