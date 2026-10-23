"""Verify delivered files using only the Python standard library."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def verify():
    manifest = json.loads((ROOT / 'bundle_manifest.json').read_text(encoding='utf-8'))
    for row in manifest['files']:
        path = (ROOT / row['path']).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError('Manifest path leaves the package directory')
        if not path.is_file() or path.stat().st_size != row['bytes']:
            raise ValueError('Missing or changed file: ' + row['path'])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != row['sha256']:
            raise ValueError('SHA256 mismatch: ' + row['path'])
    config = json.loads((ROOT / 'runtime_config.json').read_text(encoding='utf-8'))
    if config != {'cache_dir': 'models'}:
        raise ValueError('Expected a portable, package-local model configuration')
    for name in ('PP-OCRv6_medium_det', 'PP-OCRv6_medium_rec'):
        for filename in ('inference.json', 'inference.pdiparams', 'inference.yml'):
            if not (ROOT / 'models' / 'paddlex' / 'official_models' / name / filename).is_file():
                raise ValueError('Missing model file: ' + name + '/' + filename)
    return {'passed': True, 'verified_files': len(manifest['files']),
            'model_configuration': 'package-local relative path'}


if __name__ == '__main__':
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
