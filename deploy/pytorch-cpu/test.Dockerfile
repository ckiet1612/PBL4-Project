# Test-only layer for the B16 torch tests (VPS1, --network none). Never tagged as a product
# image: it adds hash-locked pytest/hypothesis and the tests on top of a built product image.
ARG PRODUCT_IMAGE_REF
FROM ${PRODUCT_IMAGE_REF}

USER 0:0
COPY deploy/pytorch-cpu/requirements-test.linux-amd64.txt /tmp/requirements-test.txt
RUN pip install --no-cache-dir --no-deps --require-hashes --only-binary=:all: \
        --index-url https://pypi.org/simple -r /tmp/requirements-test.txt \
    && rm /tmp/requirements-test.txt
COPY tests/__init__.py /opt/nexa-tests/tests/__init__.py
COPY tests/workloads/__init__.py \
     tests/workloads/test_pytorch_workloads_b16.py \
     tests/workloads/test_safetensors_format_b16.py \
     tests/workloads/test_dataset_format_b16.py \
     tests/workloads/test_training_state_b16.py \
     tests/workloads/test_inference_state_b16.py \
     /opt/nexa-tests/tests/workloads/
WORKDIR /opt/nexa-tests
ENV HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis
USER 1000:1000
ENTRYPOINT ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
