"""Offline setup/export regression tests, never connected to hardware."""
from pathlib import Path
import shutil
import tempfile
import unittest
from create_local_config import create
from prepare_public_release import candidates, SECRET


class ReleaseTests(unittest.TestCase):
    def test_local_config_is_unapproved_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'drone_project/pi_flight'
            folder.mkdir(parents=True)
            shutil.copyfile(Path(__file__).resolve().parents[1] /
                            'drone_project/pi_flight/config.example.toml',
                            folder / 'config.example.toml')
            self.assertTrue(create(tmp))
            original = (folder / 'config.toml').read_bytes()
            self.assertIn(b'udpin:127.0.0.1:14550', original)
            self.assertIn(b'thresholds_approved = false', original)
            self.assertFalse(create(tmp))
            self.assertEqual((folder / 'config.toml').read_bytes(), original)

    def test_export_excludes_local_evidence_and_runtime_data(self):
        paths = [p.as_posix() for _, p in candidates()]
        self.assertIn('gcs', paths)
        self.assertIn('drone_project/pi_flight/config.example.toml', paths)
        for bad in ('drone_project/pi_flight/config.toml',
                    'drone_project/pi_flight/fc-review-current.parm',
                    'drone_project/drone_oled/commissioning/profile.live.json'):
            self.assertNotIn(bad, paths)
        self.assertFalse(any('/.venv/' in p or '/logs/' in p or '.tlog' in p or p.endswith(('.swp','.swo','.tmp','~')) for p in paths))

    def test_scanner_recognizes_common_credentials(self):
        self.assertIsNotNone(SECRET.search(b'ghp_' + b'A' * 36))
        self.assertIsNotNone(SECRET.search(b'-----BEGIN ' + b'OPENSSH PRIVATE KEY-----'))
        self.assertIsNone(SECRET.search(b'normal application source'))


if __name__ == '__main__':
    unittest.main()
