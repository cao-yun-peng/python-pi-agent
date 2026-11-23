FROM swebench/sweb.eval.x86_64.pydicom_1776_pydicom-1139@sha256:a2344b4b6db428913523e14f576fe358d6ae0c349cd77ac82e616188955ddb3b

LABEL org.pi-agent.evaluation.profile="pydicom-1139-pytest7-v1"
COPY requirements.txt check_legacy_setup.py /opt/pi-agent-eval/pydicom-1139/
RUN /opt/miniconda3/envs/testbed/bin/python -m pip install \
        --index-url https://pypi.org/simple --no-cache-dir --no-deps \
        --only-binary=:all: --require-hashes \
        -r /opt/pi-agent-eval/pydicom-1139/requirements.txt \
    && /opt/miniconda3/envs/testbed/bin/python -m pip check \
    && /opt/miniconda3/envs/testbed/bin/python /opt/pi-agent-eval/pydicom-1139/check_legacy_setup.py
