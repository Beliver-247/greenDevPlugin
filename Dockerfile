FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git maven \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY optimizer/ /opt/optimizer/optimizer/
COPY config/ /opt/optimizer/config/

ENV PYTHONPATH=/opt/optimizer

ENTRYPOINT ["python", "-m", "optimizer"]
