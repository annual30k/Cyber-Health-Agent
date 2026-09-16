# Cyber Health Core release

The public runtime is a GitHub Release artifact, not a source checkout.

1. Ensure `pyproject.toml` has the intended release version.
2. Commit the release changes and create the matching annotated tag, such as `v0.3.2`.
3. Push the tag. The Release workflow runs the test suite on Ubuntu, macOS, and Windows, builds the wheel/source distribution, writes `SHA256SUMS`, and creates the GitHub Release.

The release must contain `cyber_health_agent-<version>-py3-none-any.whl` and a SHA-256 digest. The installer and updater use GitHub's `releases/latest` endpoint, require the tag and wheel versions to match, verify the digest, cache the wheel below `~/.cyber-health/releases/`, and never install from a Git branch.

`--project-root` remains an explicit local-development override. It is not the end-user installation path.
