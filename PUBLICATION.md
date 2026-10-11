# Public release

The public repository is `https://github.com/Sreeram-R-6/drone-pi-gcs`.
Publish a reviewed source snapshot there; do not send passwords/tokens in chat.
GitHub CLI is not required when using an authenticated GitHub integration.

Prepare and inspect a fresh snapshot:

```bash
python3 scripts/prepare_public_release.py
cd public-release
git init -b main
git add .
git diff --cached --stat
git diff --cached --check
git status --short
# Review contents, then use your own configured Git identity and credentials:
git commit -m "Initial portable Pi GCS and OLED release"
git remote add origin https://github.com/Sreeram-R-6/drone-pi-gcs.git
git push -u origin main
```

This is a fresh source snapshot, not a push of the old `Drone-Sims` history.
Local telemetry can contain GPS coordinates and aircraft identifiers; do not
upload logs, FC parameter dumps, live commissioning profiles or local configs.
Original files are preserved on this Pi; exclusion is not deletion. Large
third-party simulator meshes/textures stay in the original `Drone-Sims` repo;
source/model descriptors and application/test images are retained here.

Models, images and simulation assets may have third-party ownership. No blanket
license has been invented for those assets or for this repository. Confirm your
rights and choose a license before advertising unrestricted reuse. Dependency
licenses remain their own. The exporter retains source and existing assets but
does not validate their provenance. Public availability alone is not a license.
