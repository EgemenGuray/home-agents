FROM python:3.12-slim
RUN useradd -m -u 10001 agent
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[service]'
USER agent
ENV HOME_AGENTS_DATA=/data
EXPOSE 8700
CMD ["uvicorn", "home_agents.api:build_app", "--factory", "--host", "0.0.0.0", "--port", "8700"]
