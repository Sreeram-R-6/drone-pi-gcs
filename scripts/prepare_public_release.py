"""Export source without Git history, local evidence, logs or environments."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import stat

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache',
             '.mypy_cache', '.ruff_cache', 'build', 'dist', 'install', 'log',
             'logs', 'fc_logs', 'wheelhouse', 'previews', 'deployment-local',
             '.locks', '.agents', '.aws', '.codex'}
PRIVATE_FILES = {
    'pi_flight/config.toml', 'pi_flight/fc-review-current.parm',
    'pi_flight/FC_EVIDENCE_REVIEW.md', 'pi_flight/OPERATOR_REVIEW.md',
    'pi_flight/REQUESTED_FC_SETUP.md', 'pi_flight/VALIDATION.md',
    'drone_oled/VALIDATION.md', 'drone_oled/commissioning/fc-audit.json',
    'drone_oled/commissioning/profile.live.json', 'drone_oled/commissioning/README.md',
}
PRIVATE_NAMES = {'mav.parm', '.github_token', '.DS_Store', 'splash-original.jpg'}
SECRET = re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|'
                    rb'AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)')


def candidates():
    for name in ('README.md', 'PUBLICATION.md', 'requirements.txt',
                 'requirements-dev.txt', '.gitignore', 'gcs'):
        yield ROOT / name, Path(name)
    for directory in ('scripts', 'drone_project', '.github'):
        for parent, dirs, names in os.walk(ROOT / directory, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                             and not d.endswith('.egg-info')
                             and not (Path(parent) / d).is_symlink())
            for name in sorted(names):
                path = Path(parent) / name
                rel = path.relative_to(ROOT)
                project_rel = path.relative_to(ROOT / directory).as_posix()
                if (path.is_symlink() or not path.is_file() or name in PRIVATE_NAMES
                    or name.startswith(('.env', '.lgd-nfy'))
                    or (directory == 'drone_project' and project_rel in PRIVATE_FILES)
                    or any(s in name for s in ('.tlog', '.backup', '.qrbackup', '.cameratest'))
                    or path.suffix in {'.pem', '.key', '.pyc', '.so', '.bin', '.part'}):
                    continue
                # Large third-party simulator meshes/textures remain upstream.
                # Preserve model descriptors and all application/test images.
                if 'models' in rel.parts and path.suffix.lower() in {
                        '.dae', '.stl', '.obj', '.mtl', '.png', '.jpg', '.jpeg', '.tga'}:
                    continue
                yield path, rel


def export():
    target = ROOT / 'public-release'
    if target.exists():
        raise SystemExit('public-release already exists; preserve/review it or choose a fresh checkout.')
    files = list(candidates())
    # Check before creating/copying anything, including unexpectedly large files.
    for source, rel in files:
        if source.stat().st_size > 20 * 1024 * 1024:
            raise SystemExit(f'Review oversized public file: {rel}')
        if SECRET.search(source.read_bytes()):
            raise SystemExit(f'Possible credential in {rel}; release not created.')
    target.mkdir()
    for source, rel in files:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    print(f'Prepared {len(files)} files in {target}; originals unchanged.')
    print('Review before publishing. Local evidence and third-party simulator meshes excluded.')


def upload_batch(start):
    """JSON for authenticated publishing tools; does not perform network writes."""
    target = ROOT / 'public-release'
    paths = sorted(p for p in target.rglob('*') if p.is_file() and '.git' not in p.parts)
    batch = []
    size = 0
    for p in paths[start:]:
        raw = p.read_bytes()
        if batch and size + len(raw) > 20000:
            break
        try:
            content, encoding = raw.decode('utf-8'), 'utf-8'
        except UnicodeDecodeError:
            content, encoding = base64.b64encode(raw).decode('ascii'), 'base64'
        batch.append({'path': p.relative_to(target).as_posix(), 'content': content,
                      'encoding': encoding,
                      'mode': '100755' if p.stat().st_mode & stat.S_IXUSR else '100644'})
        size += len(raw)
    print(json.dumps({'files': batch, 'next': start + len(batch), 'total': len(paths)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upload-batch', type=int)
    args = parser.parse_args()
    if args.upload_batch is not None:
        upload_batch(args.upload_batch)
    else:
        export()
