from dataclasses import dataclass
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / 'local_vision_data'
    max_upload_bytes: int = 8 * 1024 * 1024
    max_image_pixels: int = 16_000_000
    max_pending_tasks: int = 32
    inference_timeout: float = 120.0
    startup_timeout: float = 180.0
    waiting_timeout: float = 600.0
    api_token: str = ''

    @property
    def database(self):
        return self.data_dir / 'vision_test.sqlite3'

    @classmethod
    def from_env(cls):
        return cls(data_dir=Path(os.environ.get('VISION_DATA_DIR', str(ROOT / 'local_vision_data'))).resolve(),
                   inference_timeout=float(os.environ.get('VISION_INFERENCE_TIMEOUT', '120')),
                   startup_timeout=float(os.environ.get('VISION_STARTUP_TIMEOUT', '180')),
                   waiting_timeout=float(os.environ.get('VISION_WAITING_TIMEOUT', '600')),
                   api_token=os.environ.get('VISION_API_TOKEN', ''))
