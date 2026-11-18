FROM python:3.12-slim

# A non-root user (R8.7). The phase 5 spike confirmed that the sidecar's shared
# daemon socket answers status and whois for a non-root caller. Nothing is
# installed: the image stays the standard library only (R8.2).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin grange

WORKDIR /app
COPY content/ ./

# Git metadata captured by the Makefile at build time (see entrypoint.py).
# Declared after COPY so changing the commit does not invalidate the code layer.
ARG GIT_COMMIT=unknown
ARG GIT_BRANCH=unknown
ARG GIT_REPO=unknown
ARG GIT_DIRTY=0
ARG GIT_PUSHED_BASE=""
ENV GIT_COMMIT=$GIT_COMMIT GIT_BRANCH=$GIT_BRANCH GIT_REPO=$GIT_REPO GIT_DIRTY=$GIT_DIRTY GIT_PUSHED_BASE=$GIT_PUSHED_BASE
LABEL org.opencontainers.image.revision=$GIT_COMMIT \
      org.opencontainers.image.source=https://github.com/$GIT_REPO

ENV PORT=8080
EXPOSE 8080
USER grange
ENTRYPOINT ["python3", "/app/entrypoint.py"]
CMD ["python3", "server.py"]
