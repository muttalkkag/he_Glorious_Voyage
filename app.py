
from __future__ import annotations

import ctypes
import difflib
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional

from scipy.optimize import Bounds, LinearConstraint, milp

# Windows DPI scaling and screenshot coordinates should use the same pixel system.
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

import cv2
import mss
import numpy as np
import pyautogui
from PIL import Image, ImageTk

from game_digit_ocr import GameDigitOCR

from hex_map import (
    DEFAULT_HEX_MAP,
    HexMapEditor,
    MAP_SLOT_LABELS as HEX_SLOT_LABELS,
    build_pair_path_options,
    normalize_hex_map,
    parse_cell,
    simulate_hex_path,
)

SOURCE_DIR = Path(__file__).resolve().parent
APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else SOURCE_DIR
)
CONFIG_PATH = APP_DIR / "config.json"
DATA_PATH = APP_DIR / "trade_data.json"
ERROR_LOG = APP_DIR / "runtime_error.log"

ISLANDS = [
    "농부들의 섬",
    "목동들의 섬",
    "어부들의 섬",
    "감정가들의 섬",
    "장인들의 섬",
    "조각가들의 섬",
]

MAP_SLOT_LABELS = [
    "북서쪽",
    "북동쪽",
    "서쪽",
    "중앙",
    "동쪽",
    "남쪽",
]

# 맵의 물리적 위치는 고정이고, 매 판 섬 이름만 이 여섯 위치에 바뀌어 배치된다.
# 순서: 북서, 북동, 서쪽, 중앙, 동쪽, 남쪽
FIXED_SLOT_DISTANCE_MATRIX = [
    [0, 5, 4, 4, 4, 9],
    [5, 0, 8, 4, 4, 9],
    [4, 8, 0, 5, 7, 6],
    [4, 4, 5, 0, 4, 6],
    [4, 4, 7, 4, 0, 6],
    [9, 9, 6, 6, 6, 0],
]

DEFAULT_SLOT_ISLANDS = [
    "농부들의 섬",   # 북서
    "어부들의 섬",   # 북동
    "목동들의 섬",   # 서쪽
    "장인들의 섬",   # 중앙
    "조각가들의 섬", # 동쪽
    "감정가들의 섬", # 남쪽
]

DEFAULT_CONFIG = {
    "capture_monitor": 1,
    "capture_delay": 0.12,
    "ocr_price_max": 9999,
    "ocr_stock_max": 99,
    "ocr_game_font_fast": True,
    "ocr_gpu_enabled": False,
    "route_solve_limit": 5000,
    "slot_islands": DEFAULT_SLOT_ISLANDS,
    "fixed_slot_distance_matrix": FIXED_SLOT_DISTANCE_MATRIX,
    "required_islands": [],
    "forbidden_islands": [],
    "hex_map": DEFAULT_HEX_MAP,
    "max_durability": 4800,
    "route_objective": "이번 회차 코인 최대",
    "auto_balance_cycle_turns": 15,
    "regions": {
        "product_name": None,
        "buy_column": None,
        "sell_column": None,
        "stock_column": None,
    },
    "region_screen_size": None,
    "distance_matrix": [
        [0, 5, 7, 6, 4, 7],
        [5, 0, 7, 8, 6, 6],
        [7, 7, 0, 5, 5, 8],
        [6, 8, 5, 0, 4, 5],
        [4, 6, 5, 4, 0, 4],
        [7, 6, 8, 5, 4, 0, 4],
    ],
    "map_calibration": None,
}

def load_json(path: Path, default):
    if not path.exists():
        save_json(path, default)
        return json.loads(json.dumps(default, ensure_ascii=False))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(default, ensure_ascii=False))

def save_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

@dataclass
class Market:
    buy: Optional[int] = None
    sell: Optional[int] = None
    stock: Optional[int] = None
    buy_unavailable: bool = False
    sell_unavailable: bool = False
    stock_unavailable: bool = False
    buy_review: bool = False
    sell_review: bool = False
    stock_review: bool = False

@dataclass
class OCRCellResult:
    value: Optional[int] = None
    unavailable: bool = False
    review: bool = False

@dataclass
class Good:
    name: str
    active: bool
    markets: dict[str, Market]
    source_file: str = ""

    def to_dict(self):
        return {
            "name": self.name,
            "active": self.active,
            "markets": {k: asdict(v) for k, v in self.markets.items()},
            "source_file": self.source_file,
        }

    @staticmethod
    def from_dict(value):
        markets = {}
        raw_markets = value.get("markets", {})
        for island in ISLANDS:
            raw = raw_markets.get(island, {})
            markets[island] = Market(
                buy=raw.get("buy"),
                sell=raw.get("sell"),
                stock=raw.get("stock"),
                buy_unavailable=bool(raw.get("buy_unavailable", False)),
                sell_unavailable=bool(raw.get("sell_unavailable", False)),
                stock_unavailable=bool(raw.get("stock_unavailable", False)),
                buy_review=bool(raw.get("buy_review", False)),
                sell_review=bool(raw.get("sell_review", False)),
                stock_review=bool(raw.get("stock_review", False)),
            )
        return Good(
            name=value.get("name") or "이름 없음",
            active=bool(value.get("active", True)),
            markets=markets,
            source_file=str(value.get("source_file", "")),
        )

def blank_good(index: int) -> Good:
    return Good(
        name=f"무역품 {index + 1}",
        active=True,
        markets={island: Market() for island in ISLANDS},
        source_file="",
    )

