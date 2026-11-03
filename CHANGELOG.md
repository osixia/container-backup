# Changelog

Changes to the backup toolbox and its build configuration are recorded here.
The toolbox uses its own version; it does not follow age's release numbers.

## [1.0.1] - 2026-11-03

### Fixed

- Use an explicit in-cluster kubeconfig and the mounted service-account token
  for application orchestration instead of relying on kubectl auto-detection.
- Add an offline SQLite capture profile that briefly pauses a Deployment,
  copies its database and journals privately, resumes it, then runs SQLite backup.
- Share Deployment recovery between Nextcloud and offline SQLite captures.

## [1.0.0] - 2026-11-02

### Added

- Shared backup engine and five capture profiles included in the image.
- Generator for self-contained, Git-tracked Kustomize trees, with project
  overlays, edit protection, scoped recovery RBAC and synthetic checks.

- Backup toolbox based on `osixia/container-age` and Alpine 3.24.
- age 1.3.2, GNU tar and file metadata tools, gzip, SQLite, PostgreSQL 18 clients,
  kubectl 1.36, jq and Python 3.
- Non-root default user and functional tests with synthetic data.
- Native amd64 and arm64 CI builds using `osixia/dagger-build`.
- Release pipeline with image signatures and SPDX SBOM attestations.
- Documented archive, database capture and restore examples.
