"""Conservatively recover reading order for mixed vertical/horizontal OCR.

Only geometry controls grouping. Original OCR dictionaries are retained in
``source_fragments`` so downstream extraction can keep every source box.
"""

from __future__ import annotations

from copy import deepcopy
import math
from statistics import median


def _rectangle(line):
    box = line.get("bbox")
    if box is None:
        return None
    try:
        if len(box) == 4 and all(isinstance(value, (int, float)) for value in box):
            left, top, right, bottom = map(float, box)
        else:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        if not all(math.isfinite(value) for value in (left, top, right, bottom)):
            return None
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom
    except (TypeError, ValueError, IndexError):
        return None


def _fragments(line):
    # Flatten a previous normalization without making evidence recursive.
    existing = line.get("source_fragments")
    if isinstance(existing, list) and existing:
        return [deepcopy(fragment) for fragment in existing]
    return [deepcopy(line)]


def _merge(items, orientation, group_id):
    """Merge a column segment; each item is (input_position, line, rectangle)."""
    ordered = sorted(items, key=lambda item: (item[2][1], item[0]))
    result = deepcopy(ordered[0][1])
    result["text"] = "".join(str(item[1].get("text", "")) for item in ordered)
    fragments = [fragment for item in ordered for fragment in _fragments(item[1])]
    result["source_fragments"] = fragments
    indices = [fragment.get("line_index") for fragment in fragments]
    numeric_indices = [index for index in indices if isinstance(index, (int, float))]
    result["line_index"] = min(numeric_indices) if numeric_indices else ordered[0][0]
    scores = [fragment.get("ocr_confidence") for fragment in fragments]
    numeric_scores = [float(score) for score in scores
                      if isinstance(score, (int, float)) and math.isfinite(score)]
    result["ocr_confidence"] = min(numeric_scores) if numeric_scores else None
    left = min(item[2][0] for item in ordered)
    top = min(item[2][1] for item in ordered)
    right = max(item[2][2] for item in ordered)
    bottom = max(item[2][3] for item in ordered)
    result["bbox"] = [[left, top], [right, top], [right, bottom], [left, bottom]]
    result["layout_group_id"] = group_id
    result["layout_orientation"] = orientation
    return result


def normalize_layout(lines) -> list[dict]:
    """Return reading-order lines, preserving all original OCR fragments.

    At least three tall boxes are required before vertical handling is enabled.
    Vertical columns join only nearby fragments at similar x coordinates; their
    overlapping y intervals form blocks read from right to left. Horizontal
    lines remain separate and are interleaved with blocks by top position.
    """
    entries = [(index, line, _rectangle(line)) for index, line in enumerate(lines)]
    seeds = [item for item in entries if item[2] is not None
             and item[2][3] - item[2][1] > 35
             and item[2][3] - item[2][1] > 1.4 * (item[2][2] - item[2][0])]
    vertical_positions = set()
    columns = []
    if len(seeds) >= 3:
        column_width = median(item[2][2] - item[2][0] for item in seeds)
        tolerance = 0.55 * column_width
        # Clustering around the median limits drift into an adjacent column.
        for item in sorted(seeds, key=lambda value: (value[2][0] + value[2][2]) / 2):
            center = (item[2][0] + item[2][2]) / 2
            compatible = [(abs(center - median((x[2][0] + x[2][2]) / 2 for x in col)), col)
                          for col in columns
                          if abs(center - median((x[2][0] + x[2][2]) / 2 for x in col)) < tolerance]
            if compatible:
                min(compatible, key=lambda candidate: candidate[0])[1].append(item)
            else:
                columns.append([item])
            vertical_positions.add(item[0])

        # A square single glyph can be a broken vertical heading or its value.
        for item in entries:
            if item[0] in vertical_positions or item[2] is None:
                continue
            if len(str(item[1].get("text", "")).strip()) != 1:
                continue
            width = item[2][2] - item[2][0]
            if width > 1.5 * column_width:
                continue
            center = (item[2][0] + item[2][2]) / 2
            compatible = []
            for column in columns:
                distance = abs(center - median((x[2][0] + x[2][2]) / 2 for x in column))
                nearest_y_gap = min(max(x[2][1] - item[2][3], item[2][1] - x[2][3], 0)
                                    for x in column)
                if distance < tolerance and nearest_y_gap < max(3 * column_width, 70):
                    compatible.append((distance, column))
            if compatible:
                min(compatible, key=lambda candidate: candidate[0])[1].append(item)
                vertical_positions.add(item[0])

    column_segments = []
    for column in columns:
        ordered = sorted(column, key=lambda item: (item[2][1], item[0]))
        segment = []
        segment_bottom = None
        width = median(item[2][2] - item[2][0] for item in ordered)
        for item in ordered:
            if segment and item[2][1] - segment_bottom >= max(3 * width, 70):
                column_segments.append(segment)
                segment = []
                segment_bottom = None
            segment.append(item)
            segment_bottom = max(segment_bottom or item[2][3], item[2][3])
        if segment:
            column_segments.append(segment)

    # Interval-overlap components form separate vertical text blocks.
    blocks = []
    block_bottom = None
    for segment in sorted(column_segments, key=lambda seg: min(item[2][1] for item in seg)):
        top = min(item[2][1] for item in segment)
        bottom = max(item[2][3] for item in segment)
        if not blocks or top > block_bottom:
            blocks.append([segment])
            block_bottom = bottom
        else:
            blocks[-1].append(segment)
            block_bottom = max(block_bottom, bottom)

    chunks = []
    for block_index, block in enumerate(blocks):
        group_id = f"vertical-{block_index}"
        ordered = sorted(block, key=lambda seg: (
            -median((item[2][0] + item[2][2]) / 2 for item in seg),
            min(item[2][1] for item in seg)))
        merged = [_merge(segment, "vertical", group_id) for segment in ordered]
        top = min(item[2][1] for segment in block for item in segment)
        first = min(item[0] for segment in block for item in segment)
        chunks.append((top, first, merged))

    for position, line, rectangle in entries:
        if position in vertical_positions:
            continue
        copy = deepcopy(line)
        copy["source_fragments"] = _fragments(line)
        copy["layout_group_id"] = f"horizontal-{position}"
        copy["layout_orientation"] = "horizontal"
        # Missing/invalid coordinates retain relative input order at the end.
        chunks.append((rectangle[1] if rectangle else float("inf"), position, [copy]))
    chunks.sort(key=lambda chunk: (chunk[0], chunk[1]))
    return [line for _, _, chunk in chunks for line in chunk]