class OCRReader:
    """
    상품명은 Windows OCR을 우선 사용한다.
    숫자는 전체 표 OCR 뒤 의심 셀만 재검사하며 다음을 별도로 판정한다.

    - 천 단위 쉼표가 있는데 앞자리 1이 빠진 경우
    - 가격 옆 화살표가 숫자로 붙은 경우
    - 구매/재고가 '-'로 표시된 구매 불가 셀
    """

    def __init__(self, gpu_requested: bool = False):
        self.winocr = None
        self.rapid = None
        self.game_digits = None
        self.winocr_error = None
        self.rapid_error = None
        self.game_digit_error = None
        self.gpu_requested = bool(gpu_requested)
        self.gpu_active = False
        self.gpu_available = False
        self.rapid_initialized = False
        self.last_grid_stats = {"fast": 0, "fallback": 0, "dash": 0}

        try:
            import winocr
            self.winocr = winocr
        except Exception as exc:
            self.winocr_error = str(exc)

        try:
            self.game_digits = GameDigitOCR(
                SOURCE_DIR / "ocr_digit_templates.npz"
            )
        except Exception as exc:
            self.game_digit_error = str(exc)

        try:
            import onnxruntime as ort
            if self.gpu_requested and hasattr(ort, "preload_dlls"):
                try:
                    ort.preload_dlls(directory="")
                except Exception:
                    pass
            self.gpu_available = (
                "CUDAExecutionProvider" in ort.get_available_providers()
            )
        except Exception:
            self.gpu_available = False

        # RapidOCR is intentionally loaded lazily. On the supplied game font,
        # the template engine handles normal numbers in a few milliseconds and
        # only uncertain cells need the heavier neural OCR fallback.
        if self.winocr is None and self.game_digits is None:
            self._ensure_rapid()
            if self.rapid is None:
                raise RuntimeError(
                    "OCR 구성요소를 불러오지 못했습니다.\n"
                    f"게임 숫자 OCR: {self.game_digit_error}\n"
                    f"Windows OCR: {self.winocr_error}\n"
                    f"RapidOCR: {self.rapid_error}"
                )
    def _ensure_rapid(self):
        if self.rapid_initialized:
            return
        self.rapid_initialized = True

        cuda_available = False
        try:
            import onnxruntime as ort
            if self.gpu_requested and hasattr(ort, "preload_dlls"):
                try:
                    ort.preload_dlls(directory="")
                except Exception:
                    pass
            cuda_available = self.gpu_requested and self.gpu_available
        except Exception:
            cuda_available = False

        try:
            from rapidocr_onnxruntime import RapidOCR
            if cuda_available:
                try:
                    self.rapid = RapidOCR(
                        use_gpu=True,
                        det_use_cuda=True,
                        rec_use_cuda=True,
                        cls_use_cuda=True,
                    )
                    self.gpu_active = True
                except Exception:
                    # Some RapidOCR releases do not accept every legacy CUDA
                    # option. Keep OCR usable instead of failing startup.
                    self.rapid = RapidOCR()
                    self.gpu_active = False
            else:
                self.rapid = RapidOCR()
        except Exception as exc:
            self.rapid_error = str(exc)
            self.rapid = None
            self.gpu_active = False

    def engine_summary(self) -> str:
        number_engine = (
            "게임 전용 숫자 OCR"
            if self.game_digits is not None
            else "신경망 숫자 OCR"
        )
        if self.gpu_requested:
            fallback = (
                "RTX CUDA 보조"
                if self.gpu_available
                else "RTX 런타임 없음·CPU 대체"
            )
        else:
            fallback = "CPU 보조"
        return f"{number_engine} · {fallback} · 상품명 Windows OCR"

    @staticmethod
    def _to_pil(image_bgr: np.ndarray) -> Image.Image:
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb)

    @staticmethod
    def _resize_sharpen(image_bgr: np.ndarray, scale: float):
        resized = cv2.resize(
            image_bgr, None, fx=scale, fy=scale,
            interpolation=cv2.INTER_CUBIC
        )
        blur = cv2.GaussianBlur(resized, (0, 0), 1.0)
        return cv2.addWeighted(resized, 1.65, blur, -0.65, 0)

    @classmethod
    def _number_variants(cls, image_bgr: np.ndarray, scale: float = 3.0):
        sharpened = cls._resize_sharpen(image_bgr, scale)

        gray = cv2.cvtColor(sharpened, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(6, 6)).apply(gray)
        clahe_bgr = cv2.cvtColor(clahe, cv2.COLOR_GRAY2BGR)

        _, binary = cv2.threshold(
            clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        binary_bgr = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)

        return [
            (sharpened, scale),
            (clahe_bgr, scale),
            (binary_bgr, scale),
        ]

    def _winocr_text(self, image_bgr: np.ndarray) -> str:
        if self.winocr is None:
            return ""
        pil = self._to_pil(image_bgr)
        try:
            result = self.winocr.recognize_pil_sync(pil, "ko")
        except Exception:
            try:
                result = self.winocr.recognize_pil_sync(pil)
            except Exception:
                return ""

        if isinstance(result, dict):
            return str(result.get("text", ""))
        return str(getattr(result, "text", ""))

    def _rapid_items(self, image_bgr: np.ndarray):
        if self.rapid is None:
            self._ensure_rapid()
        if self.rapid is None:
            return []
        try:
            raw_result = self.rapid(image_bgr)
        except Exception:
            return []

        # rapidocr-onnxruntime returns (result, elapsed). Newer wrappers may
        # return an object or a list directly.
        if isinstance(raw_result, tuple) and len(raw_result) >= 1:
            result = raw_result[0]
        else:
            result = raw_result
        if hasattr(result, "txts") and hasattr(result, "boxes"):
            items = []
            scores = getattr(result, "scores", None)
            if scores is None:
                scores = [0.0] * len(result.txts)
            for box, value, score in zip(result.boxes, result.txts, scores):
                items.append((box, str(value), float(score)))
            return items
        if not result:
            return []
        items = []
        for item in result:
            if len(item) < 2:
                continue
            box = item[0]
            value = str(item[1])
            score = float(item[2]) if len(item) >= 3 else 0.0
            items.append((box, value, score))
        return items

    def _rapid_text(self, image_bgr: np.ndarray) -> str:
        return " ".join(value for _box, value, _score in self._rapid_items(image_bgr))

    @staticmethod
    def _normalize_candidate(raw_digits: str, max_value: int) -> Optional[int]:
        if not raw_digits:
            return None
        raw_digits = raw_digits.lstrip("0") or "0"
        try:
            value = int(raw_digits)
        except Exception:
            return None
        if value <= max_value:
            return value

        # OCR 아이콘이 앞뒤에 붙었을 때만 제한적으로 잘라본다.
        candidates = []
        for cut in range(1, min(2, len(raw_digits) - 1) + 1):
            suffix = int(raw_digits[cut:])
            prefix = int(raw_digits[:-cut])
            if suffix <= max_value:
                candidates.append((len(str(suffix)), suffix))
            if prefix <= max_value:
                candidates.append((len(str(prefix)), prefix))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    @classmethod
    def _numbers_from_text(cls, text: str, max_value: int):
        values = []
        for digits in re.findall(r"\d+", text):
            value = cls._normalize_candidate(digits, max_value)
            if value is not None:
                values.append(value)
        return values

    @staticmethod
    def _clean_name_candidate(text: str) -> list[str]:
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        output = []
        for line in lines:
            line = re.sub(
                r"^[^0-9A-Za-z가-힣]+|[^0-9A-Za-z가-힣]+$", "", line
            ).strip()
            if not line:
                continue
            if line in {"보유 수량", "무역품 분류", "무역품 시세"}:
                continue
            output.append(line)
        return output

    @staticmethod
    def _name_score(value: str) -> float:
        hangul = len(re.findall(r"[가-힣]", value))
        alnum = len(re.findall(r"[0-9A-Za-z가-힣]", value))
        replacement_like = len(re.findall(r"[^0-9A-Za-z가-힣 ]", value))
        return hangul * 3.0 + alnum - replacement_like * 2.0

    @staticmethod
    def _normalize_name_for_match(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z가-힣]", "", value).lower()

    def _correct_name_from_history(
        self,
        candidate: str,
        known_names: Optional[list[str]],
    ) -> str:
        if not candidate or not known_names:
            return candidate
        normalized = self._normalize_name_for_match(candidate)
        if not normalized:
            return candidate

        best_name = candidate
        best_ratio = 0.0
        for known in known_names:
            if not known or re.fullmatch(r"무역품 \d+", known):
                continue
            known_normalized = self._normalize_name_for_match(known)
            if not known_normalized:
                continue
            ratio = difflib.SequenceMatcher(
                None, normalized, known_normalized
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_name = known

        # Correct only reasonably close OCR. This lets manual name corrections
        # become a reusable dictionary without forcing unrelated new goods to
        # an old name.
        if best_ratio >= 0.64:
            return best_name
        return candidate

    def read_name(
        self,
        image_bgr: np.ndarray,
        known_names: Optional[list[str]] = None,
    ) -> str:
        candidates: list[str] = []

        # Fast path: one Windows OCR pass on the original title crop.
        candidates.extend(
            self._clean_name_candidate(self._winocr_text(image_bgr))
        )
        best_initial = max(
            (self._name_score(value) for value in candidates),
            default=0.0,
        )

        # Only weak title reads receive extra preprocessing passes. This keeps
        # normal batch scans fast while still helping outlined/stylized names.
        if best_initial < 9:
            try:
                enlarged = self._resize_sharpen(image_bgr, 2.4)
                candidates.extend(
                    self._clean_name_candidate(self._winocr_text(enlarged))
                )
                current_best = max(
                    (self._name_score(value) for value in candidates),
                    default=0.0,
                )
                if current_best < 9:
                    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
                    clahe = cv2.createCLAHE(
                        clipLimit=2.0, tileGridSize=(6, 6)
                    ).apply(gray)
                    candidates.extend(
                        self._clean_name_candidate(
                            self._winocr_text(
                                cv2.cvtColor(clahe, cv2.COLOR_GRAY2BGR)
                            )
                        )
                    )
            except Exception:
                pass

        # Neural OCR is a fallback for empty/very weak Windows OCR only. This
        # avoids loading its models during normal successful scans.
        if not candidates or max(self._name_score(x) for x in candidates) < 6:
            candidates.extend(
                self._clean_name_candidate(self._rapid_text(image_bgr))
            )
        if not candidates:
            return ""

        counts = {}
        for value in candidates:
            counts[value] = counts.get(value, 0) + 1
        result = max(
            candidates,
            key=lambda value: (
                counts[value],
                self._name_score(value),
                len(value),
            ),
        )
        return self._correct_name_from_history(result, known_names).strip()

    @staticmethod
    def _number_roi(cell: np.ndarray, is_price: bool):
        """
        가격 오른쪽의 상승/하락 화살표를 잘라낸다.
        OCR 영역을 화살표까지 넓게 잡아도 숫자 부분만 사용한다.
        """
        h, w = cell.shape[:2]
        x1 = int(w * 0.04)
        x2 = int(w * (0.78 if is_price else 0.96))
        y1 = int(h * 0.08)
        y2 = int(h * 0.92)
        return cell[y1:y2, x1:x2]

    @staticmethod
    def _detect_dash(cell: np.ndarray) -> bool:
        """
        게임의 '-'는 흰색이 아니라 붉은 회색이라 밝기 임계값만으로는 놓친다.
        셀 배경과 다른 중앙의 짧은 가로형 도형을 검출한다.
        """
        h, w = cell.shape[:2]
        roi = cell[int(h * 0.15):int(h * 0.85), int(w * 0.05):int(w * 0.95)]
        if roi.size == 0:
            return False

        edge_h = max(1, int(roi.shape[0] * 0.20))
        edge_w = max(1, int(roi.shape[1] * 0.12))
        samples = np.concatenate([
            roi[:edge_h, :].reshape(-1, 3),
            roi[-edge_h:, :].reshape(-1, 3),
            roi[:, :edge_w].reshape(-1, 3),
            roi[:, -edge_w:].reshape(-1, 3),
        ])
        background = np.median(samples, axis=0)
        distance = np.linalg.norm(
            roi.astype(np.float32) - background.astype(np.float32),
            axis=2
        )
        mask = (distance > 18).astype(np.uint8) * 255
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, np.ones((3, 5), np.uint8)
        )

        count, _labels, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
        components = []
        for index in range(1, count):
            x, y, cw, ch, area = stats[index]
            if area < 8:
                continue
            cx, cy = centers[index]
            components.append((x, y, cw, ch, area, cx, cy))

        significant = [component for component in components if component[4] >= 10]
        matching = [
            component for component in significant
            if 0.25 * roi.shape[1] <= component[5] <= 0.75 * roi.shape[1]
            and 0.20 * roi.shape[0] <= component[6] <= 0.80 * roi.shape[0]
            and component[2] >= 5
            and component[2] <= 0.35 * roi.shape[1]
            and component[3] <= 0.55 * roi.shape[0]
            and component[2] / max(1, component[3]) >= 1.5
        ]
        return bool(matching) and len(significant) <= 2

    @classmethod
    def _visual_info(cls, cell: np.ndarray, is_price: bool):
        """
        글자 모양을 직접 판독하지 않고 숫자 개수와 쉼표 위치만 얻는다.
        이 정보로 1,283을 283으로 읽은 경우를 재검사한다.
        """
        roi = cls._number_roi(cell, is_price)
        if roi.size == 0:
            return 0, None

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        bright = (hsv[:, :, 2] > 140).astype(np.uint8) * 255
        count, _labels, stats, centers = cv2.connectedComponentsWithStats(bright, 8)
        height, width = bright.shape

        digit_components = []
        commas = []
        for index in range(1, count):
            x, y, cw, ch, area = stats[index]
            cx, cy = centers[index]

            if (
                2 <= area <= 35
                and cw <= 5
                and ch <= 7
                and cy > height * 0.55
                and width * 0.10 < cx < width * 0.90
            ):
                commas.append((cx, cy, area))

            if (
                area >= 18
                and ch >= height * 0.30
                and ch <= height * 0.85
                and cw >= 2
                and cw <= width * 0.25
                and height * 0.15 < cy < height * 0.78
            ):
                digit_components.append((x, y, cw, ch, area, cx, cy))

        comma_x = min((item[0] for item in commas), default=None)
        return len(digit_components), comma_x

    def _collect_cell_candidates(
        self,
        cell: np.ndarray,
        max_value: int,
        is_price: bool,
    ):
        roi = self._number_roi(cell, is_price)
        candidates = []

        for variant_index, (variant, _scale) in enumerate(
            self._number_variants(roi, 3.2)
        ):
            for _box, raw_text, score in self._rapid_items(variant):
                for value in self._numbers_from_text(raw_text, max_value):
                    candidates.append((value, float(score), variant_index))

        # RapidOCR 후보가 약한 경우 Windows OCR 후보도 합친다.
        for value in self._numbers_from_text(self._winocr_text(roi), max_value):
            candidates.append((value, 0.55, 9))
        return candidates

    def _read_leading_digit(self, cell: np.ndarray, comma_x: float) -> Optional[int]:
        roi = self._number_roi(cell, True)
        cut = max(2, min(roi.shape[1], int(round(comma_x))))
        left = roi[:, :cut]
        if left.size == 0:
            return None

        candidates = []
        for variant, _scale in self._number_variants(left, 4.0):
            for _box, raw_text, score in self._rapid_items(variant):
                for value in self._numbers_from_text(raw_text, 9):
                    if 1 <= value <= 9:
                        candidates.append((score, value))
        for value in self._numbers_from_text(self._winocr_text(left), 9):
            if 1 <= value <= 9:
                candidates.append((0.55, value))

        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]

        # 고립된 '1'은 OCR에서 자주 빠진다. 세로로 긴 좁은 성분이면 1로 복구한다.
        hsv = cv2.cvtColor(left, cv2.COLOR_BGR2HSV)
        bright = (hsv[:, :, 2] > 140).astype(np.uint8) * 255
        count, _labels, stats, _centers = cv2.connectedComponentsWithStats(bright, 8)
        shapes = []
        for index in range(1, count):
            x, y, cw, ch, area = stats[index]
            if area >= 15 and ch >= bright.shape[0] * 0.30:
                shapes.append((area, cw, ch))
        if shapes:
            _area, cw, ch = max(shapes)
            if cw / max(1, ch) <= 0.65:
                return 1
        return None

    @staticmethod
    def _choose_candidate(
        candidates,
        digit_count: int,
        comma_x: Optional[float],
    ) -> Optional[int]:
        if not candidates:
            return None

        grouped = {}
        for value, score, variant_index in candidates:
            data = grouped.setdefault(value, {"votes": 0, "score": 0.0, "variants": set()})
            data["votes"] += 1
            data["score"] = max(data["score"], score)
            data["variants"].add(variant_index)

        ranked = []
        for value, data in grouped.items():
            digits = len(str(abs(value)))
            consistency = 0
            if digit_count:
                consistency += 5 if digits == digit_count else -abs(digits - digit_count) * 2
            if comma_x is not None:
                consistency += 4 if value >= 1000 else -3
            else:
                consistency += 2 if value < 1000 else 0

            ranked.append((
                consistency,
                len(data["variants"]),
                data["votes"],
                data["score"],
                digits,
                value,
            ))

        ranked.sort(reverse=True)
        return ranked[0][-1]

    def _read_cell_precise(
        self,
        cell: np.ndarray,
        max_value: int,
        is_price: bool,
        digit_count: int,
        comma_x: Optional[float],
    ) -> OCRCellResult:
        candidates = self._collect_cell_candidates(cell, max_value, is_price)
        value = self._choose_candidate(candidates, digit_count, comma_x)

        # 쉼표가 보이는데 3자리 이하라면 앞자리와 뒤 3자리를 다시 결합한다.
        if is_price and comma_x is not None and (value is None or value < 1000):
            leading = self._read_leading_digit(cell, comma_x)
            if leading is not None:
                suffix = 0 if value is None else value % 1000
                rebuilt = leading * 1000 + suffix
                if rebuilt <= max_value:
                    value = rebuilt

        review = value is None
        if value is not None and digit_count:
            if len(str(abs(value))) != digit_count:
                review = True
        if is_price and comma_x is not None and (value is None or value < 1000):
            review = True
        if is_price and value is not None and value < 100:
            review = True

        return OCRCellResult(value=value, unavailable=False, review=review)

    def read_market_grid(
        self,
        full_image: np.ndarray,
        regions: dict,
        rows: int,
        price_max: int,
        stock_max: int,
    ) -> dict[str, list[OCRCellResult]]:
        keys = ["buy_column", "sell_column", "stock_column"]
        rects = [regions[key] for key in keys]
        output = {key: [OCRCellResult() for _ in range(rows)] for key in keys}
        unresolved: list[tuple[int, int, np.ndarray, bool, int]] = []
        stats = {"fast": 0, "fallback": 0, "dash": 0}

        # First pass: fixed game-font template recognition. It does not run
        # text detection and typically resolves all 18 numeric cells directly.
        for column, key in enumerate(keys):
            rect = rects[column]
            is_price = column != 2
            max_value = stock_max if column == 2 else price_max
            for row in range(rows):
                y1 = round(rect[1] + (rect[3] - rect[1]) * row / rows)
                y2 = round(rect[1] + (rect[3] - rect[1]) * (row + 1) / rows)
                cell = full_image[y1:y2, rect[0]:rect[2]]

                if self._detect_dash(cell):
                    output[key][row] = OCRCellResult(
                        value=None, unavailable=True, review=False
                    )
                    stats["dash"] += 1
                    continue

                if self.game_digits is not None:
                    fast = self.game_digits.read_cell(
                        cell, is_price=is_price, max_value=max_value
                    )
                    if fast.value is not None and fast.confidence >= 0.74:
                        output[key][row] = OCRCellResult(
                            value=fast.value,
                            unavailable=False,
                            review=fast.confidence < 0.80,
                        )
                        stats["fast"] += 1
                        continue

                unresolved.append((column, row, cell, is_price, max_value))

        if not unresolved:
            self.last_grid_stats = stats
            return output

        # Fallback pass: run one full-table neural OCR and then precision OCR
        # only on cells that the game-font engine could not resolve.
        ux1 = min(rect[0] for rect in rects)
        uy1 = min(rect[1] for rect in rects)
        ux2 = max(rect[2] for rect in rects)
        uy2 = max(rect[3] for rect in rects)
        union = full_image[uy1:uy2, ux1:ux2]
        cell_candidates = {
            (column, row): [] for column in range(3) for row in range(rows)
        }

        # One sharpened table pass is usually enough because only uncertain
        # cells reach this stage. Cell-level variants are used afterward when
        # necessary.
        prepared, scale = self._number_variants(union, 2.35)[0]
        for box, raw_text, score in self._rapid_items(prepared):
            try:
                points = np.array(box, dtype=float)
                cx = float(points[:, 0].mean()) / scale + ux1
                cy = float(points[:, 1].mean()) / scale + uy1
            except Exception:
                continue
            column = None
            for candidate_column, rect in enumerate(rects):
                if rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]:
                    column = candidate_column
                    break
            if column is None:
                continue
            rect = rects[column]
            relative = (cy - rect[1]) / max(1, rect[3] - rect[1])
            row = min(rows - 1, max(0, int(relative * rows)))
            max_value = stock_max if column == 2 else price_max
            for value in self._numbers_from_text(raw_text, max_value):
                cell_candidates[(column, row)].append(
                    (value, float(score), 0)
                )

        for column, row, cell, is_price, max_value in unresolved:
            key = keys[column]
            digit_count, comma_x = self._visual_info(cell, is_price)
            value = self._choose_candidate(
                cell_candidates[(column, row)], digit_count, comma_x
            )
            suspicious = (
                value is None
                or (is_price and value < 100)
                or (
                    digit_count > 0
                    and value is not None
                    and len(str(abs(value))) != digit_count
                )
                or (
                    is_price
                    and comma_x is not None
                    and (value is None or value < 1000)
                )
            )
            if suspicious:
                output[key][row] = self._read_cell_precise(
                    cell, max_value, is_price, digit_count, comma_x
                )
            else:
                output[key][row] = OCRCellResult(
                    value=value, unavailable=False, review=False
                )
            stats["fallback"] += 1

        self.last_grid_stats = stats
        return output

class RectangleSelector(tk.Toplevel):
    def __init__(
        self,
        parent,
        screenshot: Image.Image,
        labels: list[str],
        callback: Callable[[Optional[list[list[int]]]], None],
    ):
        super().__init__(parent)
        self.callback = callback
        self.labels = labels
        self.index = 0
        self.rectangles: list[list[int]] = []
        self.start = None
        self.temp_rect = None

        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.configure(cursor="crosshair")
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.photo = ImageTk.PhotoImage(screenshot)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.overlay_bg = self.canvas.create_rectangle(0, 0, screenshot.width, 55, fill="black", stipple="gray50")
        self.info = self.canvas.create_text(
            screenshot.width // 2,
            27,
            text="",
            fill="white",
            font=("맑은 고딕", 14, "bold"),
        )
        self._update_info()

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda _e: self._finish(None))

    def _update_info(self):
        label = self.labels[self.index]
        self.canvas.itemconfigure(
            self.info,
            text=f"{self.index + 1}/{len(self.labels)}  {label} 영역을 드래그하세요.  Esc: 취소",
        )

    def _press(self, event):
        self.start = (event.x, event.y)
        if self.temp_rect is not None:
            self.canvas.delete(self.temp_rect)
        self.temp_rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#ff3030", width=3
        )

    def _drag(self, event):
        if self.start and self.temp_rect is not None:
            self.canvas.coords(self.temp_rect, self.start[0], self.start[1], event.x, event.y)

    def _release(self, event):
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = event.x, event.y
        x1, x2 = sorted((max(0, x1), max(0, x2)))
        y1, y2 = sorted((max(0, y1), max(0, y2)))
        if x2 - x1 < 8 or y2 - y1 < 8:
            self.canvas.delete(self.temp_rect)
            self.temp_rect = None
            self.start = None
            return

        self.rectangles.append([x1, y1, x2, y2])
        self.canvas.itemconfigure(self.temp_rect, outline="#00ff70")
        self.canvas.create_text(
            x1 + 6,
            y1 + 6,
            text=self.labels[self.index],
            fill="yellow",
            anchor="nw",
            font=("맑은 고딕", 10, "bold"),
        )
        self.temp_rect = None
        self.start = None
        self.index += 1

        if self.index >= len(self.labels):
            self.after(250, lambda: self._finish(self.rectangles))
        else:
            self._update_info()

    def _finish(self, result):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        self.callback(result)

