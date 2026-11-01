# Build and release guide

This project reuses [osixia/dagger-build](https://github.com/osixia/dagger-build),
following the layout of [container-age](https://github.com/osixia/container-age).
The Dockerfile is also directly buildable with Docker.

## Files

| File | Role |
| --- | --- |
| `../Dockerfile` | Alpine base, packages, tool versions and default user |
| `main.go` | Image metadata and connection to the shared build pipeline |
| `tests/test.go` | Runs the functional check in every built image |
| `tests/testdata/image.sh` | Actual checks, using disposable test data |
| `../.github/workflows/go.yml` | Lint, tests, native builds and release publication |

There is no custom application binary. The Go module only wires this image into
the existing Osixia build pipeline.

## Fast local check with Docker

From the repository root:

```bash
docker build --tag osixia/backup:develop .

docker run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  --mount type=bind,src="$PWD/build/tests/testdata/image.sh",dst=/checks/image.sh,readonly \
  osixia/backup:develop bash /checks/image.sh
```

The checks cover:

- Default non-root user and expected tool families.
- GNU tar, findutils and coreutils; kubectl's client command and jq.
- Archive creation, age encryption, checksums, decryption and extraction.
- Permissions, numeric ownership, symbolic links, hard links, FIFOs, ACLs and xattrs.
- Rejection of a wrong age identity and a truncated ciphertext.
- Online SQLite backup while the source database is open in WAL mode.
- Python's standard SQLite module and PostgreSQL client binaries.

The test does not contact Kubernetes or a PostgreSQL server. Tests create their
own temporary keys, suppress key output and remove their working directory on exit.

## Check the shared pipeline

From this `build/` directory, on an amd64 host:

```bash
go mod vendor
go run main.go job test ../Dockerfile --arches amd64
```

Use `--arches arm64` on a native arm64 host. CI tests both architectures on native
runners. The custom `job.Test` runs the same shell check as the Docker command.

For all pipeline options:

```bash
go run main.go --help
```

## Change tool versions

The defaults are `AGE_VERSION=1.3.2`, `POSTGRESQL_MAJOR=18` and
`KUBECTL_VERSION=1.36`. To build for another supported client family:

```bash
docker build \
  --build-arg POSTGRESQL_MAJOR=17 \
  --tag osixia/backup:local-pg17 .
```

The selected packages must exist in the pinned Alpine release. Re-run the
functional checks against that tag. When changing the Alpine base, update both
the Dockerfile and `BaseImage` in `main.go`.

Pin the published image by digest when repeatable deployments matter: package
updates can change the result of a later build even with a pinned base image.

## Publish a release

The inherited GitHub workflow tests branch and pull request builds. Only a pushed
release tag triggers registry publication. Before tagging:

1. Confirm the native amd64 and arm64 builds pass.
2. Configure the GitHub Actions secrets `REGISTRY_USERNAME` and `REGISTRY_PASSWORD`
   for Docker Hub. The operator supplies their values; the repository contains none.
3. Update `../CHANGELOG.md` with the version to publish.
4. Push that version tag, for example `1.0.0`.

The release build exports tested OCI archives. The publish job combines those
archives into the multi-architecture image, then signs it with keyless cosign
and attaches an SPDX SBOM. Signing uses GitHub OIDC; no signing key is stored here.

Verify a published release, replacing `VERSION` with its tag:

```bash
cosign verify osixia/backup:VERSION \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/osixia/container-backup/\.github/workflows/'

cosign verify-attestation --type spdxjson osixia/backup:VERSION \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/osixia/container-backup/\.github/workflows/'
```
