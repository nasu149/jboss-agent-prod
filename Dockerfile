FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Install dependencies in the image. The workspace itself is mounted by Dev Containers.
COPY pyproject.toml /tmp/jboss-agent/pyproject.toml
COPY src /tmp/jboss-agent/src
RUN cd /tmp/jboss-agent \
    && pip install --upgrade pip \
    && pip install '.[dev]' \
    && rm -rf /tmp/jboss-agent

EXPOSE 8501
CMD ["bash"]
