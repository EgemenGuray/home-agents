# noVNC for the web screen viewer (/screen, S1.6.1), fetched at a pinned commit (v1.6.0) so the build
# can't pick up a changed upstream. Only its ES modules are copied into the image.
FROM python:3.12-slim AS novnc
ARG NOVNC_COMMIT=a8dfd6a3ea3c74244f5ebdaa5a7f1023007a7820
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && \
    git init -q /novnc && cd /novnc && \
    git fetch -q --depth 1 https://github.com/novnc/noVNC.git "$NOVNC_COMMIT" && \
    git checkout -q FETCH_HEAD && test "$(git rev-parse HEAD)" = "$NOVNC_COMMIT"

FROM python:3.12-slim
RUN useradd -m -u 10001 agent
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[service]'
COPY --from=novnc /novnc/core /opt/novnc/core
COPY --from=novnc /novnc/vendor /opt/novnc/vendor
COPY --from=novnc /novnc/LICENSE.txt /opt/novnc/LICENSE.txt
USER agent
ENV HOME_AGENTS_DATA=/data NOVNC_DIR=/opt/novnc
EXPOSE 8700
CMD ["uvicorn", "home_agents.api:build_app", "--factory", "--host", "0.0.0.0", "--port", "8700"]
