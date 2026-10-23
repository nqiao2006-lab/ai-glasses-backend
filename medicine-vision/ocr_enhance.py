"""Optional camera-angle retries with source-coordinate evidence.

Candidate selection uses model scores only, never benchmark transcriptions.
Every attempted recognition is retained so a selected retry stays auditable.
"""
from __future__ import annotations

from copy import deepcopy
import math


def quality(lines):
    """A retry heuristic, NOT an estimate of character accuracy."""
    characters = 0
    weighted_confidence = 0.0
    score = 0.0
    for line in lines:
        count = sum(char.isalnum() for char in line.get('text', ''))
        confidence = line.get('ocr_confidence')
        if not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
            continue
        confidence = min(1.0, max(0.0, confidence))
        characters += count
        weighted_confidence += count * confidence
        score += count * max(0.0, (confidence - 0.5) / 0.5) ** 2
    return dict(score=score, characters=characters,
                mean_confidence=weighted_confidence / max(characters, 1))


def select_candidate(candidates):
    """Keep the original unless a retry has materially more reliable text."""
    original = candidates[0]
    best = max(candidates, key=lambda item: item['quality']['score'])
    base, trial = original['quality'], best['quality']
    if (best is not original and trial['characters'] >= 6
            and trial['mean_confidence'] >= 0.75
            and trial['score'] >= max(base['score'] * 1.15, base['score'] + 2.0)):
        return best
    return original


def recognize_candidates(image, recognize, *, perspective=False):
    """recognize(BGR ndarray) -> raw line dictionaries in working coordinates."""
    import numpy as np
    from ocr_geometry import rotation_variant, perspective_variant, map_polygon
    from ocr_layout import normalize_layout

    variants = [rotation_variant(image, angle) for angle in (0, 90, 180, 270)]
    if perspective:
        flat = perspective_variant(image)
        if flat is not None:
            for angle in (0, 90, 180, 270):
                rotated = rotation_variant(flat['image'], angle)
                rotated['H'] = rotated['H'] @ flat['H']
                rotated['name'] = 'perspective-' + rotated['name']
                rotated['metadata'] = dict(rotated.get('metadata', {}),
                                           perspective=flat.get('metadata', {}))
                variants.append(rotated)

    candidates = []
    for variant in variants:
        raw = recognize(variant['image'])
        arranged = normalize_layout(raw)
        inverse = np.linalg.inv(variant['H'])

        def source_coordinates(line):
            result = deepcopy(line)
            if result.get('bbox') is not None:
                result['layout_bbox'] = deepcopy(result['bbox'])
                result['bbox'] = map_polygon(result['bbox'], inverse)
            result['recognition_variant'] = variant['name']
            if 'source_fragments' in result:
                result['source_fragments'] = [source_coordinates(x)
                                               for x in result['source_fragments']]
            return result

        candidates.append(dict(name=variant['name'], quality=quality(raw),
                               transform_source_to_working=variant['H'].tolist(),
                               metadata=variant.get('metadata', {}),
                               raw_lines=[source_coordinates(x) for x in raw],
                               lines=[source_coordinates(x) for x in arranged]))
    selected = select_candidate(candidates)
    return dict(raw_lines=selected['raw_lines'], lines=selected['lines'],
                enhancement=dict(selected=selected['name'],
                                 selection='confidence-weighted character coverage; not measured accuracy',
                                 perspective_enabled=perspective),
                recognition_candidates=candidates)
