ARG PW_VERSION=1.49.1
FROM mcr.microsoft.com/playwright/python:v${PW_VERSION}-noble

ARG PW_VERSION
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEXUS_NO_SANDBOX=1

WORKDIR /app
RUN chown pwuser:pwuser /app

COPY requirements.txt .
RUN pip install --break-system-packages -r requirements.txt "playwright==${PW_VERSION}"

COPY --chown=pwuser:pwuser automation_stage/ ./automation_stage/

# run as a normal user
USER pwuser

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

CMD ["python3", "-m", "streamlit", "run", "automation_stage/app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", \
     "--server.headless=true", "--browser.gatherUsageStats=false"]