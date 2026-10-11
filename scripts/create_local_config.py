"""Create an unapproved shared-router config; never overwrite a local setup."""
from pathlib import Path


def create(base):
    path = Path(base) / 'drone_project/pi_flight/config.toml'
    if path.exists():
        print('Preserving existing local config:', path)
        return False
    sample = path.with_name('config.example.toml').read_text()
    sample = sample.replace(
        'endpoint = "/dev/serial0" # Direct UART default; do not run MAVProxy alongside GCS.',
        'endpoint = "udpin:127.0.0.1:14550" # Shared UART router; OLED on 14553.')
    with path.open('x') as stream:
        stream.write(sample)
    print('Created unreviewed local config:', path)
    return True


if __name__ == '__main__':
    create(Path(__file__).resolve().parents[1])
