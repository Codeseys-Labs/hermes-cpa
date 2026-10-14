# Public manifest digests observed from registry-1.docker.io.
# Build from the repository root. The checked-in console must already be built.
# The public host lock owns its dependencies; only the plugin's missing exact
# bcrypt dependency is added. The named cpa-runtime context is the verified build output.
FROM --platform=linux/amd64 astral/uv@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv
FROM --platform=linux/amd64 node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS node
FROM --platform=linux/amd64 python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS build
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/q
COPY scripts/qualification.py scripts/cpa_runtime_common.py /opt/q/scripts/
COPY hermes-cpa/src/hermes_cpa/runtime_manifest.py /opt/q/hermes-cpa/src/hermes_cpa/runtime_manifest.py
COPY host-integration /opt/q/host-integration
ADD https://github.com/NousResearch/hermes-agent.git#0ed432d1871a9a9b169f5a778f6f9444fc13e0c1 /opt/q/host
RUN python /opt/q/scripts/qualification.py build-host --host /opt/q/host
ENV UV_PROJECT_ENVIRONMENT=/opt/q/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /opt/q/host
RUN uv sync --frozen --no-dev --python /usr/local/bin/python \
    && uv pip install --python /opt/q/venv/bin/python --no-deps bcrypt==5.0.0 \
    && uv pip check --python /opt/q/venv/bin/python
WORKDIR /opt/q
COPY --from=cpa-runtime /CLIProxyAPI_8.0.21_hermes-owner-v1_linux_amd64.tar.gz /opt/q/cache/CLIProxyAPI_8.0.21_hermes-owner-v1_linux_amd64.tar.gz
COPY hermes-cpa /opt/q/hermes-cpa
RUN python -c "import sys; sys.path.insert(0,'/opt/q/scripts'); import qualification as q; a=q.runtime_artifact(q.ROOT/'hermes-cpa'); b=(q.ROOT/'cache'/a.name).read_bytes(); assert len(b)==a.archive_size and q.digest(b)==a.archive_sha256, 'runtime archive does not match package'"
COPY qualification/test_qualification.py /opt/q/qualification/test_qualification.py
COPY qualification/provider_smoke.py qualification/console_client.py /opt/q/qualification/

FROM --platform=linux/amd64 python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 qualification \
    && useradd --uid 10001 --gid 10001 --home-dir /work --no-create-home qualification \
    && mkdir /work && chown 10001:10001 /work && chmod 0700 /work
COPY --from=build /usr/local/bin/node /usr/local/bin/node
COPY --from=build /usr/local/bin/uv /usr/local/bin/uv
COPY --from=build --chown=10001:10001 /opt/q /opt/q
ENV HOME=/work PATH=/opt/q/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 UV_OFFLINE=1
LABEL org.opencontainers.image.title="Hermes CPA isolated backend qualification" \
      dev.hermes.host-base="0ed432d1871a9a9b169f5a778f6f9444fc13e0c1" \
      dev.hermes.qualification="candidate-only; not native WSL or Windows certification"
USER 10001:10001
WORKDIR /work
CMD ["python", "/opt/q/scripts/qualification.py", "serve", "--root", "/work/q", "--prepare-if-missing"]
