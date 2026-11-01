# Changelog

Changes to the backup toolbox and its build configuration are recorded here.
The toolbox uses its own version; it does not follow age's release numbers.

## [Unreleased]

### Added

- Backup toolbox based on `osixia/container-age` and Alpine 3.24.
- age 1.3.2, GNU tar and file metadata tools, gzip, SQLite, PostgreSQL 18 clients,
  kubectl 1.36, jq and Python 3.
- Non-root default user and functional tests with synthetic data.
- Native amd64 and arm64 CI builds using `osixia/dagger-build`.
- Release pipeline with image signatures and SPDX SBOM attestations.
- Documented archive, database capture and restore examples.
