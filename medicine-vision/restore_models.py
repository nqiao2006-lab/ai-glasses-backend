"""Restore byte-identical model files transported in GitHub web-sized parts."""
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile


def restore(root=None):
    root = Path(root or Path(__file__).resolve().parent)
    metadata = root / 'model_parts.json'
    if not metadata.exists():
        # A normal local checkout already contains the original models.
        if not (root / 'models').is_dir():
            raise RuntimeError('Neither original models nor model_parts.json are present')
        return
    manifest = json.loads((root / 'bundle_manifest.json').read_text(encoding='utf-8'))
    expected = {item['path']: item for item in manifest['files']
                if item['path'].startswith('models/')}
    parts = json.loads(metadata.read_text(encoding='utf-8'))
    with tempfile.TemporaryFile() as archive:
        for index, part in enumerate(parts, 1):
            name = f'model_bundle.part{index:03d}'
            if part['name'] != name:
                raise RuntimeError('Invalid model part sequence')
            data = (root / name).read_bytes()
            if len(data) != part['bytes'] or hashlib.sha256(data).hexdigest() != part['sha256']:
                raise RuntimeError(f'Model part checksum failed: {name}')
            archive.write(data)
        archive.seek(0)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(expected) or set(names) != set(expected):
                raise RuntimeError('Unexpected or missing model archive members')
            # Validate every member before writing any model files.
            for name in names:
                data = bundle.read(name)
                item = expected[name]
                if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                    raise RuntimeError(f'Original model checksum failed: {name}')
            for name in names:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(name))
    print(f'Restored and verified {len(expected)} original model files')


if __name__ == '__main__':
    restore()
