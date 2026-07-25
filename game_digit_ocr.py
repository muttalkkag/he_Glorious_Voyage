from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np


@dataclass(frozen=True)
class DigitReadResult:
    value: Optional[int]
    confidence: float
    digit_count: int
    text: str = ""


class GameDigitOCR:
    """Fast template OCR tuned for the fixed number font used by the game.

    It does not run a text detector or a neural OCR model. The cell is segmented
    into digit glyphs and matched against templates learned from real game
    screenshots. This makes the normal 18-cell numeric pass very fast. Neural
    OCR is only needed as a fallback when segmentation or confidence is weak.
    """

    CANVAS_H = 32
    CANVAS_W = 24

    def __init__(self, template_path: Path | str):
        path = Path(template_path)
        if not path.exists():
            raise FileNotFoundError(f"숫자 템플릿 파일이 없습니다: {path}")
        data = np.load(path, allow_pickle=False)
        templates: dict[str, np.ndarray] = {}
        for digit in "0123456789":
            key = f"digit_{digit}"
            if key not in data:
                raise ValueError(f"숫자 템플릿 {digit}이 없습니다.")
            value = np.asarray(data[key], dtype=np.uint8)
            if value.ndim != 3:
                raise ValueError(f"숫자 템플릿 형식 오류: {digit}")
            templates[digit] = value
        self.templates = templates
        self._banks: dict[str, np.ndarray] = {}
        self._bank_sums: dict[str, np.ndarray] = {}
        for digit, exemplars in templates.items():
            shifted_items = []
            for exemplar in exemplars:
                template = exemplar.astype(np.float32)
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        shifted_items.append(
                            self._shift_without_wrap(template, dy, dx).reshape(-1)
                        )
            bank = np.stack(shifted_items).astype(np.float32)
            self._banks[digit] = bank
            self._bank_sums[digit] = bank.sum(axis=1)

    @staticmethod
    def _number_roi(cell: np.ndarray, is_price: bool) -> np.ndarray:
        h, w = cell.shape[:2]
        # Row border/highlight lines are outside this vertical band. Price
        # arrows remain outside the digit band or are rejected by height.
        y1 = max(0, int(round(h * 0.14)))
        y2 = min(h, int(round(h * 0.86)))
        x1 = max(0, int(round(w * 0.02)))
        x2 = min(w, int(round(w * (0.82 if is_price else 0.98))))
        return cell[y1:y2, x1:x2]

    @staticmethod
    def _foreground_mask(roi: np.ndarray) -> np.ndarray:
        if roi.size == 0:
            return np.zeros((1, 1), dtype=np.uint8)
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        value = hsv[:, :, 2]
        # The UI number fills are white/cyan/pink/yellow and consistently
        # brighter than the brown table background. A mild dynamic threshold
        # keeps the mask stable when screenshots are resized or compressed.
        p95 = float(np.percentile(value, 95))
        threshold = int(max(130, min(175, p95 * 0.72)))
        mask = (value > threshold).astype(np.uint8) * 255
        if min(mask.shape) >= 4:
            mask = cv2.morphologyEx(
                mask, cv2.MORPH_CLOSE, np.ones((2, 1), np.uint8)
            )
        return mask

    @classmethod
    def _segment_glyphs(cls, cell: np.ndarray, is_price: bool) -> list[np.ndarray]:
        roi = cls._number_roi(cell, is_price)
        mask = cls._foreground_mask(roi)
        if mask.size == 0:
            return []

        column_counts = (mask > 0).sum(axis=0)
        active = column_counts >= max(2, int(round(mask.shape[0] * 0.06)))

        runs: list[tuple[int, int]] = []
        start: Optional[int] = None
        for index, enabled in enumerate(active):
            if enabled and start is None:
                start = index
            elif not enabled and start is not None:
                runs.append((start, index))
                start = None
        if start is not None:
            runs.append((start, len(active)))

        min_digit_height = max(9, int(round(mask.shape[0] * 0.40)))
        glyphs: list[np.ndarray] = []
        for x1, x2 in runs:
            ys, xs = np.where(mask[:, x1:x2] > 0)
            if len(xs) == 0:
                continue
            y1 = int(ys.min())
            y2 = int(ys.max()) + 1
            width = x2 - x1
            height = y2 - y1
            area = int(len(xs))

            # Ignore comma, trend arrow, and tiny antialiasing fragments.
            if height < min_digit_height or area < 15:
                continue
            if width > mask.shape[1] * 0.25:
                continue
            glyphs.append(mask[y1:y2, x1:x2])
        return glyphs

    @classmethod
    def _normalize(cls, glyph: np.ndarray) -> np.ndarray:
        ys, xs = np.where(glyph > 0)
        if len(xs) == 0:
            return np.zeros((cls.CANVAS_H, cls.CANVAS_W), dtype=np.uint8)
        glyph = glyph[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        scale = min(26.0 / max(1, glyph.shape[0]), 20.0 / max(1, glyph.shape[1]))
        width = max(1, int(round(glyph.shape[1] * scale)))
        height = max(1, int(round(glyph.shape[0] * scale)))
        resized = cv2.resize(glyph, (width, height), interpolation=cv2.INTER_NEAREST)
        output = np.zeros((cls.CANVAS_H, cls.CANVAS_W), dtype=np.uint8)
        y = (cls.CANVAS_H - height) // 2
        x = (cls.CANVAS_W - width) // 2
        output[y:y + height, x:x + width] = (resized > 0).astype(np.uint8)
        return output

    @staticmethod
    def _shift_without_wrap(image: np.ndarray, dy: int, dx: int) -> np.ndarray:
        shifted = np.roll(np.roll(image, dy, axis=0), dx, axis=1).copy()
        if dy > 0:
            shifted[:dy, :] = 0
        elif dy < 0:
            shifted[dy:, :] = 0
        if dx > 0:
            shifted[:, :dx] = 0
        elif dx < 0:
            shifted[:, dx:] = 0
        return shifted

    def _recognize_glyph(self, glyph: np.ndarray) -> tuple[str, float, float]:
        query = self._normalize(glyph).astype(np.float32).reshape(-1)
        query_sum = float(query.sum())
        best_digit = ""
        best_score = -1.0
        second_score = -1.0

        for digit, bank in self._banks.items():
            intersections = bank @ query
            denominators = self._bank_sums[digit] + query_sum
            scores = np.divide(
                2.0 * intersections,
                denominators,
                out=np.zeros_like(intersections),
                where=denominators > 0,
            )
            digit_score = float(scores.max(initial=0.0))
            if digit_score > best_score:
                second_score = best_score
                best_score = digit_score
                best_digit = digit
            elif digit_score > second_score:
                second_score = digit_score

        margin = max(0.0, best_score - max(0.0, second_score))
        return best_digit, best_score, margin

    def read_cell(
        self,
        cell: np.ndarray,
        is_price: bool,
        max_value: int,
    ) -> DigitReadResult:
        glyphs = self._segment_glyphs(cell, is_price)
        if not glyphs or len(glyphs) > 5:
            return DigitReadResult(None, 0.0, len(glyphs), "")

        digits: list[str] = []
        scores: list[float] = []
        margins: list[float] = []
        for glyph in glyphs:
            digit, score, margin = self._recognize_glyph(glyph)
            if not digit:
                return DigitReadResult(None, 0.0, len(glyphs), "")
            digits.append(digit)
            scores.append(score)
            margins.append(margin)

        text = "".join(digits)
        try:
            value = int(text)
        except ValueError:
            return DigitReadResult(None, 0.0, len(glyphs), text)
        if value > max_value:
            return DigitReadResult(None, min(scores), len(glyphs), text)

        # Confidence includes both shape similarity and separation from the
        # second-best digit. Exact game-font matches generally exceed 0.82.
        shape_score = min(scores)
        margin_score = min(1.0, (min(margins) + 0.02) / 0.16)
        confidence = max(0.0, min(1.0, shape_score * 0.82 + margin_score * 0.18))
        return DigitReadResult(value, confidence, len(glyphs), text)
