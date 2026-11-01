# Keep this reference in sync with BaseImage in build/main.go.
ARG BASE_IMAGE="alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a"
FROM ${BASE_IMAGE}

ARG IMAGE="osixia/backup:develop"
ENV CONTAINER_IMAGE=${IMAGE}

# Pin tool families; apk supplies their security and packaging updates.
ARG AGE_VERSION=1.3.2
ARG POSTGRESQL_MAJOR=18
ARG KUBECTL_VERSION=1.36

ENV AGE_VERSION=${AGE_VERSION} \
    POSTGRESQL_MAJOR=${POSTGRESQL_MAJOR} \
    KUBECTL_VERSION=${KUBECTL_VERSION}

# Shell scripts, archives and file metadata.
RUN apk add --no-cache \
        bash \
        coreutils \
        findutils \
        tar \
        gzip \
        acl \
        attr \
        ca-certificates \
        "age~=${AGE_VERSION}" \
        sqlite \
        "postgresql${POSTGRESQL_MAJOR}-client" \
        "kubectl~=${KUBECTL_VERSION}" \
        jq \
        python3

# Jobs can select another UID when their mounted volumes require it.
USER 65534:65534

# No entrypoint: callers choose the command or script to run.
CMD ["bash", "--help"]
