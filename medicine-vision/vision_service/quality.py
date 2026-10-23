"""Conservative heuristic quality checks, never a calibrated readability model."""
from io import BytesIO
import warnings
import cv2
import numpy as np
from PIL import Image, ImageOps
from .store import Problem


def inspect_jpeg(data, max_pixels=16_000_000):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != 'JPEG':
                    raise Problem('unsupported_image', '请上传 JPEG 图片。', 415)
                width, height = image.size
                if width*height > max_pixels:
                    raise Problem('image_too_large', '图片像素过多，请降低拍照分辨率。', 413)
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image = ImageOps.exif_transpose(image).convert('RGB')
                width, height = image.size
                image.thumbnail((1024,1024))
                gray = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
    except Problem:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise Problem('invalid_image', '无法解码完整 JPEG，请重新拍照上传。', 422) from exc
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean, spread = float(gray.mean()), float(gray.std())
    clipped = float((gray >= 250).mean())
    warnings_list, reasons = [], []
    if min(width,height) < 96:
        reasons.append('resolution_too_small')
    if spread < 4:
        reasons.append('almost_uniform')
    if variance < 6:
        reasons.append('severe_blur_or_no_detail')
    if mean > 249 and spread < 12:
        reasons.append('severe_overexposure')
    if mean < 9:
        reasons.append('severe_underexposure')
    if variance < 45:
        warnings_list.append('possible_blur')
    if mean < 45:
        warnings_list.append('dark_image')
    return dict(width=width,height=height,laplacian_variance=variance,mean_luminance=mean,
                luminance_std=spread,white_fraction=clipped,rejected=bool(reasons),
                rejection_reasons=reasons,warnings=warnings_list,
                method='thumbnail1024/laplacian-v1; heuristic, not a readability guarantee')
