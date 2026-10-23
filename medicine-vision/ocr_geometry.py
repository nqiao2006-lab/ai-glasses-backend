"""Explicit source-to-working transforms for optional planar OCR retries.

No annotation or transcript is used for document detection. A quadrilateral is
only a geometric hypothesis; retaining the original OCR remains essential.
"""
from __future__ import annotations

import cv2
import numpy as np


def _image_shape(image):
    if not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
        raise ValueError('Expected a grayscale or BGR image array.')
    height, width = image.shape[:2]
    if height < 1 or width < 1:
        raise ValueError('Image is empty.')
    return height, width


def rotation_variant(image, angle):
    """Rotate clockwise by a quarter turn; H maps original pixel centers."""
    height, width = _image_shape(image)
    if angle not in (0, 90, 180, 270):
        raise ValueError('Rotation must be 0, 90, 180 or 270 degrees.')
    matrices = {
        0: [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        90: [[0, -1, height - 1], [1, 0, 0], [0, 0, 1]],
        180: [[-1, 0, width - 1], [0, -1, height - 1], [0, 0, 1]],
        270: [[0, 1, 0], [-1, 0, width - 1], [0, 0, 1]],
    }
    return dict(image=np.ascontiguousarray(np.rot90(image, k=-angle // 90)),
                H=np.asarray(matrices[angle], dtype=np.float64),
                name=f'rotate-{angle}', metadata=dict(clockwise_degrees=angle))


def map_polygon(points, H):
    """Transform finite 2-D points, rejecting points on the projective horizon."""
    polygon = np.asarray(points, dtype=np.float64)
    matrix = np.asarray(H, dtype=np.float64)
    if polygon.ndim != 2 or polygon.shape[1] != 2 or len(polygon) < 1:
        raise ValueError('Polygon must contain 2-D points.')
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or not np.isfinite(polygon).all():
        raise ValueError('Transform and polygon must be finite.')
    homogeneous = np.column_stack((polygon, np.ones(len(polygon)))) @ matrix.T
    if np.any(np.abs(homogeneous[:, 2]) < 1e-10):
        raise ValueError('Polygon reaches the projective horizon.')
    result = homogeneous[:, :2] / homogeneous[:, 2:3]
    if not np.isfinite(result).all():
        raise ValueError('Transformed polygon is not finite.')
    return result.tolist()


def _ordered_quad(points):
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]
    start = np.argmin(points.sum(axis=1))
    return np.roll(points, -start, axis=0)


def perspective_variant(image):
    """Find one complete, convex plane; return None if no suitable border exists.

    This handles a single page or label, not curved surfaces or multiple faces.
    Border detection is deliberately independent of any text ground truth.
    """
    height, width = _image_shape(image)
    if min(height, width) < 80:
        return None
    scale = min(1.0, 1280 / max(height, width))
    small = cv2.resize(image, None, fx=scale, fy=scale) if scale < 1 else image
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edge = cv2.Canny(gray, 50, 150)
    edge = cv2.morphologyEx(edge, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    _, light = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    sh, sw = gray.shape
    candidates = []
    for mask in (edge, light):
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:100]:
            area = cv2.contourArea(contour)
            fraction = area / (sh * sw)
            if not .12 <= fraction <= .92:
                continue
            approx = cv2.approxPolyDP(contour, .02 * cv2.arcLength(contour, True), True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue
            quad = _ordered_quad(approx)
            # Complete borders only: image-edge rectangles give no evidence of a page.
            if (np.any(quad[:, 0] < 2) or np.any(quad[:, 0] > sw - 3)
                    or np.any(quad[:, 1] < 2) or np.any(quad[:, 1] > sh - 3)):
                continue
            edges = np.roll(quad, -1, axis=0) - quad
            lengths = np.linalg.norm(edges, axis=1)
            if lengths.min() < max(25, .05 * min(sh, sw)) or lengths.max() / lengths.min() > 8:
                continue
            previous = -np.roll(edges, 1, axis=0)
            cosines = np.sum(previous * edges, axis=1) / (np.linalg.norm(previous, axis=1) * lengths)
            if np.max(np.abs(cosines)) > .85:
                continue
            quad_area = cv2.contourArea(quad)
            if not .85 <= quad_area / area <= 1.15:
                continue
            candidates.append((quad_area, quad))
    if not candidates:
        return None
    _, quad = max(candidates, key=lambda item: item[0])
    # Use actual resize factors, avoiding subpixel drift from rounded dimensions.
    quad = quad.astype(np.float64) * np.array([width / sw, height / sh])
    lengths = np.linalg.norm(np.roll(quad, -1, axis=0) - quad, axis=1)
    out_w, out_h = max(lengths[0], lengths[2]), max(lengths[1], lengths[3])
    reduction = min(1.0, 2500 / max(out_w, out_h))
    out_w, out_h = max(2, round(out_w * reduction)), max(2, round(out_h * reduction))
    target = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], np.float32)
    H = cv2.getPerspectiveTransform(quad.astype(np.float32), target)
    if not np.isfinite(H).all() or abs(np.linalg.det(H)) < 1e-10:
        return None
    rectified = cv2.warpPerspective(image, H, (out_w, out_h), flags=cv2.INTER_CUBIC,
                                    borderMode=cv2.BORDER_REPLICATE)
    return dict(image=rectified, H=H, name='perspective',
                metadata=dict(source_quad=quad.tolist(), working_size=[out_w, out_h],
                              detector='largest complete convex quadrilateral; no text annotations'))
