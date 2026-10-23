"""Verify delivered files using only the Python standard library."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def matches_manifest(data, row):
    """Allow Git's LF/CRLF conversion for text; retain exact content hashes."""
    candidates = [data]
    if Path(row['path']).suffix.lower() in {'.py', '.txt', '.md', '.json', '.yml', '.yaml', '.sql', '.cmd'}:
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            pass
        else:
            if '\x00' not in text:
                lf = data.replace(b'\r\n', b'\n')
                candidates.extend((lf, lf.replace(b'\n', b'\r\n')))
    return any(len(candidate) == row['bytes'] and
               hashlib.sha256(candidate).hexdigest() == row['sha256']
               for candidate in candidates)


def verify():
    manifest = json.loads((ROOT / 'bundle_manifest.json').read_text(encoding='utf-8'))
    for row in manifest['files']:
        path = (ROOT / row['path']).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError('Manifest path leaves the package directory')
        if not path.is_file():
            raise ValueError('Missing or changed file: ' + row['path'])
        if not matches_manifest(path.read_bytes(), row):
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
