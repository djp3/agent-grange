FROM python:3.12-slim
WORKDIR /app
COPY content/*.py ./

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
ENTRYPOINT ["python3", "/app/entrypoint.py"]
CMD ["python3", "server.py"]
