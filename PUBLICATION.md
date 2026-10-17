# Public release

The public repository is `https://github.com/Sreeram-R-6/drone-pi-gcs`.
Publish a reviewed source snapshot there; do not send passwords/tokens in chat.
GitHub CLI is not required when using an authenticated GitHub integration.

Prepare a fresh snapshot and clone the existing repository into a separate working
copy. Choose new directories if either example path already exists:

```bash
python3 scripts/prepare_public_release.py --output /tmp/drone-public-release
git clone https://github.com/Sreeram-R-6/drone-pi-gcs.git /tmp/drone-pi-gcs-publish
# Overlay the reviewed snapshot, preserving the clone's .git history.
cp -a /tmp/drone-public-release/. /tmp/drone-pi-gcs-publish/
cd /tmp/drone-pi-gcs-publish
git add .
git diff --cached --stat
git diff --cached --check
git status --short
git commit -m "Update Pi GCS source and documentation"
git push origin main
```

Review files removed locally as well: overlay copying does not remove obsolete
remote files. Stage any intended deletions explicitly. The exporter refuses to
overwrite its output directory; keep older snapshots for comparison. Existing
repository history is retained, and no force push is needed.

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
