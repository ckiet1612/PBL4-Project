ARG BASE_IMAGE_REF
FROM ${BASE_IMAGE_REF}

ARG IMAGE_ARCH
LABEL org.opencontainers.image.title="nexa-cpu-iterative" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.architecture="${IMAGE_ARCH}" \
      org.opencontainers.image.source="nexa/pbl4" \
      io.nexa.adapter.id="cpu.iterative" \
      io.nexa.adapter.version="1.0.0" \
      io.nexa.framework="NEXA_CPU" \
      io.nexa.framework.version="1.0.0" \
      io.nexa.runner="trusted-runner-v1"

ENV PYTHONPATH=/opt/nexa \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 1000 nexa-runner \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin nexa-runner \
    && groupadd --gid 1001 nexa-workload \
    && useradd --uid 1001 --gid 1001 --create-home --shell /usr/sbin/nologin nexa-workload \
    && mkdir -p /opt/nexa /run/nexa /var/lib/nexa /input /output \
    && chown -R nexa-runner:nexa-runner /run/nexa /var/lib/nexa /output \
    && chmod 0555 /input

COPY src/nexa /opt/nexa/nexa
COPY deploy/cpu-iterative/runner-config.json /etc/nexa/runner-config.json

USER 1000:1000
ENTRYPOINT ["python", "-m", "nexa.workloads.trusted_runner"]
