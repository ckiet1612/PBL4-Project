ARG BASE_IMAGE_REF
FROM ${BASE_IMAGE_REF}

ARG TORCH_VERSION=2.12.1
ARG REQUIREMENTS_FILE=deploy/pytorch-cpu/requirements.linux-amd64.txt

ENV PYTHONPATH=/opt/nexa \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp/nexa-home \
    XDG_CACHE_HOME=/tmp/nexa-cache \
    TORCH_HOME=/tmp/nexa-cache/torch

COPY ${REQUIREMENTS_FILE} /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes --only-binary=:all: \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && python -c "import torch, sys; v = torch.__version__.split('+')[0]; sys.exit(0 if v == '${TORCH_VERSION}' and not torch.cuda.is_available() else 1)" \
    && groupadd --gid 1000 nexa-runner \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin nexa-runner \
    && groupadd --gid 1001 nexa-workload \
    && useradd --uid 1001 --gid 1001 --create-home --shell /usr/sbin/nologin nexa-workload \
    && mkdir -p /opt/nexa /run/nexa /var/lib/nexa /input /output \
    && chown -R nexa-runner:nexa-runner /run/nexa /var/lib/nexa /output \
    && chmod 0555 /input

# Adapter build args come after the dependency layer so both images share it.
# One Dockerfile, one image per adapter (B16-R03): ADAPTER_ID selects the labels the
# worker probe matches against the static adapter registry.
ARG IMAGE_ARCH
ARG ADAPTER_ID
ARG CHECKPOINT_FORMAT
RUN case "${ADAPTER_ID}:${CHECKPOINT_FORMAT}" in \
        pytorch.cifar10:pytorch-cifar10-state-v1|batch.inference:batch-inference-state-v1) ;; \
        *) echo "unsupported ADAPTER_ID/CHECKPOINT_FORMAT pair" >&2; exit 2 ;; \
    esac

COPY src/nexa /opt/nexa/nexa
COPY deploy/pytorch-cpu/runner-config.json /etc/nexa/runner-config.json

LABEL org.opencontainers.image.title="nexa-${ADAPTER_ID}" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.architecture="${IMAGE_ARCH}" \
      org.opencontainers.image.source="nexa/pbl4" \
      io.nexa.adapter.id="${ADAPTER_ID}" \
      io.nexa.adapter.version="1.0.0" \
      io.nexa.framework="PYTORCH" \
      io.nexa.framework.version="${TORCH_VERSION}" \
      io.nexa.runner="trusted-runner-v1" \
      io.nexa.runner.checkpoint="${CHECKPOINT_FORMAT}"

USER 1000:1000
ENTRYPOINT ["python", "-m", "nexa.workloads.trusted_runner"]