class PointSelector(tk.Toplevel):
    def __init__(
        self,
        parent,
        screenshot: Image.Image,
        title_text: str,
        callback: Callable[[Optional[list[list[int]]]], None],
        fixed_prompts: Optional[list[str]] = None,
        existing: Optional[list[list[int]]] = None,
    ):
        super().__init__(parent)
        self.callback = callback
        self.fixed_prompts = fixed_prompts
        self.points = [list(p) for p in (existing or [])]

        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.configure(cursor="crosshair")
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.photo = ImageTk.PhotoImage(screenshot)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.canvas.create_rectangle(0, 0, screenshot.width, 70, fill="black", stipple="gray50")
        self.info = self.canvas.create_text(
            screenshot.width // 2,
            34,
            text="",
            fill="white",
            font=("맑은 고딕", 13, "bold"),
        )
        self.title_text = title_text
        self._redraw()
        self._update_info()

        self.canvas.bind("<Button-1>", self._add)
        self.canvas.bind("<Button-3>", self._undo)
        self.bind("<Return>", self._enter)
        self.bind("<Escape>", lambda _e: self._finish(None))

    def _update_info(self):
        if self.fixed_prompts:
            if len(self.points) < len(self.fixed_prompts):
                task = self.fixed_prompts[len(self.points)]
                text = (
                    f"{self.title_text} | {len(self.points) + 1}/{len(self.fixed_prompts)}: {task}\n"
                    "왼쪽 클릭: 등록 · 오른쪽 클릭: 되돌리기 · Esc: 취소"
                )
            else:
                text = "등록 완료"
        else:
            text = (
                f"{self.title_text} | 현재 {len(self.points)}개\n"
                "왼쪽 클릭: 상품 추가 · 오른쪽 클릭: 마지막 취소 · Enter: 저장 · Esc: 취소"
            )
        self.canvas.itemconfigure(self.info, text=text)

    def _redraw(self):
        self.canvas.delete("point_mark")
        for idx, (x, y) in enumerate(self.points, 1):
            self.canvas.create_oval(
                x - 10, y - 10, x + 10, y + 10,
                fill="#00ff70", outline="black", width=2, tags="point_mark"
            )
            self.canvas.create_text(
                x, y, text=str(idx), fill="black",
                font=("Arial", 9, "bold"), tags="point_mark"
            )

    def _add(self, event):
        if self.fixed_prompts and len(self.points) >= len(self.fixed_prompts):
            return
        self.points.append([event.x, event.y])
        self._redraw()
        self._update_info()
        if self.fixed_prompts and len(self.points) == len(self.fixed_prompts):
            self.after(300, lambda: self._finish(self.points))

    def _undo(self, _event):
        if self.points:
            self.points.pop()
            self._redraw()
            self._update_info()

    def _enter(self, _event):
        if self.fixed_prompts:
            return
        self._finish(self.points)

    def _finish(self, result):
        self.destroy()
        self.callback(result)

class TradePlannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("의지의 바다 무역 OCR 동선 계산기 v19")
        self.geometry("1450x920")
        self.minsize(1160, 720)

        self.config_data = load_json(CONFIG_PATH, DEFAULT_CONFIG)
        self._migrate_map_config()
        raw_goods = load_json(DATA_PATH, {"goods": []}).get("goods", [])
        self.goods: list[Good] = [Good.from_dict(x) for x in raw_goods]

        saved_required = self.config_data.get("required_islands", [])
        self.required_islands = {
            island for island in saved_required if island in ISLANDS
        }

        saved_forbidden = self.config_data.get("forbidden_islands", [])
        self.forbidden_islands = {
            island for island in saved_forbidden if island in ISLANDS
        }

        # 같은 섬이 필수 방문과 방문 금지에 동시에 저장되어 있으면 필수 방문을 우선한다.
        overlap = self.required_islands & self.forbidden_islands
        if overlap:
            self.forbidden_islands -= overlap
            self.config_data["forbidden_islands"] = [
                island for island in ISLANDS
                if island in self.forbidden_islands
            ]
            save_json(CONFIG_PATH, self.config_data)

        self.required_island_vars: dict[str, tk.BooleanVar] = {}
        self.forbidden_island_vars: dict[str, tk.BooleanVar] = {}

        self.ocr: Optional[OCRReader] = None
        self.batch_running = False
        self.cell_editor = None
        self.batch_errors: list[str] = []

        self._build_ui()
        self.refresh_table()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="보유 코인").grid(row=0, column=0, padx=3)
        self.cash_var = tk.StringVar(value="15000")
        ttk.Entry(top, textvariable=self.cash_var, width=11).grid(row=0, column=1, padx=3)

        ttk.Label(top, text="남은 턴").grid(row=0, column=2, padx=3)
        self.turn_var = tk.StringVar(value="15")
        ttk.Entry(top, textvariable=self.turn_var, width=6).grid(row=0, column=3, padx=3)

        ttk.Label(top, text="내구도").grid(row=0, column=4, padx=3)
        self.durability_var = tk.StringVar(value="4800")
        ttk.Entry(top, textvariable=self.durability_var, width=8).grid(row=0, column=5, padx=3)

        ttk.Label(top, text="현재 위치").grid(row=0, column=6, padx=3)
        self.start_island_var = tk.StringVar(value=ISLANDS[1])
        ttk.Combobox(
            top, textvariable=self.start_island_var, values=ISLANDS,
            state="readonly", width=15
        ).grid(row=0, column=7, padx=3)

        ttk.Button(top, text="최적 동선 계산", command=self.calculate_routes).grid(
            row=0, column=8, padx=10
        )

        ttk.Label(top, text="최대 내구도").grid(row=1, column=0, padx=3, pady=(6, 0))
        self.max_durability_var = tk.StringVar(
            value=str(self.config_data.get("max_durability", 4800))
        )
        ttk.Entry(top, textvariable=self.max_durability_var, width=9).grid(
            row=1, column=1, padx=3, pady=(6, 0)
        )

        ttk.Label(top, text="계산 기준").grid(row=1, column=2, padx=3, pady=(6, 0))
        saved_objective = self.config_data.get(
            "route_objective", "이번 회차 코인 최대"
        )
        if saved_objective == "균형형":
            saved_objective = "자동 균형"
        self.objective_var = tk.StringVar(value=saved_objective)
        ttk.Combobox(
            top,
            textvariable=self.objective_var,
            values=["이번 회차 코인 최대", "자동 균형", "종료 내구도 우선"],
            state="readonly",
            width=18,
        ).grid(row=1, column=3, padx=3, pady=(6, 0))

        ttk.Label(top, text="내구도 가치").grid(row=1, column=4, padx=3, pady=(6, 0))
        self.auto_balance_hint_var = tk.StringVar(value="시장·잔여량으로 자동 산정")
        ttk.Label(
            top,
            textvariable=self.auto_balance_hint_var,
            width=23,
            anchor="w",
        ).grid(row=1, column=5, padx=3, pady=(6, 0), sticky="w")

        self.hex_map_enabled_var = tk.BooleanVar(
            value=bool(normalize_hex_map(self.config_data.get("hex_map"))["enabled"])
        )
        ttk.Checkbutton(
            top,
            text="육각 맵 경로 사용",
            variable=self.hex_map_enabled_var,
            command=self._on_hex_map_enabled_changed,
        ).grid(row=1, column=6, columnspan=2, padx=8, pady=(6, 0), sticky="w")
        ttk.Button(top, text="육각 맵 편집", command=self.open_hex_map_editor).grid(
            row=1, column=8, padx=10, pady=(6, 0)
        )

        route_filter_frame = ttk.Labelframe(
            self,
            text="방문 조건",
            padding=(8, 5),
        )
        route_filter_frame.pack(fill="x", padx=8, pady=(0, 4))

        required_row = ttk.Frame(route_filter_frame)
        required_row.pack(fill="x", pady=2)

        ttk.Label(
            required_row,
            text="꼭 방문:",
            width=10,
            anchor="w",
        ).pack(side="left")

        for island in ISLANDS:
            variable = tk.BooleanVar(value=island in self.required_islands)
            self.required_island_vars[island] = variable
            ttk.Checkbutton(
                required_row,
                text=island.replace("들의 섬", ""),
                variable=variable,
                command=lambda name=island: self._on_required_island_toggled(name),
            ).pack(side="left", padx=3)

        ttk.Button(
            required_row,
            text="필수 전체 해제",
            command=self.clear_required_islands,
        ).pack(side="right", padx=3)

        forbidden_row = ttk.Frame(route_filter_frame)
        forbidden_row.pack(fill="x", pady=2)

        ttk.Label(
            forbidden_row,
            text="방문 금지:",
            width=10,
            anchor="w",
        ).pack(side="left")

        for island in ISLANDS:
            variable = tk.BooleanVar(value=island in self.forbidden_islands)
            self.forbidden_island_vars[island] = variable
            ttk.Checkbutton(
                forbidden_row,
                text=island.replace("들의 섬", ""),
                variable=variable,
                command=lambda name=island: self._on_forbidden_island_toggled(name),
            ).pack(side="left", padx=3)

        ttk.Button(
            forbidden_row,
            text="금지 전체 해제",
            command=self.clear_forbidden_islands,
        ).pack(side="right", padx=3)

        ttk.Label(
            route_filter_frame,
            text=(
                "필수 섬은 모두 방문하고, 방문 금지 섬은 경로에 한 번도 포함하지 않습니다. "
                "같은 섬을 양쪽에 동시에 선택할 수 없습니다."
            ),
            padding=(0, 4, 0, 0),
        ).pack(fill="x")

        self.required_summary_var = tk.StringVar()
        ttk.Label(
            self,
            textvariable=self.required_summary_var,
            padding=(10, 0, 10, 3),
        ).pack(fill="x")
        self._update_required_summary()

        scan = ttk.Frame(self, padding=(8, 0, 8, 4))
        scan.pack(fill="x")
        ttk.Button(
            scan, text="샘플 이미지로 OCR 영역 지정",
            command=self.select_regions_from_image
        ).pack(side="left", padx=3)
        ttk.Separator(scan, orient="vertical").pack(side="left", fill="y", padx=8)

        self.batch_button = ttk.Button(
            scan, text="스크린샷 일괄 OCR (1~18장)",
            command=self.start_batch_ocr
        )
        self.batch_button.pack(side="left", padx=3)
        self.replace_batch_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            scan, text="기존 목록 교체", variable=self.replace_batch_var
        ).pack(side="left", padx=5)

        ttk.Label(scan, text="가격 상한").pack(side="left", padx=(10, 2))
        self.price_max_var = tk.StringVar(
            value=str(self.config_data.get("ocr_price_max", 9999))
        )
        ttk.Entry(scan, textvariable=self.price_max_var, width=7).pack(side="left", padx=(0, 3))

        self.gpu_ocr_var = tk.BooleanVar(
            value=bool(self.config_data.get("ocr_gpu_enabled", False))
        )
        ttk.Checkbutton(
            scan,
            text="RTX 보조 OCR(오류칸만)",
            variable=self.gpu_ocr_var,
            command=self._on_gpu_ocr_changed,
        ).pack(side="left", padx=(8, 3))

        self.status_var = tk.StringVar(
            value="같은 해상도의 무역품 스크린샷을 여러 장 선택하면 순서대로 한 번에 OCR합니다."
        )
        ttk.Label(self, textvariable=self.status_var, padding=(10, 3)).pack(fill="x")

        self.ocr_engine_var = tk.StringVar(
            value="OCR 엔진: 게임 전용 숫자 OCR · 신경망은 오류칸만 사용"
        )
        ttk.Label(
            self,
            textvariable=self.ocr_engine_var,
            padding=(10, 0, 10, 3),
        ).pack(fill="x")

        progress_frame = ttk.Frame(self, padding=(8, 0, 8, 4))
        progress_frame.pack(fill="x")
        self.batch_progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.batch_progress.pack(side="left", fill="x", expand=True)
        self.progress_text_var = tk.StringVar(value="대기")
        ttk.Label(progress_frame, textvariable=self.progress_text_var, width=24).pack(side="left", padx=8)

        controls = ttk.Frame(self, padding=(8, 3))
        controls.pack(fill="x")
        ttk.Button(controls, text="모두 펼치기", command=self.expand_all).pack(side="left", padx=3)
        ttk.Button(controls, text="모두 접기", command=self.collapse_all).pack(side="left", padx=3)
        ttk.Button(controls, text="상품 행 추가", command=self.add_good).pack(side="left", padx=3)
        ttk.Button(controls, text="선택 상품 삭제", command=self.delete_selected).pack(side="left", padx=3)
        ttk.Button(controls, text="원본 이미지 열기", command=self.open_source_image).pack(side="left", padx=3)
        ttk.Button(controls, text="목록 비우기", command=self.clear_scanned_goods).pack(side="left", padx=3)
        ttk.Button(controls, text="저장", command=self.save_data).pack(side="left", padx=3)

        ttk.Button(
            controls, text="고정 거리표 수동 수정", command=self.open_fixed_distance_viewer
        ).pack(side="right", padx=3)
        ttk.Button(
            controls, text="육각 맵 편집", command=self.open_hex_map_editor
        ).pack(side="right", padx=3)
        ttk.Button(
            controls, text="이번 판 섬 배치 설정", command=self.open_island_layout_editor
        ).pack(side="right", padx=3)

        help_text = (
            "사용 칸 클릭: 계산 포함/제외 · 상품명/가격/수량 더블클릭: 바로 수정 · "
            "상품 왼쪽 화살표: 펼치기/접기 · 육각 맵에서는 장애물 우회와 수리 타일을 실제 이동 순서로 계산"
        )
        ttk.Label(self, text=help_text, padding=(10, 2)).pack(fill="x")

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=8, pady=4)

        grid_frame = ttk.Labelframe(
            pane, text="무역품 전체 가격표 — 상품별 6개 섬을 한 화면에서 펼쳐서 직접 수정"
        )
        result_frame = ttk.Labelframe(pane, text="추천 동선")
        pane.add(grid_frame, weight=4)
        pane.add(result_frame, weight=2)

        columns = ("active", "number", "island", "buy", "sell", "stock", "review")
        self.tree = ttk.Treeview(
            grid_frame, columns=columns, show="tree headings", height=22
        )
        self.tree.heading("#0", text="품목")
        self.tree.column("#0", width=300, minwidth=180, anchor="w", stretch=True)

        headings = [
            ("active", "사용", 65),
            ("number", "번호", 55),
            ("island", "섬 이름", 155),
            ("buy", "구매가", 105),
            ("sell", "판매가", 105),
            ("stock", "수량", 85),
            ("review", "검토", 105),
        ]
        for key, title, width in headings:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="center", stretch=False)

        self.tree.tag_configure("inactive", foreground="#888888")
        self.tree.tag_configure("warning", background="#fff4c2")
        self.tree.tag_configure("child", foreground="#222222")

        vertical = ttk.Scrollbar(grid_frame, orient="vertical", command=self.tree.yview)
        vertical.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(grid_frame, orient="horizontal", command=self.tree.xview)
        horizontal.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)

        self.tree.bind("<Button-1>", self.on_tree_click, add="+")
        self.tree.bind("<Double-1>", self.begin_cell_edit)

        self.result = tk.Text(result_frame, wrap="word", font=("맑은 고딕", 10))
        self.result.pack(fill="both", expand=True)
        self.result.insert(
            "1.0",
            "스크린샷으로 등록된 상품만 계산합니다. 사용 체크가 꺼진 상품은 계산에서 제외됩니다.\n"
        )

    def on_close(self):
        self.save_data()
        try:
            self.config_data["max_durability"] = int(
                self.max_durability_var.get().replace(",", "").strip()
            )
            objective = self.objective_var.get()
            if objective == "균형형":
                objective = "자동 균형"
            self.config_data["route_objective"] = objective
            hex_map = normalize_hex_map(self.config_data.get("hex_map"))
            hex_map["enabled"] = bool(self.hex_map_enabled_var.get())
            self.config_data["hex_map"] = hex_map
            self.save_config()
        except Exception:
            pass
        self.destroy()

    def save_data(self):
        save_json(DATA_PATH, {"goods": [g.to_dict() for g in self.goods]})

    def save_config(self):
        save_json(CONFIG_PATH, self.config_data)

    def _save_route_conditions(self):
        self.config_data["required_islands"] = [
            island for island in ISLANDS
            if island in self.required_islands
        ]
        self.config_data["forbidden_islands"] = [
            island for island in ISLANDS
            if island in self.forbidden_islands
        ]
        self.save_config()
        self._update_required_summary()

    def _on_required_island_toggled(self, island):
        if self.required_island_vars[island].get():
            # 필수 방문을 켜면 같은 섬의 방문 금지는 자동 해제한다.
            self.required_islands.add(island)
            self.forbidden_islands.discard(island)
            self.forbidden_island_vars[island].set(False)
        else:
            self.required_islands.discard(island)
        self._save_route_conditions()

    def _on_forbidden_island_toggled(self, island):
        if self.forbidden_island_vars[island].get():
            # 방문 금지를 켜면 같은 섬의 필수 방문은 자동 해제한다.
            self.forbidden_islands.add(island)
            self.required_islands.discard(island)
            self.required_island_vars[island].set(False)
        else:
            self.forbidden_islands.discard(island)
        self._save_route_conditions()

    def clear_required_islands(self):
        for variable in self.required_island_vars.values():
            variable.set(False)
        self.required_islands.clear()
        self._save_route_conditions()

    def clear_forbidden_islands(self):
        for variable in self.forbidden_island_vars.values():
            variable.set(False)
        self.forbidden_islands.clear()
        self._save_route_conditions()

    def _update_required_summary(self):
        if not hasattr(self, "required_summary_var"):
            return

        required = [
            island for island in ISLANDS
            if island in self.required_islands
        ]
        forbidden = [
            island for island in ISLANDS
            if island in self.forbidden_islands
        ]

        required_text = " · ".join(required) if required else "없음"
        forbidden_text = " · ".join(forbidden) if forbidden else "없음"
        self.required_summary_var.set(
            f"필수 방문: {required_text}  |  방문 금지: {forbidden_text}"
        )

    def selected_required_island_indices(self) -> set[int]:
        return {
            ISLANDS.index(island)
            for island in self.required_islands
            if island in ISLANDS
        }

    def selected_forbidden_island_indices(self) -> set[int]:
        return {
            ISLANDS.index(island)
            for island in self.forbidden_islands
            if island in ISLANDS
        }

    def capture_bgr(self) -> np.ndarray:
        monitor_idx = int(self.config_data.get("capture_monitor", 1))
        with mss.mss() as sct:
            monitor_idx = min(max(1, monitor_idx), len(sct.monitors) - 1)
            shot = np.array(sct.grab(sct.monitors[monitor_idx]))
        return cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)

    def capture_pil_hidden(self) -> Image.Image:
        self.withdraw()
        self.update()
        time.sleep(0.35)
        image_bgr = self.capture_bgr()
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb)

    def restore_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def select_regions(self):
        screenshot = self.capture_pil_hidden()
        labels = ["상품명", "구매가 6행 전체", "판매가 6행 전체", "재고량 6행 전체"]

        def done(rectangles):
            self.restore_window()
            if not rectangles:
                return
            keys = ["product_name", "buy_column", "sell_column", "stock_column"]
            for key, rect in zip(keys, rectangles):
                self.config_data["regions"][key] = rect
            self.config_data["region_screen_size"] = [screenshot.width, screenshot.height]
            self.save_config()
            self.status_var.set("OCR 영역 4개를 저장했습니다.")

        RectangleSelector(self, screenshot, labels, done)

    @staticmethod
    def load_image_bgr(path: str) -> np.ndarray:
        data = np.fromfile(path, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"이미지를 열 수 없습니다: {path}")
        return image

    def select_regions_from_image(self):
        path = filedialog.askopenfilename(
            title="OCR 영역을 지정할 샘플 스크린샷 선택",
            filetypes=[
                ("이미지", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("모든 파일", "*.*"),
            ],
        )
        if not path:
            return
        try:
            image_bgr = self.load_image_bgr(path)
            image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
            screenshot = Image.fromarray(image_rgb)
        except Exception as exc:
            messagebox.showerror("이미지 열기 실패", str(exc))
            return

        labels = ["상품명", "구매가 6행 전체", "판매가 6행 전체", "재고량 6행 전체"]

        def done(rectangles):
            self.deiconify()
            self.lift()
            if not rectangles:
                return
            keys = ["product_name", "buy_column", "sell_column", "stock_column"]
            for key, rect in zip(keys, rectangles):
                self.config_data["regions"][key] = rect
            self.config_data["region_screen_size"] = [screenshot.width, screenshot.height]
            self.save_config()
            self.status_var.set(
                f"샘플 이미지 {screenshot.width}×{screenshot.height} 기준 OCR 영역을 저장했습니다."
            )

        self.withdraw()
        self.update()
        RectangleSelector(self, screenshot, labels, done)

    def scaled_regions_for_image(self, image: np.ndarray) -> dict:
        regions = self.config_data.get("regions", {})
        if not all(regions.get(k) for k in ("product_name", "buy_column", "sell_column", "stock_column")):
            raise RuntimeError("OCR 영역 4개가 지정되지 않았습니다.")

        h, w = image.shape[:2]
        base_size = self.config_data.get("region_screen_size") or [w, h]
        base_w, base_h = max(1, int(base_size[0])), max(1, int(base_size[1]))
        sx, sy = w / base_w, h / base_h

        scaled = {}
        for key, rect in regions.items():
            if rect is None:
                scaled[key] = None
                continue
            x1, y1, x2, y2 = rect
            scaled[key] = [
                round(x1 * sx), round(y1 * sy),
                round(x2 * sx), round(y2 * sy),
            ]
        return scaled

    def _on_gpu_ocr_changed(self):
        enabled = bool(self.gpu_ocr_var.get())
        self.config_data["ocr_gpu_enabled"] = enabled
        self.save_config()
        self.ocr = None
        if enabled:
            self.status_var.set(
                "RTX 보조 OCR을 요청했습니다. GPU_OCR_SETUP.cmd를 실행한 환경이면 "
                "불확실한 셀의 신경망 재검사에 CUDA를 사용합니다."
            )
        else:
            self.status_var.set(
                "게임 전용 숫자 OCR을 기본으로 사용하고 오류칸만 CPU로 재검사합니다."
            )

    def ensure_ocr(self):
        if self.ocr is None:
            self.status_var.set("OCR 엔진을 처음 불러오는 중입니다...")
            self.update_idletasks()
            self.ocr = OCRReader(
                gpu_requested=bool(self.config_data.get("ocr_gpu_enabled", False))
            )
            if hasattr(self, "ocr_engine_var"):
                self.ocr_engine_var.set("OCR 엔진: " + self.ocr.engine_summary())

    @staticmethod
    def crop(image: np.ndarray, rect) -> np.ndarray:
        x1, y1, x2, y2 = [int(v) for v in rect]
        h, w = image.shape[:2]
        x1, x2 = max(0, x1), min(w, x2)
        y1, y2 = max(0, y1), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            raise ValueError("잘못된 OCR 영역입니다.")
        return image[y1:y2, x1:x2]

    def read_product_from_image(
        self,
        image: np.ndarray,
        price_max: Optional[int] = None,
    ):
        regions = self.scaled_regions_for_image(image)
        if price_max is None:
            try:
                price_max = int(self.price_max_var.get().replace(",", "").strip())
                if price_max < 100:
                    raise ValueError
            except Exception:
                price_max = 9999
                self.price_max_var.set(str(price_max))

        name = self.ocr.read_name(
            self.crop(image, regions["product_name"]),
            known_names=[good.name for good in self.goods],
        )
        columns = self.ocr.read_market_grid(
            image,
            regions,
            rows=6,
            price_max=price_max,
            stock_max=int(self.config_data.get("ocr_stock_max", 99)),
        )

        markets = []
        for row, island in enumerate(ISLANDS):
            buy_cell = columns["buy_column"][row]
            sell_cell = columns["sell_column"][row]
            stock_cell = columns["stock_column"][row]
            markets.append(
                Market(
                    buy=buy_cell.value,
                    sell=sell_cell.value,
                    stock=stock_cell.value,
                    buy_unavailable=buy_cell.unavailable,
                    sell_unavailable=sell_cell.unavailable,
                    stock_unavailable=stock_cell.unavailable,
                    buy_review=buy_cell.review,
                    sell_review=sell_cell.review,
                    stock_review=stock_cell.review,
                )
            )
        return name, markets

    def read_current_product(self, image: np.ndarray):
        try:
            price_max = int(self.price_max_var.get().replace(",", "").strip())
            if price_max < 100:
                raise ValueError
        except Exception:
            price_max = 9999
            self.price_max_var.set(str(price_max))
        self.config_data["ocr_price_max"] = price_max
        self.save_config()
        return self.read_product_from_image(image, price_max)

    def test_current_ocr(self):
        if self.batch_running:
            return
        self.withdraw()
        self.update()
        time.sleep(0.35)

        try:
            self.ensure_ocr()
            image = self.capture_bgr()
            name, markets = self.read_current_product(image)
            lines = [f"상품명: {name or '(인식 실패)'}", ""]
            for island, market in zip(ISLANDS, markets):
                buy_text = "—" if market.buy_unavailable else market.buy
                sell_text = "—" if market.sell_unavailable else market.sell
                stock_text = "—" if market.stock_unavailable else market.stock
                lines.append(
                    f"{island}: 구매 {buy_text} / 판매 {sell_text} / 재고 {stock_text}"
                )
            self.restore_window()
            messagebox.showinfo("현재 화면 OCR 결과", "\n".join(lines))
        except Exception as exc:
            self.restore_window()
            messagebox.showerror("OCR 시험 실패", str(exc))

    def start_batch_ocr(self):
        if self.batch_running:
            return
        if not all(self.config_data.get("regions", {}).get(k) for k in (
            "product_name", "buy_column", "sell_column", "stock_column"
        )):
            messagebox.showerror(
                "OCR 영역 없음",
                "먼저 샘플 이미지 또는 현재 화면에서 OCR 영역 4개를 지정하세요."
            )
            return

        files = filedialog.askopenfilenames(
            title="무역품 스크린샷 선택 — 상품 1개당 이미지 1장",
            filetypes=[
                ("이미지", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("모든 파일", "*.*"),
            ],
        )
        if not files:
            return
        def natural_key(path):
            return [int(part) if part.isdigit() else part.lower()
                    for part in re.split(r"(\d+)", Path(path).name)]
        files = tuple(sorted(files, key=natural_key))
        if len(files) > 18:
            messagebox.showerror("개수 초과", "스크린샷은 한 번에 최대 18장까지 선택할 수 있습니다.")
            return

        try:
            price_max = int(self.price_max_var.get().replace(",", "").strip())
            if price_max < 100:
                raise ValueError
        except Exception:
            messagebox.showerror("가격 상한 오류", "가격 상한은 100 이상의 정수로 입력하세요.")
            return

        try:
            self.ensure_ocr()
        except Exception as exc:
            messagebox.showerror("OCR 시작 실패", str(exc))
            return

        self.config_data["ocr_price_max"] = price_max
        self.save_config()
        self.batch_running = True
        self.batch_errors = []
        self.batch_ocr_stats = {"fast": 0, "fallback": 0, "dash": 0}
        self.batch_button.configure(state="disabled")
        self.batch_progress.configure(maximum=len(files), value=0)
        self.progress_text_var.set(f"0 / {len(files)}")
        self.status_var.set(f"스크린샷 {len(files)}장을 일괄 OCR하는 중입니다...")
        replace = bool(self.replace_batch_var.get())

        threading.Thread(
            target=self._batch_ocr_worker,
            args=(list(files), replace, price_max),
            daemon=True,
        ).start()

    def _batch_ocr_worker(self, files: list[str], replace: bool, price_max: int):
        scanned: list[Good] = []
        errors: list[str] = []

        for position, path in enumerate(files, 1):
            try:
                image = self.load_image_bgr(path)
                name, markets = self.read_product_from_image(image, price_max)
                for key, value in self.ocr.last_grid_stats.items():
                    self.batch_ocr_stats[key] = self.batch_ocr_stats.get(key, 0) + value
                good = blank_good(position - 1)
                good.name = (name or Path(path).stem or f"무역품 {position}").strip()
                good.markets = {
                    island: market for island, market in zip(ISLANDS, markets)
                }
                good.source_file = path
                scanned.append(good)
            except Exception as exc:
                good = blank_good(position - 1)
                good.name = Path(path).stem or f"무역품 {position}"
                good.source_file = path
                scanned.append(good)
                errors.append(f"{Path(path).name}: {exc}")

            self.after(
                0,
                lambda p=position, total=len(files), n=scanned[-1].name:
                    self._update_batch_progress(p, total, n)
            )

        self.after(
            0,
            lambda: self._finish_batch_ocr(scanned, errors, replace)
        )

    def _update_batch_progress(self, position: int, total: int, name: str):
        self.batch_progress.configure(value=position)
        self.progress_text_var.set(f"{position} / {total}")
        self.status_var.set(f"{position}/{total} 처리: {name}")

    def _finish_batch_ocr(self, scanned: list[Good], errors: list[str], replace: bool):
        if replace:
            self.goods = scanned[:18]
        else:
            room = max(0, 18 - len(self.goods))
            self.goods.extend(scanned[:room])

        self.save_data()
        self.batch_running = False
        self.batch_button.configure(state="normal")
        self.refresh_table(expand_all=True)
        self.progress_text_var.set(f"완료 {len(scanned)}장")

        warning_cells = self.count_warning_cells()
        if errors:
            self.status_var.set(
                f"일괄 OCR 완료: {len(scanned)}장 · 파일 오류 {len(errors)}개 · "
                f"검토 필요 칸 {warning_cells}개 · "
                f"고속 인식 {self.batch_ocr_stats.get('fast', 0)}칸 / "
                f"정밀 재검사 {self.batch_ocr_stats.get('fallback', 0)}칸"
            )
            ERROR_LOG.write_text("\n".join(errors), encoding="utf-8")
        else:
            self.status_var.set(
                f"일괄 OCR 완료: {len(scanned)}장 · 검토 필요 칸 {warning_cells}개 · "
                f"고속 인식 {self.batch_ocr_stats.get('fast', 0)}칸 / "
                f"정밀 재검사 {self.batch_ocr_stats.get('fallback', 0)}칸. "
                "노란 행은 OCR 누락 또는 다른 상품보다 지나치게 크거나 작은 값입니다. 구매 불가의 — 표시는 정상값입니다."
            )

    def clear_scanned_goods(self):
        if not messagebox.askyesno("목록 비우기", "저장된 무역품과 가격표를 모두 비울까요?"):
            return
        self.goods.clear()
        self.save_data()
        self.refresh_table()
        self.batch_progress.configure(value=0)
        self.progress_text_var.set("대기")
        self.status_var.set("무역품 목록을 비웠습니다.")

    @staticmethod
    def _base_warning_fields(market: Market) -> set[str]:
        """OCR 누락, 구매 불가 불일치, 기존 검토 플래그를 셀 단위로 반환한다."""
        fields = set()

        if market.buy is None and not market.buy_unavailable:
            fields.add("buy")
        if market.sell is None and not market.sell_unavailable:
            fields.add("sell")
        if market.stock is None and not market.stock_unavailable:
            fields.add("stock")

        if market.buy_unavailable != market.stock_unavailable:
            fields.add("buy")
            fields.add("stock")

        if market.buy_review:
            fields.add("buy")
        if market.sell_review:
            fields.add("sell")
        if market.stock_review:
            fields.add("stock")

        if market.buy is not None and market.buy < 100:
            fields.add("buy")
        if market.sell is not None and market.sell < 100:
            fields.add("sell")
        if market.stock is not None and not (0 <= market.stock <= 99):
            fields.add("stock")

        return fields

    @classmethod
    def market_warning(cls, market: Market) -> bool:
        return bool(cls._base_warning_fields(market))

    def _build_relative_outlier_map(self):
        """
        다른 상품 및 같은 상품의 다른 섬 가격과 비교해 지나치게 낮거나 높은 값을 찾는다.

        반환 형식:
            {(상품번호, 섬번호): {"buy": "구매↓", "sell": "판매↑", ...}}

        이 검사는 값을 자동 수정하지 않고 노란색 검토 표시만 추가한다.
        """
        result = {}

        global_values = {
            "buy": [],
            "sell": [],
            "stock": [],
        }

        for good in self.goods:
            for island in ISLANDS:
                market = good.markets[island]

                if (
                    market.buy is not None
                    and not market.buy_unavailable
                    and market.buy > 0
                ):
                    global_values["buy"].append(float(market.buy))

                if (
                    market.sell is not None
                    and not market.sell_unavailable
                    and market.sell > 0
                ):
                    global_values["sell"].append(float(market.sell))

                if (
                    market.stock is not None
                    and not market.stock_unavailable
                    and market.stock >= 0
                ):
                    global_values["stock"].append(float(market.stock))

        global_medians = {
            field: (
                float(np.median(values))
                if len(values) >= 5
                else None
            )
            for field, values in global_values.items()
        }

        def add_reason(good_index, island_index, field, reason):
            key = (good_index, island_index)
            field_reasons = result.setdefault(key, {})
            field_reasons.setdefault(field, reason)

        for good_index, good in enumerate(self.goods):
            local_values = {
                "buy": [],
                "sell": [],
                "stock": [],
            }

            for island in ISLANDS:
                market = good.markets[island]

                if (
                    market.buy is not None
                    and not market.buy_unavailable
                    and market.buy > 0
                ):
                    local_values["buy"].append(float(market.buy))

                if (
                    market.sell is not None
                    and not market.sell_unavailable
                    and market.sell > 0
                ):
                    local_values["sell"].append(float(market.sell))

                if (
                    market.stock is not None
                    and not market.stock_unavailable
                    and market.stock >= 0
                ):
                    local_values["stock"].append(float(market.stock))

            local_medians = {
                field: (
                    float(np.median(values))
                    if len(values) >= 3
                    else None
                )
                for field, values in local_values.items()
            }

            for island_index, island in enumerate(ISLANDS):
                market = good.markets[island]

                for field, low_text, high_text in (
                    ("buy", "구매↓", "구매↑"),
                    ("sell", "판매↓", "판매↑"),
                ):
                    value = getattr(market, field)
                    unavailable = getattr(market, f"{field}_unavailable")
                    if value is None or unavailable or value <= 0:
                        continue

                    value = float(value)
                    local_median = local_medians[field]
                    global_median = global_medians[field]

                    # 같은 상품의 다른 섬 가격보다 자릿수 단위로 차이 나는 값.
                    if local_median is not None and local_median >= 300:
                        if value < local_median * 0.42:
                            add_reason(
                                good_index, island_index, field, low_text
                            )
                            continue
                        if value > local_median * 2.40:
                            add_reason(
                                good_index, island_index, field, high_text
                            )
                            continue

                    # 같은 상품 전체가 잘못 읽힌 경우를 대비한 전체 상품 비교.
                    if global_median is not None and global_median >= 300:
                        if value < max(100.0, global_median * 0.25):
                            add_reason(
                                good_index, island_index, field, low_text
                            )
                            continue
                        if value > global_median * 4.80:
                            add_reason(
                                good_index, island_index, field, high_text
                            )

                # 수량은 0도 정상일 수 있어 지나치게 큰 값만 표시.
                if (
                    market.stock is not None
                    and not market.stock_unavailable
                    and market.stock >= 0
                ):
                    value = float(market.stock)
                    local_median = local_medians["stock"]
                    global_median = global_medians["stock"]

                    if (
                        local_median is not None
                        and local_median >= 1
                        and value > max(30.0, local_median * 3.0)
                    ):
                        add_reason(
                            good_index, island_index, "stock", "수량↑"
                        )
                    elif (
                        global_median is not None
                        and global_median >= 1
                        and value > max(40.0, global_median * 4.0)
                    ):
                        add_reason(
                            good_index, island_index, "stock", "수량↑"
                        )

        return result

    def count_warning_cells(self) -> int:
        outliers = self._build_relative_outlier_map()
        count = 0

        for good_index, good in enumerate(self.goods):
            for island_index, island in enumerate(ISLANDS):
                market = good.markets[island]
                fields = self._base_warning_fields(market)
                fields.update(
                    outliers.get((good_index, island_index), {}).keys()
                )
                count += len(fields)

        return count

    def refresh_table(self, expand_all: Optional[bool] = None):
        open_indices = set()
        if expand_all is None:
            for item in self.tree.get_children(""):
                if item.startswith("g:") and self.tree.item(item, "open"):
                    try:
                        open_indices.add(int(item.split(":")[1]))
                    except Exception:
                        pass
        elif expand_all:
            open_indices = set(range(len(self.goods)))

        self.tree.delete(*self.tree.get_children())
        outlier_map = self._build_relative_outlier_map()

        for idx, good in enumerate(self.goods):
            markets = [good.markets[island] for island in ISLANDS]
            buys = [
                market.buy for market in markets
                if market.buy is not None and not market.buy_unavailable
            ]
            sells = [
                market.sell for market in markets
                if market.sell is not None and not market.sell_unavailable
            ]
            stocks = [
                market.stock for market in markets
                if market.stock is not None and not market.stock_unavailable
            ]

            warning_rows = 0
            for island_index, market in enumerate(markets):
                if (
                    self._base_warning_fields(market)
                    or outlier_map.get((idx, island_index))
                ):
                    warning_rows += 1

            parent_id = f"g:{idx}"
            parent_tags = []
            if not good.active:
                parent_tags.append("inactive")
            if warning_rows:
                parent_tags.append("warning")

            self.tree.insert(
                "", "end", iid=parent_id,
                text=good.name,
                open=idx in open_indices,
                values=(
                    "☑" if good.active else "☐",
                    idx + 1,
                    "6개 섬",
                    min(buys) if buys else "",
                    max(sells) if sells else "",
                    sum(stocks) if stocks else "",
                    f"⚠ {warning_rows}행" if warning_rows else "정상",
                ),
                tags=tuple(parent_tags),
            )

            for island_index, island in enumerate(ISLANDS):
                market = good.markets[island]
                base_fields = self._base_warning_fields(market)
                outlier_fields = outlier_map.get((idx, island_index), {})
                warning_fields = set(base_fields)
                warning_fields.update(outlier_fields.keys())

                tags = ["child"]
                if warning_fields:
                    tags.append("warning")
                if not good.active:
                    tags.append("inactive")

                reasons = []
                if base_fields:
                    reasons.append("OCR 확인")
                for field in ("buy", "sell", "stock"):
                    reason = outlier_fields.get(field)
                    if reason and reason not in reasons:
                        reasons.append(reason)
                review = " · ".join(reasons)

                self.tree.insert(
                    parent_id, "end", iid=f"m:{idx}:{island_index}",
                    text="",
                    values=(
                        "", "", island,
                        "—" if market.buy_unavailable else (
                            "" if market.buy is None else market.buy
                        ),
                        "—" if market.sell_unavailable else (
                            "" if market.sell is None else market.sell
                        ),
                        "—" if market.stock_unavailable else (
                            "" if market.stock is None else market.stock
                        ),
                        review,
                    ),
                    tags=tuple(tags),
                )

    def expand_all(self):
        for item in self.tree.get_children(""):
            self.tree.item(item, open=True)

    def collapse_all(self):
        for item in self.tree.get_children(""):
            self.tree.item(item, open=False)

    def selected_good_index(self) -> Optional[int]:
        selected = self.tree.selection()
        if not selected:
            return None
        parts = selected[0].split(":")
        if len(parts) < 2:
            return None
        try:
            return int(parts[1])
        except Exception:
            return None

    def on_tree_click(self, event):
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not item or column != "#1":
            return
        if item.startswith("m:"):
            item = self.tree.parent(item)
        if not item.startswith("g:"):
            return
        try:
            idx = int(item.split(":")[1])
        except Exception:
            return
        self.goods[idx].active = not self.goods[idx].active
        if not self.goods[idx].active:
            self.tree.item(item, open=False)
        self.save_data()
        self.refresh_table()
        return "break"

    def begin_cell_edit(self, event):
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not item:
            return

        editable = False
        current_value = ""
        field = None
        good_index = None
        island_index = None

        if item.startswith("g:") and column == "#0":
            good_index = int(item.split(":")[1])
            current_value = self.goods[good_index].name
            field = "name"
            editable = True
        elif item.startswith("m:") and column in ("#4", "#5", "#6"):
            _, g, i = item.split(":")
            good_index, island_index = int(g), int(i)
            market = self.goods[good_index].markets[ISLANDS[island_index]]
            field = {"#4": "buy", "#5": "sell", "#6": "stock"}[column]
            value = getattr(market, field)
            unavailable = getattr(market, f"{field}_unavailable")
            current_value = "—" if unavailable else (
                "" if value is None else str(value)
            )
            editable = True

        if not editable:
            return
        bbox = self.tree.bbox(item, column)
        if not bbox:
            return

        if self.cell_editor is not None:
            try:
                self.cell_editor.destroy()
            except Exception:
                pass

        x, y, width, height = bbox
        editor = ttk.Entry(self.tree)
        editor.insert(0, current_value)
        editor.select_range(0, "end")
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()
        self.cell_editor = editor
        committed = {"done": False}

        def close(save: bool):
            if committed["done"]:
                return
            committed["done"] = True
            new_value = editor.get()
            try:
                if save:
                    if field == "name":
                        self.goods[good_index].name = new_value.strip() or f"무역품 {good_index + 1}"
                    else:
                        stripped = new_value.replace(",", "").strip()
                        market = self.goods[good_index].markets[ISLANDS[island_index]]

                        if stripped in ("-", "—"):
                            setattr(market, field, None)
                            setattr(market, f"{field}_unavailable", True)
                            setattr(market, f"{field}_review", False)
                            if field == "buy":
                                market.stock = None
                                market.stock_unavailable = True
                                market.stock_review = False
                        else:
                            parsed = None if stripped == "" else int(stripped)
                            if parsed is not None and parsed < 0:
                                raise ValueError
                            setattr(market, field, parsed)
                            setattr(market, f"{field}_unavailable", False)
                            setattr(market, f"{field}_review", parsed is None)
                            if field == "buy" and parsed is not None:
                                market.buy_unavailable = False
                    self.save_data()
            except Exception:
                messagebox.showerror("입력 오류", "빈칸, - 또는 0 이상의 정수를 입력하세요.")
            finally:
                try:
                    editor.destroy()
                except Exception:
                    pass
                self.cell_editor = None
                self.refresh_table()

        editor.bind("<Return>", lambda _e: close(True))
        editor.bind("<Escape>", lambda _e: close(False))
        editor.bind("<FocusOut>", lambda _e: close(True))
        return "break"

    def open_source_image(self):
        idx = self.selected_good_index()
        if idx is None or idx >= len(self.goods):
            messagebox.showinfo("선택 필요", "원본을 볼 상품을 선택하세요.")
            return
        path = self.goods[idx].source_file
        if not path or not Path(path).exists():
            messagebox.showinfo("원본 없음", "이 상품에 연결된 원본 스크린샷이 없습니다.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror("원본 열기 실패", str(exc))


    def add_good(self):
        if len(self.goods) >= 18:
            messagebox.showinfo("최대 개수", "무역품은 최대 18종까지 등록할 수 있습니다.")
            return
        self.goods.append(blank_good(len(self.goods)))
        self.save_data()
        self.refresh_table()
        parent = f"g:{len(self.goods) - 1}"
        if self.tree.exists(parent):
            self.tree.item(parent, open=True)
            self.tree.selection_set(parent)
            self.tree.see(parent)

    def delete_selected(self):
        idx = self.selected_good_index()
        if idx is None or idx >= len(self.goods):
            messagebox.showinfo("선택 필요", "삭제할 상품 또는 해당 섬 행을 선택하세요.")
            return
        if not messagebox.askyesno("상품 삭제", f"'{self.goods[idx].name}' 전체를 삭제할까요?"):
            return
        self.goods.pop(idx)
        self.save_data()
        self.refresh_table()

    def _migrate_map_config(self):
        """기존 버전 config.json을 고정 맵 슬롯 방식으로 자동 보완한다."""
        changed = False

        slot_islands = self.config_data.get("slot_islands")
        if (
            not isinstance(slot_islands, list)
            or len(slot_islands) != 6
            or sorted(slot_islands) != sorted(ISLANDS)
        ):
            self.config_data["slot_islands"] = list(DEFAULT_SLOT_ISLANDS)
            changed = True

        fixed = self.config_data.get("fixed_slot_distance_matrix")
        if (
            not isinstance(fixed, list)
            or len(fixed) != 6
            or any(not isinstance(row, list) or len(row) != 6 for row in fixed)
        ):
            self.config_data["fixed_slot_distance_matrix"] = [
                list(row) for row in FIXED_SLOT_DISTANCE_MATRIX
            ]
            changed = True

        normalized_hex = normalize_hex_map(self.config_data.get("hex_map"))
        if self.config_data.get("hex_map") != normalized_hex:
            self.config_data["hex_map"] = normalized_hex
            changed = True

        if self.config_data.get("route_objective") == "균형형":
            self.config_data["route_objective"] = "자동 균형"
            changed = True

        if "durability_value_per_100" in self.config_data:
            # v18부터는 수동 환산값을 사용하지 않는다.
            self.config_data.pop("durability_value_per_100", None)
            changed = True

        for key, default_value in (
            ("max_durability", 4800),
            ("route_objective", "이번 회차 코인 최대"),
            ("auto_balance_cycle_turns", 15),
        ):
            if key not in self.config_data:
                self.config_data[key] = default_value
                changed = True

        # 기존 코드와의 호환을 위해 현재 이름 기준 거리표도 함께 저장한다.
        current = self._build_name_distance_matrix(
            self.config_data["slot_islands"],
            self.config_data["fixed_slot_distance_matrix"],
        )
        if self.config_data.get("distance_matrix") != current:
            self.config_data["distance_matrix"] = current
            changed = True

        if changed:
            save_json(CONFIG_PATH, self.config_data)

    @staticmethod
    def _build_name_distance_matrix(slot_islands, fixed_matrix):
        slot_of_island = {
            island_name: slot_index
            for slot_index, island_name in enumerate(slot_islands)
        }
        matrix = [[0] * 6 for _ in range(6)]
        for island_a, name_a in enumerate(ISLANDS):
            for island_b, name_b in enumerate(ISLANDS):
                slot_a = slot_of_island[name_a]
                slot_b = slot_of_island[name_b]
                matrix[island_a][island_b] = int(fixed_matrix[slot_a][slot_b])
        return matrix

    def get_current_distance_matrix(self):
        slot_islands = self.config_data.get(
            "slot_islands", list(DEFAULT_SLOT_ISLANDS)
        )
        fixed_matrix = self.config_data.get(
            "fixed_slot_distance_matrix",
            [list(row) for row in FIXED_SLOT_DISTANCE_MATRIX],
        )
        return self._build_name_distance_matrix(slot_islands, fixed_matrix)

    def _on_hex_map_enabled_changed(self):
        config = normalize_hex_map(self.config_data.get("hex_map"))
        config["enabled"] = bool(self.hex_map_enabled_var.get())
        self.config_data["hex_map"] = config
        self.save_config()
        mode = "사용" if config["enabled"] else "미사용"
        self.status_var.set(f"육각 맵 경로 계산: {mode}")

    def open_hex_map_editor(self):
        def save_hex_map(value):
            normalized = normalize_hex_map(value)
            self.config_data["hex_map"] = normalized
            self.hex_map_enabled_var.set(bool(normalized["enabled"]))
            self.save_config()
            self.status_var.set(
                "육각 맵을 저장했습니다. 장애물 우회, 수리 타일, 실제 이동 내구도를 다음 계산부터 반영합니다."
            )

        HexMapEditor(
            self,
            normalize_hex_map(self.config_data.get("hex_map")),
            save_hex_map,
        )

    def _hex_island_cells(self, hex_map):
        """현재 섬 이름을 육각 맵의 물리적 슬롯 타일에 연결한다."""
        slot_islands = self.config_data.get("slot_islands", list(DEFAULT_SLOT_ISLANDS))
        slots = hex_map.get("slots", {})
        island_cells = {}
        for slot_index, island_name in enumerate(slot_islands):
            if slot_index >= len(MAP_SLOT_LABELS):
                continue
            slot_label = MAP_SLOT_LABELS[slot_index]
            cell = parse_cell(slots.get(slot_label))
            if cell is not None and island_name in ISLANDS:
                island_cells[ISLANDS.index(island_name)] = cell
        return island_cells

    @staticmethod
    def _movement_route_key(result):
        segment_paths = []
        for segment in result.get("segments", []):
            segment_paths.append(tuple(tuple(cell) for cell in segment.get("tile_path", [])))
        return tuple(result.get("path", [])), tuple(segment_paths)

    @staticmethod
    def _estimate_auto_balance_model(results, move_cost, max_durability, config):
        """현재 가격·재고·보유 코인에서 내구도의 미래 수익 가치를 자동 추정한다.

        1칸 기대수익은 실제 생성된 수익 경로의 턴당 순이익으로 계산한다.
        극단적인 한 경로에 끌려가지 않도록 상위 15% 경로의 중앙값을 사용한다.
        내구도는 15칸 단위의 미래 무역 회차로 나누고, 먼 회차일수록 할인한다.
        """
        move_cost = max(1, int(move_cost))
        cycle_turns = max(1, int(config.get("auto_balance_cycle_turns", 15)))

        rates = []
        for result in results:
            used = int(result.get("used", 0))
            profit = int(result.get("profit", 0))
            if used > 0 and profit > 0 and result.get("transactions"):
                rates.append(profit / used)

        sample_count = 0
        rate_variability = 0.0
        if rates:
            rates.sort(reverse=True)
            sample_count = max(1, int(math.ceil(len(rates) * 0.15)))
            sample_count = min(len(rates), max(3, sample_count))
            sample_rates = rates[:sample_count]
            base_profit_per_move = float(statistics.median(sample_rates))
            rate_mean = float(statistics.fmean(sample_rates))
            if len(sample_rates) >= 2 and rate_mean > 0:
                rate_variability = float(statistics.pstdev(sample_rates)) / rate_mean
        else:
            base_profit_per_move = 0.0

        # 현재 수익 경로가 안정적이면 미래 회차도 비슷한 가치를 가질 가능성이 높아
        # 할인율을 높이고, 경로별 수익 편차가 크면 미래 불확실성을 크게 본다.
        future_discount = 0.90 - min(0.25, rate_variability * 0.80)
        future_discount = min(0.92, max(0.65, future_discount))

        return {
            "base_profit_per_move": max(0.0, base_profit_per_move),
            "move_cost": move_cost,
            "cycle_turns": cycle_turns,
            "future_discount": future_discount,
            "rate_variability": rate_variability,
            "max_durability": max(0, int(max_durability)),
            "sample_count": sample_count,
            "profitable_route_count": len(rates),
        }

    @staticmethod
    def _auto_durability_utility(durability, model):
        """내구도를 미래에 사용할 이동권의 기대 수익으로 환산한다.

        내구도가 적을 때 남은 100은 가까운 회차에 사용되므로 가치가 높고,
        최대치에 가까울 때 추가 100은 먼 회차 몫이므로 자동으로 낮게 평가된다.
        """
        durability = max(0.0, float(durability))
        move_cost = max(1.0, float(model["move_cost"]))
        cycle_turns = max(1, int(model["cycle_turns"]))
        discount = float(model["future_discount"])
        base = max(0.0, float(model["base_profit_per_move"]))

        movement_units = durability / move_cost
        full_units = int(math.floor(movement_units))
        partial_unit = movement_units - full_units
        utility = 0.0

        for unit_index in range(full_units):
            cycle_index = unit_index // cycle_turns
            utility += base * (discount ** cycle_index)

        if partial_unit > 0:
            cycle_index = full_units // cycle_turns
            utility += partial_unit * base * (discount ** cycle_index)

        return utility

    @classmethod
    def _auto_marginal_durability_value(cls, durability, model, amount=100):
        """현재 내구도에서 마지막 amount만큼이 가지는 자동 추정 가치."""
        durability = max(0, int(durability))
        amount = max(1, int(amount))
        lower = max(0, durability - amount)
        return (
            cls._auto_durability_utility(durability, model)
            - cls._auto_durability_utility(lower, model)
        )

    def _route_sort_key(self, result, objective, auto_model=None):
        ending = int(result.get("ending_durability", 0))
        cash = int(result.get("cash", 0))
        used = int(result.get("used", 0))
        if objective == "종료 내구도 우선":
            return (ending, cash, -used, -len(result.get("path", [])))
        if objective == "자동 균형":
            utility = self._auto_durability_utility(ending, auto_model or {})
            score = cash + utility
            result["durability_utility"] = utility
            result["strategy_score"] = score
            result["marginal_durability_value_100"] = (
                self._auto_marginal_durability_value(
                    ending,
                    auto_model or {},
                    100,
                )
            )
            return (score, cash, ending, -used)
        result["strategy_score"] = float(cash)
        return (cash, ending, -used, -len(result.get("path", [])))

    def open_island_layout_editor(self):
        """
        거리는 수정하지 않고, 이번 판에서 각 고정 위치에 어떤 섬이 놓였는지만 지정한다.
        """
        win = tk.Toplevel(self)
        win.title("이번 판 섬 배치 설정")
        win.geometry("760x455")
        win.transient(self)
        win.grab_set()

        ttk.Label(
            win,
            text=(
                "맵 타일과 거리는 항상 동일하므로 섬 이름만 위치에 맞게 지정하세요.\n"
                "각 섬은 정확히 한 번씩 선택해야 합니다."
            ),
            padding=12,
            justify="left",
        ).pack(fill="x")

        board = ttk.Frame(win, padding=12)
        board.pack(fill="both", expand=True)

        current = self.config_data.get(
            "slot_islands", list(DEFAULT_SLOT_ISLANDS)
        )
        variables = [tk.StringVar(value=current[i]) for i in range(6)]

        def add_slot(slot_index, row, column, label):
            frame = ttk.Labelframe(board, text=label, padding=8)
            frame.grid(row=row, column=column, padx=12, pady=10, sticky="nsew")
            ttk.Combobox(
                frame,
                textvariable=variables[slot_index],
                values=ISLANDS,
                state="readonly",
                width=17,
            ).pack()

        # 실제 화면 배치와 비슷하게 배치
        add_slot(0, 0, 0, "북서쪽")
        add_slot(1, 0, 2, "북동쪽")
        add_slot(2, 1, 0, "서쪽")
        add_slot(3, 1, 1, "중앙")
        add_slot(4, 1, 2, "동쪽")
        add_slot(5, 2, 1, "남쪽")

        for col in range(3):
            board.columnconfigure(col, weight=1)
        for row in range(3):
            board.rowconfigure(row, weight=1)

        preview_var = tk.StringVar()
        ttk.Label(
            win,
            textvariable=preview_var,
            padding=(12, 0, 12, 5),
            justify="left",
        ).pack(fill="x")

        def update_preview(*_args):
            values = [variable.get() for variable in variables]
            duplicate = len(set(values)) != 6
            if duplicate:
                preview_var.set("⚠ 같은 섬이 두 위치 이상에 선택되어 있습니다.")
            else:
                preview_var.set(
                    "현재 배치: "
                    + " · ".join(
                        f"{MAP_SLOT_LABELS[i]}={values[i].replace('들의 섬', '')}"
                        for i in range(6)
                    )
                )

        for variable in variables:
            variable.trace_add("write", update_preview)
        update_preview()

        button_frame = ttk.Frame(win, padding=10)
        button_frame.pack(fill="x")

        def apply():
            values = [variable.get() for variable in variables]
            if sorted(values) != sorted(ISLANDS):
                messagebox.showerror(
                    "배치 오류",
                    "6개 섬을 중복 없이 한 번씩 선택하세요.",
                    parent=win,
                )
                return

            self.config_data["slot_islands"] = values
            self.config_data["distance_matrix"] = self._build_name_distance_matrix(
                values,
                self.config_data.get(
                    "fixed_slot_distance_matrix",
                    [list(row) for row in FIXED_SLOT_DISTANCE_MATRIX],
                ),
            )
            self.save_config()
            self.status_var.set(
                "이번 판 섬 배치를 저장했습니다. 고정 거리표가 섬 이름에 맞게 자동 변환됩니다."
            )
            win.destroy()

        ttk.Button(button_frame, text="현재 화면 배치로 초기화", command=lambda: [
            variables[i].set(DEFAULT_SLOT_ISLANDS[i]) for i in range(6)
        ]).pack(side="left")
        ttk.Button(button_frame, text="저장", command=apply).pack(side="right", padx=5)
        ttk.Button(button_frame, text="취소", command=win.destroy).pack(side="right")

    def open_fixed_distance_viewer(self):
        """
        물리적 위치 기준 고정 거리표를 직접 수정한다.

        위쪽 삼각형만 입력하며 반대 방향 값은 자동으로 동일하게 반영한다.
        저장 즉시 현재 섬 배치에 맞는 이름 기준 거리표도 다시 계산한다.
        """
        win = tk.Toplevel(self)
        win.title("고정 맵 거리표 수동 수정")
        win.geometry("980x610")
        win.transient(self)
        win.grab_set()

        slot_islands = self.config_data.get(
            "slot_islands", list(DEFAULT_SLOT_ISLANDS)
        )
        stored_matrix = self.config_data.get(
            "fixed_slot_distance_matrix",
            [list(row) for row in FIXED_SLOT_DISTANCE_MATRIX],
        )
        matrix = [list(row) for row in stored_matrix]

        ttk.Label(
            win,
            text=(
                "물리적 위치 사이의 실제 이동 턴 수를 수정하세요.\n"
                "대각선은 0으로 고정되며, 한쪽 값을 수정하면 반대 방향도 자동으로 같은 값이 됩니다."
            ),
            padding=12,
            justify="left",
        ).pack(fill="x")

        mapping = ttk.Labelframe(
            win,
            text="현재 위치별 섬 배치",
            padding=8,
        )
        mapping.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(
            mapping,
            text=" · ".join(
                f"{MAP_SLOT_LABELS[index]}={slot_islands[index]}"
                for index in range(6)
            ),
            wraplength=920,
            justify="left",
        ).pack(anchor="w")

        table_frame = ttk.Labelframe(
            win,
            text="고정 위치 간 거리",
            padding=10,
        )
        table_frame.pack(fill="both", expand=True, padx=10, pady=5)

        variables = {}
        entries = {}

        # 열 제목: 위치명과 현재 섬 이름을 함께 표시
        for column, label in enumerate(MAP_SLOT_LABELS, 1):
            island_short = slot_islands[column - 1].replace("들의 섬", "")
            ttk.Label(
                table_frame,
                text=f"{label}\n({island_short})",
                width=13,
                anchor="center",
                justify="center",
            ).grid(row=0, column=column, padx=3, pady=4)

        for row, label in enumerate(MAP_SLOT_LABELS, 1):
            island_short = slot_islands[row - 1].replace("들의 섬", "")
            ttk.Label(
                table_frame,
                text=f"{label}\n({island_short})",
                width=13,
                anchor="center",
                justify="center",
            ).grid(row=row, column=0, padx=3, pady=4)

            for column in range(1, 7):
                row_index = row - 1
                column_index = column - 1
                value = int(matrix[row_index][column_index])
                variable = tk.StringVar(value=str(value))
                variables[(row_index, column_index)] = variable

                entry = ttk.Entry(
                    table_frame,
                    textvariable=variable,
                    width=9,
                    justify="center",
                )
                entry.grid(row=row, column=column, padx=3, pady=4)
                entries[(row_index, column_index)] = entry

                if row_index == column_index:
                    entry.configure(state="disabled")
                elif row_index > column_index:
                    # 아래쪽 삼각형은 위쪽 입력값을 보여주는 읽기 전용 칸
                    entry.configure(state="readonly")

        status_var = tk.StringVar(
            value="위쪽 삼각형의 숫자만 수정하면 반대 방향 값이 자동으로 따라옵니다."
        )
        ttk.Label(
            win,
            textvariable=status_var,
            padding=(12, 4),
        ).pack(fill="x")

        updating = {"active": False}

        def mirror_value(source_row, source_column):
            if updating["active"]:
                return
            updating["active"] = True
            try:
                value = variables[(source_row, source_column)].get()
                variables[(source_column, source_row)].set(value)
                status_var.set(
                    f"{MAP_SLOT_LABELS[source_row]} ↔ "
                    f"{MAP_SLOT_LABELS[source_column]} 거리: {value or '?'}턴"
                )
            finally:
                updating["active"] = False

        # 위쪽 삼각형의 변경을 아래쪽에 실시간 반영
        for row_index in range(6):
            for column_index in range(row_index + 1, 6):
                variables[(row_index, column_index)].trace_add(
                    "write",
                    lambda *_args, r=row_index, c=column_index: mirror_value(r, c),
                )

        button_frame = ttk.Frame(win, padding=10)
        button_frame.pack(fill="x")

        def reset_defaults():
            default_matrix = [
                list(row) for row in FIXED_SLOT_DISTANCE_MATRIX
            ]
            updating["active"] = True
            try:
                for row_index in range(6):
                    for column_index in range(6):
                        variables[(row_index, column_index)].set(
                            str(default_matrix[row_index][column_index])
                        )
            finally:
                updating["active"] = False
            status_var.set("기본 고정 거리표로 되돌렸습니다. 저장을 눌러야 적용됩니다.")

        def apply():
            try:
                new_matrix = [[0] * 6 for _ in range(6)]

                for row_index in range(6):
                    for column_index in range(row_index + 1, 6):
                        raw_value = variables[
                            (row_index, column_index)
                        ].get().strip()
                        value = int(raw_value)
                        if value < 1 or value > 99:
                            raise ValueError(
                                f"{MAP_SLOT_LABELS[row_index]} ↔ "
                                f"{MAP_SLOT_LABELS[column_index]}"
                            )

                        new_matrix[row_index][column_index] = value
                        new_matrix[column_index][row_index] = value

                self.config_data[
                    "fixed_slot_distance_matrix"
                ] = new_matrix

                # 현재 섬 이름 배치 기준 거리표도 즉시 갱신
                self.config_data[
                    "distance_matrix"
                ] = self._build_name_distance_matrix(
                    slot_islands,
                    new_matrix,
                )
                self.save_config()

                self.status_var.set(
                    "고정 맵 거리표를 저장했습니다. 이후 동선 계산부터 새 거리를 사용합니다."
                )
                win.destroy()
            except Exception as exc:
                messagebox.showerror(
                    "입력 오류",
                    "모든 거리는 1~99 사이 정수로 입력하세요.\n\n"
                    f"확인 위치: {exc}",
                    parent=win,
                )

        ttk.Button(
            button_frame,
            text="기본값으로 되돌리기",
            command=reset_defaults,
        ).pack(side="left")

        ttk.Button(
            button_frame,
            text="취소",
            command=win.destroy,
        ).pack(side="right", padx=4)

        ttk.Button(
            button_frame,
            text="저장",
            command=apply,
        ).pack(side="right", padx=4)

    def auto_distance_calibration(self):
        prompts = [
            "기준이 될 빈 육각형 타일의 정확한 중앙",
            "기준 타일의 바로 오른쪽 인접 타일 중앙",
            "기준 타일의 오른쪽 아래 인접 타일 중앙",
        ] + [f"{name}이 놓인 육각형의 중앙" for name in ISLANDS]

        screenshot = self.capture_pil_hidden()

        def selected(points):
            self.restore_window()
            if not points:
                return
            try:
                p0 = np.array(points[0], dtype=float)
                bq = np.array(points[1], dtype=float) - p0
                br = np.array(points[2], dtype=float) - p0
                basis = np.column_stack([bq, br])
                if abs(np.linalg.det(basis)) < 100:
                    raise ValueError("두 인접 타일 방향이 너무 비슷합니다.")

                coords = []
                for point in points[3:]:
                    coeff = np.linalg.solve(basis, np.array(point, dtype=float) - p0)
                    q, r = int(round(coeff[0])), int(round(coeff[1]))
                    coords.append([q, r])

                matrix = [[0] * 6 for _ in range(6)]
                for i in range(6):
                    for j in range(6):
                        dq = coords[j][0] - coords[i][0]
                        dr = coords[j][1] - coords[i][1]
                        matrix[i][j] = max(abs(dq), abs(dr), abs(dq + dr))

                self.config_data["distance_matrix"] = matrix
                self.config_data["map_calibration"] = {
                    "points": points,
                    "hex_coordinates": coords,
                }
                self.save_config()
                self.open_distance_editor(
                    title="자동 계산 결과 확인 — 잘못된 값은 바로 수정하세요."
                )
            except Exception as exc:
                messagebox.showerror("거리 자동 계산 실패", str(exc))

        PointSelector(
            self,
            screenshot,
            "맵 거리 자동 계산",
            selected,
            fixed_prompts=prompts,
            existing=None,
        )

    def open_distance_editor(self, title="섬 간 최소 이동 턴 수"):
        win = tk.Toplevel(self)
        win.title("거리 설정")
        win.geometry("830x470")
        win.transient(self)

        ttk.Label(
            win,
            text=title + "\n대칭 위치는 저장할 때 자동으로 같은 값으로 맞춰집니다.",
            padding=10,
            justify="left",
        ).pack(fill="x")

        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        matrix = self.get_current_distance_matrix()
        vars_ = {}

        short_names = ["농부", "목동", "어부", "감정가", "장인", "조각가"]
        for col, name in enumerate(short_names, 1):
            ttk.Label(frame, text=name, width=10).grid(row=0, column=col, padx=2)
        for row, name in enumerate(short_names, 1):
            ttk.Label(frame, text=name, width=10).grid(row=row, column=0, padx=2)
            for col in range(1, 7):
                value = matrix[row - 1][col - 1]
                var = tk.StringVar(value=str(value))
                entry = ttk.Entry(frame, textvariable=var, width=8, justify="center")
                entry.grid(row=row, column=col, padx=3, pady=4)
                if row == col:
                    entry.configure(state="disabled")
                vars_[(row - 1, col - 1)] = var

        def apply():
            try:
                new = [[0] * 6 for _ in range(6)]
                for i in range(6):
                    for j in range(6):
                        if i == j:
                            continue
                        value = int(vars_[(i, j)].get())
                        if value < 1 or value > 99:
                            raise ValueError
                        new[i][j] = value

                for i in range(6):
                    for j in range(i + 1, 6):
                        a, b = new[i][j], new[j][i]
                        value = a if a == b else min(a, b)
                        new[i][j] = value
                        new[j][i] = value

                self.config_data["distance_matrix"] = new
                self.save_config()
                win.destroy()
            except Exception:
                messagebox.showerror("입력 오류", "거리는 1~99 사이 정수로 입력하세요.", parent=win)

        ttk.Button(win, text="저장", command=apply).pack(pady=8)

    def _enumerate_hex_routes(
        self,
        start,
        turns,
        current_durability,
        max_durability,
        active_goods,
        required_islands=None,
        forbidden_islands=None,
    ):
        """
        육각 타일을 실제로 이동하면서 섬 방문 순서를 만든다.

        - 장애물 타일은 통과하지 않는다.
        - 다른 섬 타일을 몰래 지나가지 않고, 방문 순서에 명시된 목적지만 통과한다.
        - 수리 타일은 이동 후 수리하며 최대 내구도를 넘지 않는다.
        - 같은 수리 타일은 기본적으로 한 계산 경로에서 한 번만 적용한다.
        """
        required_islands = set(required_islands or ())
        forbidden_islands = set(forbidden_islands or ())
        hex_map = normalize_hex_map(self.config_data.get("hex_map"))
        island_cells = self._hex_island_cells(hex_map)

        if len(island_cells) != 6:
            return [], 0, "육각 맵에 6개 물리적 섬 위치가 모두 지정되지 않았습니다."
        if start in forbidden_islands:
            return [], 0, "현재 위치가 방문 금지 섬입니다."

        all_slot_cells = set(island_cells.values())
        pair_cache = {}

        def pair_options(source, destination):
            key = (source, destination)
            if key not in pair_cache:
                pair_cache[key] = build_pair_path_options(
                    hex_map,
                    island_cells[source],
                    island_cells[destination],
                    int(turns),
                    other_slot_cells=all_slot_cells - {
                        island_cells[source], island_cells[destination]
                    },
                )
            return pair_cache[key]

        initial = {
            "path": [start],
            "used": 0,
            "ending_durability": int(current_durability),
            "used_repairs": set(),
            "repair_gain": 0,
            "segments": [],
        }
        stack = [initial]
        routes = []
        generated_states = 0
        best_prefix = {}
        hard_state_limit = max(20000, int(self.config_data.get("route_solve_limit", 5000)) * 8)

        while stack:
            state = stack.pop()
            current = state["path"][-1]

            for destination in range(6):
                if destination == current or destination in forbidden_islands:
                    continue

                for option in pair_options(current, destination):
                    steps = int(option["steps"])
                    new_used = state["used"] + steps
                    if new_used > turns:
                        continue

                    simulated = simulate_hex_path(
                        option,
                        state["ending_durability"],
                        max_durability,
                        hex_map,
                        state["used_repairs"],
                    )
                    if simulated is None:
                        continue

                    segment = {
                        "source": current,
                        "destination": destination,
                        "turns": steps,
                        "tile_path": [list(cell) for cell in option["path"]],
                        "durability_spent": simulated["durability_spent"],
                        "repair_gain": simulated["repair_gain"],
                        "net_durability_change": simulated["net_durability_change"],
                        "ending_durability": simulated["ending_durability"],
                        "repairs": simulated["repairs"],
                    }
                    new_path = state["path"] + [destination]
                    new_state = {
                        "path": new_path,
                        "used": new_used,
                        "ending_durability": simulated["ending_durability"],
                        "used_repairs": simulated["used_repairs"],
                        "repair_gain": state["repair_gain"] + simulated["repair_gain"],
                        "segments": state["segments"] + [segment],
                    }
                    generated_states += 1

                    if required_islands.issubset(set(new_path)):
                        routes.append(new_state)

                    if new_used >= turns:
                        continue

                    # 같은 섬 순서·턴·수리 사용 상태에서 내구도가 더 낮은 접두 경로는 버린다.
                    prefix_key = (
                        tuple(new_path),
                        new_used,
                        tuple(sorted(simulated["used_repairs"])),
                    )
                    previous_best = best_prefix.get(prefix_key)
                    if previous_best is not None and previous_best >= simulated["ending_durability"]:
                        continue
                    best_prefix[prefix_key] = simulated["ending_durability"]
                    stack.append(new_state)

                    if generated_states >= hard_state_limit:
                        stack.clear()
                        break
                if generated_states >= hard_state_limit:
                    break

        unique = {}
        for route in routes:
            key = (
                tuple(route["path"]),
                tuple(
                    tuple(tuple(cell) for cell in segment["tile_path"])
                    for segment in route["segments"]
                ),
            )
            previous = unique.get(key)
            if previous is None or route["ending_durability"] > previous["ending_durability"]:
                unique[key] = route

        ranked = list(unique.values())
        ranked.sort(
            key=lambda route: (
                self._route_upper_bound(route["path"], active_goods),
                route["ending_durability"],
                -route["used"],
            ),
            reverse=True,
        )
        return ranked, generated_states, ""

    def _fixed_route_records(self, routes, starting_durability):
        records = []
        move_cost = 100
        for path, used in routes:
            segments = []
            running = int(starting_durability)
            matrix = self.get_current_distance_matrix()
            for source, destination in zip(path, path[1:]):
                turns = int(matrix[source][destination])
                spent = turns * move_cost
                running -= spent
                segments.append({
                    "source": source,
                    "destination": destination,
                    "turns": turns,
                    "tile_path": [],
                    "durability_spent": spent,
                    "repair_gain": 0,
                    "net_durability_change": -spent,
                    "ending_durability": running,
                    "repairs": [],
                })
            records.append({
                "path": path,
                "used": used,
                "ending_durability": running,
                "used_repairs": set(),
                "repair_gain": 0,
                "segments": segments,
            })
        return records

    def _route_upper_bound(self, path, active_goods):
        """
        코인 제한을 무시한 낙관적 이익.
        같은 섬·같은 상품 재고는 경로에서 재방문하더라도 한 번만 더한다.
        """
        total = 0
        for source_island_index in range(6):
            occurrence_indices = [
                visit for visit, island in enumerate(path[:-1])
                if island == source_island_index
            ]
            if not occurrence_indices:
                continue

            source_name = ISLANDS[source_island_index]
            for _global_index, good in active_goods:
                market = good.markets[source_name]
                if (
                    market.buy is None
                    or market.stock is None
                    or market.buy <= 0
                    or market.stock <= 0
                ):
                    continue

                best_unit_profit = 0
                for purchase_visit in occurrence_indices:
                    for sell_visit in range(purchase_visit + 1, len(path)):
                        sell = good.markets[ISLANDS[path[sell_visit]]].sell
                        if sell is not None:
                            best_unit_profit = max(
                                best_unit_profit,
                                int(sell) - int(market.buy),
                            )

                if best_unit_profit > 0:
                    total += best_unit_profit * int(market.stock)
        return total

    def _enumerate_routes(
        self,
        start,
        max_turns,
        matrix,
        active_goods,
        required_islands=None,
        forbidden_islands=None,
    ):
        """
        중복 방문을 허용하면서 이동 한도 안의 모든 경로를 생성한다.
        필수 방문 섬이 선택되어 있으면 그 섬을 모두 포함한 경로만
        실제 수익 최적화 대상으로 넘긴다.
        """
        required_islands = set(required_islands or ())
        forbidden_islands = set(forbidden_islands or ())

        if start in forbidden_islands:
            return [], 0

        routes = []
        stack = [([start], 0)]

        while stack:
            path, used = stack.pop()
            current = path[-1]

            for destination in range(6):
                if destination == current:
                    continue
                if destination in forbidden_islands:
                    continue

                distance = int(matrix[current][destination])
                new_used = used + distance
                if distance <= 0 or new_used > max_turns:
                    continue

                new_path = path + [destination]

                if required_islands.issubset(set(new_path)):
                    routes.append((new_path, new_used))

                # 아직 필수 섬을 모두 방문하지 않았더라도 뒤에서 방문할 수 있으므로
                # 이동 한도가 남아 있으면 계속 확장한다.
                stack.append((new_path, new_used))

        unique = {}
        for path, used in routes:
            key = tuple(path)
            previous = unique.get(key)
            if previous is None or used < previous[1]:
                unique[key] = (path, used)

        ranked = list(unique.values())
        ranked.sort(
            key=lambda item: (
                self._route_upper_bound(item[0], active_goods),
                -item[1],
            ),
            reverse=True,
        )
        return ranked, len(ranked)

    def _optimize_fixed_route(self, path, used_turns, start_cash, active_goods):
        """
        최대 수익 규칙

        q[a,b,g] = a번째 방문 섬에서 사서 b번째 방문 섬에서 파는 수량
        y[a,b,g] = 그 매입·판매 조합을 선택했는지 여부

        동일한 상품이라도 섬마다 재고가 별도이므로 다른 섬에서 각각 살 수 있다.
        다만 같은 '섬 + 상품' 재고는 전체 경로에서 한 번만 구매한다.

        따라서:
        - 같은 섬을 재방문해도 동일 재고를 다시 사지 않음
        - 한 섬의 같은 상품을 여러 판매처로 나눠 사지 않음
        - 다른 섬의 별도 재고는 추가 구매 가능
        - 중간 섬에서 판매한 돈으로 다른 섬 재고를 새로 구매 가능
        """
        variables = []
        for purchase_visit in range(len(path) - 1):
            source_island_index = path[purchase_visit]
            source_name = ISLANDS[source_island_index]

            for sale_visit in range(purchase_visit + 1, len(path)):
                destination_island_index = path[sale_visit]
                destination_name = ISLANDS[destination_island_index]

                for local_good, (_global_good, good) in enumerate(active_goods):
                    buy_market = good.markets[source_name]
                    sell_market = good.markets[destination_name]

                    if (
                        buy_market.buy is None
                        or buy_market.stock is None
                        or sell_market.sell is None
                        or buy_market.buy <= 0
                        or buy_market.stock <= 0
                        or sell_market.sell <= buy_market.buy
                    ):
                        continue

                    variables.append({
                        "a": purchase_visit,
                        "b": sale_visit,
                        "g": local_good,
                        "name": good.name,
                        "source": source_island_index,
                        "destination": destination_island_index,
                        "buy": int(buy_market.buy),
                        "sell": int(sell_market.sell),
                        "stock": int(buy_market.stock),
                        "unit_profit": int(sell_market.sell - buy_market.buy),
                    })

        if not variables:
            return {
                "path": path,
                "used": used_turns,
                "cash": start_cash,
                "profit": 0,
                "transactions": [],
                "visits": self._build_visit_cashflow(path, start_cash, []),
            }

        quantity_count = len(variables)
        binary_offset = quantity_count
        variable_count = quantity_count * 2

        objective = np.zeros(variable_count, dtype=float)
        for index, variable in enumerate(variables):
            objective[index] = -variable["unit_profit"]

        lower = np.zeros(variable_count, dtype=float)
        upper = np.zeros(variable_count, dtype=float)
        upper[:quantity_count] = np.array(
            [variable["stock"] for variable in variables],
            dtype=float,
        )
        upper[binary_offset:] = 1.0
        integrality = np.ones(variable_count, dtype=int)

        rows = []
        lower_bounds = []
        upper_bounds = []

        # 방문 시점별 보유 현금 제약:
        # 해당 섬에 도착하면 먼저 판매하고, 판매대금으로 새 상품을 구매할 수 있다.
        for visit in range(len(path)):
            row = np.zeros(variable_count, dtype=float)
            for index, variable in enumerate(variables):
                if variable["a"] <= visit:
                    row[index] += variable["buy"]
                if variable["b"] <= visit:
                    row[index] -= variable["sell"]
            rows.append(row)
            lower_bounds.append(-np.inf)
            upper_bounds.append(float(start_cash))

        # 수량 변수와 선택 변수 연결
        for index, variable in enumerate(variables):
            selection_index = binary_offset + index

            # q <= stock * y
            row = np.zeros(variable_count, dtype=float)
            row[index] = 1.0
            row[selection_index] = -float(variable["stock"])
            rows.append(row)
            lower_bounds.append(-np.inf)
            upper_bounds.append(0.0)

            # y <= q : 선택한 거래는 최소 1개 이상
            row = np.zeros(variable_count, dtype=float)
            row[index] = -1.0
            row[selection_index] = 1.0
            rows.append(row)
            lower_bounds.append(-np.inf)
            upper_bounds.append(0.0)

        # 핵심: 같은 섬의 같은 상품 재고는 경로 전체에서 한 거래만 선택.
        # 재방문해도 다시 살 수 없고, 여러 판매처로 분할 구매하지도 않는다.
        for source_island_index in range(6):
            for local_good, (_global_good, _good) in enumerate(active_goods):
                row = np.zeros(variable_count, dtype=float)
                found = False

                for index, variable in enumerate(variables):
                    if (
                        variable["source"] == source_island_index
                        and variable["g"] == local_good
                    ):
                        row[binary_offset + index] = 1.0
                        found = True

                if found:
                    rows.append(row)
                    lower_bounds.append(-np.inf)
                    upper_bounds.append(1.0)

        constraints = LinearConstraint(
            np.vstack(rows),
            np.array(lower_bounds, dtype=float),
            np.array(upper_bounds, dtype=float),
        )

        result = milp(
            c=objective,
            integrality=integrality,
            bounds=Bounds(lower, upper),
            constraints=constraints,
            options={
                "time_limit": 2.0,
                "mip_rel_gap": 0.0,
                "presolve": True,
            },
        )

        if result.x is None:
            return {
                "path": path,
                "used": used_turns,
                "cash": start_cash,
                "profit": 0,
                "transactions": [],
                "visits": self._build_visit_cashflow(path, start_cash, []),
            }

        transactions = []
        for variable, raw_quantity in zip(
            variables,
            result.x[:quantity_count],
        ):
            quantity = int(round(float(raw_quantity)))
            if quantity <= 0:
                continue

            spent = variable["buy"] * quantity
            revenue = variable["sell"] * quantity
            transactions.append({
                **variable,
                "qty": quantity,
                "spent": spent,
                "revenue": revenue,
                "profit": revenue - spent,
            })

        # 안전 검증: 동일 섬·동일 상품 구매는 최대 한 줄이어야 한다.
        purchase_keys = {}
        for transaction in transactions:
            key = (transaction["source"], transaction["g"])
            purchase_keys[key] = purchase_keys.get(key, 0) + 1

        if any(count > 1 for count in purchase_keys.values()):
            return {
                "path": path,
                "used": used_turns,
                "cash": start_cash,
                "profit": 0,
                "transactions": [],
                "visits": self._build_visit_cashflow(path, start_cash, []),
            }

        total_profit = sum(
            transaction["profit"] for transaction in transactions
        )
        transactions.sort(
            key=lambda transaction: (
                transaction["a"],
                transaction["b"],
                -transaction["profit"],
                transaction["name"],
            )
        )

        return {
            "path": path,
            "used": used_turns,
            "cash": start_cash + total_profit,
            "profit": total_profit,
            "transactions": transactions,
            "visits": self._build_visit_cashflow(
                path,
                start_cash,
                transactions,
            ),
        }

    @staticmethod
    def _build_visit_cashflow(path, start_cash, transactions):
        cash = start_cash
        visits = []
        for visit, island in enumerate(path):
            sold = [t for t in transactions if t["b"] == visit]
            bought = [t for t in transactions if t["a"] == visit]
            revenue = sum(t["revenue"] for t in sold)
            spent = sum(t["spent"] for t in bought)
            cash = cash + revenue - spent
            visits.append({
                "visit": visit,
                "island": island,
                "sold": sold,
                "bought": bought,
                "revenue": revenue,
                "spent": spent,
                "cash": cash,
            })
        return visits

    def calculate_routes(self):
        try:
            cash = int(self.cash_var.get().replace(",", "").strip())
            turns = int(self.turn_var.get().strip())
            durability = int(self.durability_var.get().replace(",", "").strip())
            max_durability = int(
                self.max_durability_var.get().replace(",", "").strip()
            )
            start = ISLANDS.index(self.start_island_var.get())
            if (
                cash < 0
                or turns < 0
                or durability < 0
                or max_durability < durability
            ):
                raise ValueError
        except Exception:
            messagebox.showerror(
                "입력 오류",
                "코인·턴·내구도는 0 이상의 정수로 입력하고, 최대 내구도는 현재 내구도 이상으로 입력하세요.",
            )
            return

        objective = self.objective_var.get()
        if objective == "균형형":
            objective = "자동 균형"
        if objective not in ("이번 회차 코인 최대", "자동 균형", "종료 내구도 우선"):
            objective = "이번 회차 코인 최대"
            self.objective_var.set(objective)

        self.config_data["max_durability"] = max_durability
        self.config_data["route_objective"] = objective
        hex_map = normalize_hex_map(self.config_data.get("hex_map"))
        hex_map["enabled"] = bool(self.hex_map_enabled_var.get())
        self.config_data["hex_map"] = hex_map
        self.save_config()

        active_goods = [
            (idx, good) for idx, good in enumerate(self.goods) if good.active
        ]
        if not active_goods:
            messagebox.showinfo("계산 불가", "동선 계산에 사용할 상품이 없습니다.")
            return

        if turns <= 0:
            messagebox.showinfo("계산 결과", "남은 턴이 없어 이동할 수 없습니다.")
            return

        price_max = int(self.config_data.get("ocr_price_max", 9999))
        suspicious = []
        for _idx, good in active_goods:
            for island in ISLANDS:
                market = good.markets[island]
                for label, value in (("구매", market.buy), ("판매", market.sell)):
                    if value is not None and value > price_max:
                        suspicious.append(f"{good.name}/{island}/{label} {value:,}")
        if suspicious:
            sample = "\n".join(suspicious[:8])
            if not messagebox.askyesno(
                "가격 이상치 확인",
                f"OCR 가격 상한 {price_max:,}보다 큰 값이 있습니다.\n\n{sample}\n\n그대로 계산할까요?",
            ):
                return

        required_indices = self.selected_required_island_indices()
        forbidden_indices = self.selected_forbidden_island_indices()
        required_names = [
            island for island in ISLANDS if island in self.required_islands
        ]
        forbidden_names = [
            island for island in ISLANDS if island in self.forbidden_islands
        ]

        if start in forbidden_indices:
            messagebox.showinfo(
                "방문 조건 충돌",
                f"현재 위치인 '{ISLANDS[start]}'이 방문 금지로 선택되어 있습니다.\n"
                "현재 위치의 방문 금지를 해제하거나 현재 위치를 바꾸세요.",
            )
            return
        if required_indices & forbidden_indices:
            messagebox.showinfo(
                "방문 조건 충돌",
                "같은 섬을 필수 방문과 방문 금지에 동시에 선택할 수 없습니다.",
            )
            return

        use_hex_map = bool(hex_map.get("enabled"))
        if use_hex_map:
            self.status_var.set(
                "육각 맵의 장애물·수리 타일을 반영해 실제 이동 경로를 계산 중입니다..."
            )
            self.update_idletasks()
            route_records, total_route_count, route_error = self._enumerate_hex_routes(
                start,
                turns,
                durability,
                max_durability,
                active_goods,
                required_indices,
                forbidden_indices,
            )
            max_turns = turns
            if route_error:
                messagebox.showerror("육각 맵 설정 오류", route_error)
                return
        else:
            max_turns = min(turns, durability // 100)
            if max_turns <= 0:
                messagebox.showinfo(
                    "계산 결과",
                    "현재 내구도로 1칸도 이동할 수 없습니다. 육각 맵 수리 경로를 사용하려면 육각 맵 경로 사용을 켜세요.",
                )
                return
            self.status_var.set(
                "고정 거리표 기준으로 방문 조건을 만족하는 최대 수익 경로를 계산 중입니다..."
            )
            self.update_idletasks()
            matrix = self.get_current_distance_matrix()
            fixed_routes, total_route_count = self._enumerate_routes(
                start,
                max_turns,
                matrix,
                active_goods,
                required_indices,
                forbidden_indices,
            )
            route_records = self._fixed_route_records(fixed_routes, durability)

        if not route_records:
            condition_lines = []
            if required_names:
                condition_lines.append("필수 방문: " + " · ".join(required_names))
            if forbidden_names:
                condition_lines.append("방문 금지: " + " · ".join(forbidden_names))
            mode_line = "육각 맵의 장애물과 내구도 조건" if use_hex_map else "고정 거리표 조건"
            messagebox.showinfo(
                "조건을 만족하는 경로 없음",
                f"{mode_line}에서 현재 위치·남은 턴·내구도로 가능한 경로가 없습니다.\n\n"
                + ("\n".join(condition_lines) if condition_lines else "방문 조건 없음"),
            )
            return

        results = []
        for index, route in enumerate(route_records, 1):
            result = self._optimize_fixed_route(
                route["path"], route["used"], cash, active_goods
            )
            result["ending_durability"] = route["ending_durability"]
            result["repair_gain"] = route.get("repair_gain", 0)
            result["segments"] = route.get("segments", [])
            result["net_durability_change"] = route["ending_durability"] - durability
            result["map_mode"] = "육각 맵" if use_hex_map else "고정 거리표"
            results.append(result)
            if index % 25 == 0:
                self.status_var.set(f"최적 거래 조합 계산 중 {index}/{len(route_records)}")
                self.update_idletasks()

        move_cost = (
            int(hex_map.get("move_durability_cost", 100))
            if use_hex_map
            else 100
        )
        auto_model = self._estimate_auto_balance_model(
            results,
            move_cost,
            max_durability,
            self.config_data,
        )

        ranked_results = results
        if objective == "자동 균형":
            profitable_results = [
                result for result in results
                if int(result.get("profit", 0)) > 0 and result.get("transactions")
            ]
            if profitable_results:
                ranked_results = profitable_results

        ranked_results.sort(
            key=lambda result: self._route_sort_key(
                result, objective, auto_model
            ),
            reverse=True,
        )
        best = []
        seen = set()
        for result in ranked_results:
            key = self._movement_route_key(result)
            if key in seen:
                continue
            seen.add(key)
            best.append(result)
            if len(best) >= 5:
                break

        self.result.delete("1.0", "end")
        required_text = " · ".join(required_names) if required_names else "없음"
        forbidden_text = " · ".join(forbidden_names) if forbidden_names else "없음"
        mode_text = "육각 맵 실제 경로" if use_hex_map else "고정 거리표"
        self.result.insert(
            "end",
            f"계산 상품 {len(active_goods)}종 · 남은 턴 {turns} · 시작 내구도 {durability:,}/{max_durability:,} "
            f"· 시작 코인 {cash:,}\n"
            f"경로 계산: {mode_text} · 계산 기준: {objective}\n"
            f"필수 방문 섬: {required_text}\n"
            f"방문 금지 섬: {forbidden_text}\n"
            f"조건 충족 경로 {len(route_records)}개 / 생성 상태 {total_route_count}개\n",
        )
        if use_hex_map:
            self.result.insert(
                "end",
                f"수리 타일 설정: 1회 +{hex_map['repair_value']:,} · 이동 1칸 -{hex_map['move_durability_cost']:,} "
                f"· 같은 타일 재수리 {'허용' if hex_map.get('repair_repeatable') else '1회만'}\n",
            )
        if objective == "자동 균형":
            base_move_value = auto_model["base_profit_per_move"]
            start_marginal_100 = self._auto_marginal_durability_value(
                durability,
                auto_model,
                100,
            )
            discount_percent = auto_model["future_discount"] * 100
            self.auto_balance_hint_var.set(
                f"현재 100 ≈ {start_marginal_100:,.0f}코인"
            )
            self.result.insert(
                "end",
                f"자동 균형 산정: 현재 가격·재고·보유 코인에서 이동 1칸 기대수익을 "
                f"약 {base_move_value:,.0f}코인으로 추정\n"
                f"내구도는 {auto_model['cycle_turns']}칸 단위의 미래 무역 회차로 평가하며, "
                f"현재 경로들의 수익 편차를 바탕으로 다음 회차 구간 가치를 이전 구간의 "
                f"{discount_percent:.0f}%로 자동 산정\n"
                f"현재 내구도에서 마지막 100의 추정 가치: 약 {start_marginal_100:,.0f}코인\n",
            )
        else:
            self.auto_balance_hint_var.set("시장·잔여량으로 자동 산정")
        self.result.insert(
            "end",
            "\n계산 방식: 장애물은 통과하지 않고, 수리 타일은 실제 방문 순서대로 적용합니다. "
            "자동 균형은 현재 시장의 실제 수익성과 남은 내구도의 미래 사용 시점을 함께 계산합니다. "
            "섬마다 별도 재고를 사용하며 같은 섬의 같은 상품 재고는 경로 전체에서 한 번만 구매합니다.\n\n",
        )

        for rank, result in enumerate(best, 1):
            net_durability = result["net_durability_change"]
            net_text = f"+{net_durability:,}" if net_durability >= 0 else f"{net_durability:,}"
            self.result.insert(
                "end",
                f"[{rank}위] 최종 {result['cash']:,}코인 · 순이익 +{result['profit']:,} "
                f"· {result['used']}턴 사용 · 종료 내구도 {result['ending_durability']:,}/{max_durability:,} "
                f"(변화 {net_text}, 수리 +{result.get('repair_gain', 0):,})\n",
            )
            if objective == "자동 균형":
                self.result.insert(
                    "end",
                    f"  자동 균형점수 {result.get('strategy_score', 0):,.0f} "
                    f"(종료 내구도 미래가치 {result.get('durability_utility', 0):,.0f}, "
                    f"마지막 100 한계가치 약 {result.get('marginal_durability_value_100', 0):,.0f})\n",
                )
            self.result.insert(
                "end", " → ".join(ISLANDS[index] for index in result["path"]) + "\n"
            )

            for segment_index, segment in enumerate(result.get("segments", []), 1):
                repairs = segment.get("repairs", [])
                repair_count = len(repairs)
                net = int(segment.get("net_durability_change", 0))
                net_segment_text = f"+{net:,}" if net >= 0 else f"{net:,}"
                self.result.insert(
                    "end",
                    f"  이동 {segment_index}: {ISLANDS[segment['source']]} → {ISLANDS[segment['destination']]} "
                    f"· {segment['turns']}턴 · 이동 -{segment.get('durability_spent', 0):,} "
                    f"· 수리 +{segment.get('repair_gain', 0):,} · 순변화 {net_segment_text} "
                    f"· 도착 내구도 {segment.get('ending_durability', 0):,}",
                )
                if repair_count:
                    repair_cells = ", ".join(
                        f"({item['cell'][0]},{item['cell'][1]}) +{item['actual']:,}"
                        for item in repairs
                    )
                    self.result.insert("end", f" · 수리 {repair_count}회 [{repair_cells}]")
                self.result.insert("end", "\n")
                tile_path = segment.get("tile_path", [])
                if tile_path:
                    path_text = " → ".join(
                        f"({cell[0]},{cell[1]})" for cell in tile_path
                    )
                    self.result.insert("end", f"     타일 경로: {path_text}\n")

            for visit in result["visits"]:
                self.result.insert(
                    "end",
                    f"  [{visit['visit'] + 1}] {ISLANDS[visit['island']]}: "
                    f"판매수입 +{visit['revenue']:,} / 구매지출 -{visit['spent']:,} "
                    f"/ 남은 코인 {visit['cash']:,}\n",
                )
                for sale in visit["sold"]:
                    self.result.insert(
                        "end",
                        f"     판매 · {sale['name']} {sale['qty']}개 "
                        f"({ISLANDS[sale['source']]}에서 {sale['buy']:,}에 매입) "
                        f"→ {sale['sell']:,}에 판매 | 판매금액 {sale['revenue']:,} "
                        f"| 순이익 +{sale['profit']:,}\n",
                    )
                for buy in visit["bought"]:
                    self.result.insert(
                        "end",
                        f"     구매 · {buy['name']} {buy['qty']}개 × {buy['buy']:,} "
                        f"= 지출 {buy['spent']:,} | {ISLANDS[buy['destination']]}에서 판매 예정 "
                        f"({buy['sell']:,}, 예상 순이익 +{buy['profit']:,})\n",
                    )
            self.result.insert("end", "\n")

        self.status_var.set(
            f"계산 완료: {mode_text}에서 {len(route_records)}개 이동 경로와 거래 조합을 비교했습니다."
        )

def main():
    try:
        pyautogui.PAUSE = 0.08
        app = TradePlannerApp()
        app.mainloop()
    except Exception:
        ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
        raise

if __name__ == "__main__":
    main()
