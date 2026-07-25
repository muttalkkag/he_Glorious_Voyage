from __future__ import annotations

import copy
import json
import math
from collections import deque
from pathlib import Path
from typing import Callable, Iterable, Optional

import tkinter as tk
from tkinter import ttk, messagebox

MAP_SLOT_LABELS = [
    "북서쪽",
    "북동쪽",
    "서쪽",
    "중앙",
    "동쪽",
    "남쪽",
]

DEFAULT_HEX_MAP = {
    "enabled": False,
    "orientation": "pointy_top_odd_r",
    "cols": 11,
    "rows": 9,
    "blocked": [],
    "repairs": [],
    "slots": {
        "북서쪽": [1, 2],
        "북동쪽": [8, 2],
        "서쪽": [1, 5],
        "중앙": [5, 5],
        "동쪽": [9, 5],
        "남쪽": [5, 8],
    },
    "repair_value": 450,
    "move_durability_cost": 100,
    "repair_repeatable": False,
    "max_pair_options": 12,
}


def cell_key(cell: tuple[int, int] | list[int]) -> str:
    return f"{int(cell[0])},{int(cell[1])}"


def parse_cell(value) -> Optional[tuple[int, int]]:
    try:
        if isinstance(value, str):
            left, right = value.split(",", 1)
            return int(left), int(right)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return int(value[0]), int(value[1])
    except Exception:
        return None
    return None


def normalize_hex_map(value) -> dict:
    """구버전 또는 손상된 설정을 안전한 육각 맵 설정으로 정리한다."""
    source = value if isinstance(value, dict) else {}
    result = copy.deepcopy(DEFAULT_HEX_MAP)

    try:
        result["enabled"] = bool(source.get("enabled", result["enabled"]))
        # 게임 화면과 같은 꼭짓점 위·아래(pointy-top), 홀수 행 오른쪽 이동(odd-r) 격자
        result["orientation"] = "pointy_top_odd_r"
        result["cols"] = min(20, max(4, int(source.get("cols", result["cols"]))))
        result["rows"] = min(20, max(4, int(source.get("rows", result["rows"]))))
        result["repair_value"] = min(
            9999, max(0, int(source.get("repair_value", result["repair_value"])))
        )
        result["move_durability_cost"] = min(
            9999,
            max(1, int(source.get("move_durability_cost", result["move_durability_cost"]))),
        )
        result["repair_repeatable"] = bool(
            source.get("repair_repeatable", result["repair_repeatable"])
        )
        result["max_pair_options"] = min(
            64, max(2, int(source.get("max_pair_options", result["max_pair_options"])))
        )
    except Exception:
        pass

    cols = result["cols"]
    rows = result["rows"]

    def valid(cell: tuple[int, int]) -> bool:
        return 0 <= cell[0] < cols and 0 <= cell[1] < rows

    blocked = set()
    for raw in source.get("blocked", []):
        cell = parse_cell(raw)
        if cell is not None and valid(cell):
            blocked.add(cell)

    repairs = set()
    for raw in source.get("repairs", []):
        cell = parse_cell(raw)
        if cell is not None and valid(cell):
            repairs.add(cell)

    source_slots = source.get("slots", {})
    slots: dict[str, list[int]] = {}
    used = set()
    for label in MAP_SLOT_LABELS:
        cell = parse_cell(source_slots.get(label)) if isinstance(source_slots, dict) else None
        if cell is None or not valid(cell) or cell in used:
            default_cell = parse_cell(DEFAULT_HEX_MAP["slots"][label])
            if default_cell is not None:
                default_cell = (
                    min(cols - 1, max(0, default_cell[0])),
                    min(rows - 1, max(0, default_cell[1])),
                )
            cell = default_cell
        if cell is not None and cell not in used:
            slots[label] = [cell[0], cell[1]]
            used.add(cell)

    # 작은 격자로 바꿔 기본 위치가 겹친 경우, 남는 칸을 순서대로 배정한다.
    if len(slots) < len(MAP_SLOT_LABELS):
        available = [
            (col, row)
            for row in range(rows)
            for col in range(cols)
            if (col, row) not in used
        ]
        for label in MAP_SLOT_LABELS:
            if label not in slots and available:
                cell = available.pop(0)
                slots[label] = [cell[0], cell[1]]
                used.add(cell)

    slot_cells = {tuple(value) for value in slots.values()}
    blocked -= slot_cells
    repairs -= slot_cells
    repairs -= blocked

    result["blocked"] = sorted(cell_key(cell) for cell in blocked)
    result["repairs"] = sorted(cell_key(cell) for cell in repairs)
    result["slots"] = slots
    return result


def hex_neighbors(cell: tuple[int, int], cols: int, rows: int) -> list[tuple[int, int]]:
    """게임 화면형 pointy-top odd-r 육각 격자의 이웃.

    꼭짓점이 위·아래를 향하고, 홀수 행이 오른쪽으로 반 칸 이동한다.
    예를 들어 짝수 행의 (4, 4)는 다음 6칸과 맞닿는다.
    (3,3), (4,3), (3,4), (5,4), (3,5), (4,5)
    """
    col, row = cell

    if row % 2 == 0:
        # 짝수 행: 위·아래 대각선이 왼쪽으로 한 칸 치우친다.
        offsets = [
            (1, 0), (-1, 0),
            (0, -1), (-1, -1),
            (0, 1), (-1, 1),
        ]
    else:
        # 홀수 행: 행 자체가 오른쪽으로 반 칸 이동한다.
        offsets = [
            (1, 0), (-1, 0),
            (1, -1), (0, -1),
            (1, 1), (0, 1),
        ]

    result = []
    for dc, dr in offsets:
        nc, nr = col + dc, row + dr
        if 0 <= nc < cols and 0 <= nr < rows:
            result.append((nc, nr))
    return result


def _repair_index_map(hex_map: dict) -> dict[tuple[int, int], int]:
    repairs = sorted(parse_cell(value) for value in hex_map.get("repairs", []))
    clean = [cell for cell in repairs if cell is not None]
    return {cell: index for index, cell in enumerate(clean)}


def build_pair_path_options(
    hex_map: dict,
    start_cell: tuple[int, int],
    end_cell: tuple[int, int],
    max_turns: int,
    other_slot_cells: Iterable[tuple[int, int]] = (),
) -> list[dict]:
    """
    두 섬 사이의 후보 경로를 만든다.

    동일한 수리 타일 조합을 지나는 경로에서는 가장 짧은 경로만 유지한다.
    다른 섬 타일은 중간 통과하지 않도록 막아, 섬 방문 순서와 실제 이동이 일치한다.
    """
    config = normalize_hex_map(hex_map)
    cols, rows = config["cols"], config["rows"]
    blocked = {parse_cell(value) for value in config.get("blocked", [])}
    blocked.discard(None)
    repair_indices = _repair_index_map(config)

    forbidden = set(other_slot_cells)
    forbidden.discard(start_cell)
    forbidden.discard(end_cell)
    blocked |= forbidden

    if start_cell in blocked or end_cell in blocked:
        return []

    start_mask = 0
    if start_cell in repair_indices:
        start_mask |= 1 << repair_indices[start_cell]

    queue = deque([(start_cell, start_mask)])
    best_steps: dict[tuple[tuple[int, int], int], int] = {(start_cell, start_mask): 0}
    parents: dict[
        tuple[tuple[int, int], int], Optional[tuple[tuple[int, int], int]]
    ] = {(start_cell, start_mask): None}

    end_states: list[tuple[tuple[int, int], int]] = []

    while queue:
        cell, mask = queue.popleft()
        state = (cell, mask)
        steps = best_steps[state]
        if cell == end_cell and steps > 0:
            end_states.append(state)
        if steps >= max_turns:
            continue

        for neighbor in hex_neighbors(cell, cols, rows):
            if neighbor in blocked:
                continue
            next_mask = mask
            if neighbor in repair_indices:
                next_mask |= 1 << repair_indices[neighbor]
            next_state = (neighbor, next_mask)
            next_steps = steps + 1
            previous = best_steps.get(next_state)
            if previous is not None and previous <= next_steps:
                continue
            best_steps[next_state] = next_steps
            parents[next_state] = state
            queue.append(next_state)

    by_mask: dict[int, dict] = {}
    for state in end_states:
        cell, mask = state
        steps = best_steps[state]
        if steps <= 0 or steps > max_turns:
            continue
        old = by_mask.get(mask)
        if old is not None and old["steps"] <= steps:
            continue

        path = []
        cursor: Optional[tuple[tuple[int, int], int]] = state
        while cursor is not None:
            path.append(cursor[0])
            cursor = parents[cursor]
        path.reverse()

        ordered_repairs = [cell for cell in path[1:] if cell in repair_indices]
        by_mask[mask] = {
            "steps": steps,
            "path": path,
            "repair_mask": mask,
            "repair_cells": ordered_repairs,
        }

    options = list(by_mask.values())
    options.sort(
        key=lambda option: (
            option["steps"],
            -len(set(option["repair_cells"])),
            len(option["path"]),
        )
    )

    # 가장 짧은 경로와 수리 타일 수가 많은 경로를 모두 남긴다.
    max_options = int(config.get("max_pair_options", 12))
    if len(options) > max_options:
        selected = []
        seen = set()

        def add(option):
            key = (option["steps"], option["repair_mask"])
            if key not in seen and len(selected) < max_options:
                selected.append(option)
                seen.add(key)

        for option in options[: max(2, max_options // 2)]:
            add(option)
        for option in sorted(
            options,
            key=lambda item: (-len(set(item["repair_cells"])), item["steps"]),
        ):
            add(option)
        options = sorted(selected, key=lambda item: (item["steps"], -len(item["repair_cells"])))

    return options


def simulate_hex_path(
    option: dict,
    current_durability: int,
    max_durability: int,
    hex_map: dict,
    used_repairs: Optional[set[tuple[int, int]]] = None,
) -> Optional[dict]:
    """한 구간을 이동하며 내구도 소모와 수리를 실제 순서대로 적용한다."""
    config = normalize_hex_map(hex_map)
    move_cost = int(config.get("move_durability_cost", 100))
    repair_value = int(config.get("repair_value", 450))
    repeatable = bool(config.get("repair_repeatable", False))
    repair_cells = {parse_cell(value) for value in config.get("repairs", [])}
    repair_cells.discard(None)

    durability = int(current_durability)
    maximum = max(0, int(max_durability))
    used = set(used_repairs or ())
    repair_gain = 0
    triggered = []

    path = [tuple(cell) for cell in option.get("path", [])]
    if len(path) < 2:
        return None

    for cell in path[1:]:
        durability -= move_cost
        if durability < 0:
            return None

        if cell in repair_cells and (repeatable or cell not in used):
            before = durability
            durability = min(maximum, durability + repair_value)
            actual_gain = max(0, durability - before)
            repair_gain += actual_gain
            triggered.append({
                "cell": cell,
                "configured": repair_value,
                "actual": actual_gain,
            })
            if not repeatable:
                used.add(cell)

    return {
        "ending_durability": durability,
        "repair_gain": repair_gain,
        "repairs": triggered,
        "used_repairs": used,
        "durability_spent": (len(path) - 1) * move_cost,
        "net_durability_change": durability - int(current_durability),
    }


class HexMapEditor(tk.Toplevel):
    """수동 육각 맵 편집기."""

    MODE_LABELS = ["바다", "통행 불가", "수리"] + MAP_SLOT_LABELS

    def __init__(
        self,
        master,
        config: dict,
        on_save: Callable[[dict], None],
    ):
        super().__init__(master)
        self.title("육각 맵 · 장애물 · 수리 구역 편집")
        self.geometry("1280x820")
        self.minsize(980, 680)
        self.transient(master)
        self.grab_set()

        self.on_save = on_save
        self.config_value = normalize_hex_map(config)
        self.cols_var = tk.StringVar(value=str(self.config_value["cols"]))
        self.rows_var = tk.StringVar(value=str(self.config_value["rows"]))
        self.repair_value_var = tk.StringVar(value=str(self.config_value["repair_value"]))
        self.move_cost_var = tk.StringVar(value=str(self.config_value["move_durability_cost"]))
        self.repeatable_var = tk.BooleanVar(value=bool(self.config_value["repair_repeatable"]))
        self.enabled_var = tk.BooleanVar(value=bool(self.config_value["enabled"]))
        self.mode_var = tk.StringVar(value="통행 불가")
        self.status_var = tk.StringVar(value="게임 화면과 같은 세로 육각형입니다. 왼쪽 클릭으로 배치하고, 오른쪽 클릭은 바다로 되돌립니다.")

        self.blocked = {parse_cell(value) for value in self.config_value["blocked"]}
        self.blocked.discard(None)
        self.repairs = {parse_cell(value) for value in self.config_value["repairs"]}
        self.repairs.discard(None)
        self.slots = {
            label: parse_cell(cell)
            for label, cell in self.config_value["slots"].items()
        }

        self.cell_items: dict[int, tuple[int, int]] = {}
        self.cell_polygons: dict[tuple[int, int], int] = {}
        self.cell_texts: dict[tuple[int, int], list[int]] = {}

        self._build_ui()
        self._redraw()

    def _build_ui(self):
        settings = ttk.Frame(self, padding=8)
        settings.pack(fill="x")

        ttk.Checkbutton(
            settings,
            text="육각 맵 경로 계산 사용",
            variable=self.enabled_var,
        ).pack(side="left", padx=4)

        ttk.Label(
            settings,
            text="타일 방향: 꼭짓점 위·아래 (게임 화면형)",
        ).pack(side="left", padx=(10, 4))

        ttk.Label(settings, text="열").pack(side="left", padx=(14, 2))
        ttk.Entry(settings, textvariable=self.cols_var, width=5).pack(side="left")
        ttk.Label(settings, text="행").pack(side="left", padx=(8, 2))
        ttk.Entry(settings, textvariable=self.rows_var, width=5).pack(side="left")
        ttk.Button(settings, text="격자 크기 적용", command=self._resize_grid).pack(side="left", padx=6)

        ttk.Label(settings, text="수리량").pack(side="left", padx=(16, 2))
        ttk.Entry(settings, textvariable=self.repair_value_var, width=7).pack(side="left")
        ttk.Label(settings, text="이동 1칸 내구도").pack(side="left", padx=(10, 2))
        ttk.Entry(settings, textvariable=self.move_cost_var, width=7).pack(side="left")
        ttk.Checkbutton(
            settings,
            text="같은 수리 타일 재방문 시 다시 수리",
            variable=self.repeatable_var,
        ).pack(side="left", padx=10)

        modes = ttk.Labelframe(self, text="클릭 도구", padding=6)
        modes.pack(fill="x", padx=8, pady=(0, 5))
        for label in self.MODE_LABELS:
            ttk.Radiobutton(
                modes,
                text=label,
                variable=self.mode_var,
                value=label,
            ).pack(side="left", padx=4)

        ttk.Label(
            modes,
            text="섬 위치는 물리적 위치 이름으로 지정하고, 실제 섬 이름은 기존 '이번 판 섬 배치 설정'에서 바꿉니다.",
        ).pack(side="right", padx=6)

        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(fill="both", expand=True, padx=8, pady=4)

        self.canvas = tk.Canvas(canvas_frame, background="#0b6f9b", highlightthickness=0)
        xscroll = ttk.Scrollbar(canvas_frame, orient="horizontal", command=self.canvas.xview)
        yscroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)

        xscroll.pack(side="bottom", fill="x")
        yscroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<Button-1>", self._on_left_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Configure>", lambda _event: self._redraw())

        ttk.Label(self, textvariable=self.status_var, padding=(10, 3)).pack(fill="x")

        buttons = ttk.Frame(self, padding=8)
        buttons.pack(fill="x")
        ttk.Button(
            buttons,
            text="통행 불가·수리 타일 초기화",
            command=self._clear_dynamic,
        ).pack(side="left", padx=4)
        ttk.Button(buttons, text="전체 기본값", command=self._reset_default).pack(side="left", padx=4)
        ttk.Button(buttons, text="저장", command=self._save).pack(side="right", padx=4)
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="right", padx=4)

    def _grid_dimensions(self) -> tuple[int, int]:
        try:
            cols = min(20, max(4, int(self.cols_var.get())))
            rows = min(20, max(4, int(self.rows_var.get())))
            return cols, rows
        except Exception:
            return self.config_value["cols"], self.config_value["rows"]

    def _geometry(self):
        cols, rows = self._grid_dimensions()
        canvas_width = max(800, self.canvas.winfo_width())
        canvas_height = max(560, self.canvas.winfo_height())

        # pointy-top: 가로 간격 sqrt(3)*size, 세로 간격 1.5*size
        # 홀수 행은 오른쪽으로 가로 간격의 절반만큼 이동한다.
        max_size_x = (canvas_width - 100) / max(
            2.0,
            math.sqrt(3) * (cols + 0.5),
        )
        max_size_y = (canvas_height - 100) / max(
            2.0,
            1.5 * max(1, rows - 1) + 2.0,
        )
        size = max(18.0, min(42.0, max_size_x, max_size_y))
        margin = size + 30
        width = (
            margin * 2
            + math.sqrt(3) * size * max(0, cols - 1)
            + math.sqrt(3) * size * 0.5
            + math.sqrt(3) * size
        )
        height = margin * 2 + 1.5 * size * max(0, rows - 1) + 2 * size
        return cols, rows, size, margin, width, height

    @staticmethod
    def _points(cx: float, cy: float, size: float):
        # 30도부터 시작하면 꼭짓점이 위·아래를 향하는 pointy-top 육각형이 된다.
        points = []
        for index in range(6):
            angle = math.radians(30 + 60 * index)
            points.extend([cx + size * math.cos(angle), cy + size * math.sin(angle)])
        return points

    def _center(self, col: int, row: int, size: float, margin: float):
        horizontal = math.sqrt(3) * size
        cx = margin + horizontal * (col + 0.5 * (row % 2))
        cy = margin + 1.5 * size * row
        return cx, cy

    def _redraw(self):
        if not hasattr(self, "canvas"):
            return
        self.canvas.delete("all")
        self.cell_items.clear()
        self.cell_polygons.clear()
        self.cell_texts.clear()

        cols, rows, size, margin, width, height = self._geometry()
        self.canvas.configure(scrollregion=(0, 0, width, height))

        slot_at = {cell: label for label, cell in self.slots.items() if cell is not None}
        short_labels = {
            "북서쪽": "북서",
            "북동쪽": "북동",
            "서쪽": "서",
            "중앙": "중앙",
            "동쪽": "동",
            "남쪽": "남",
        }

        for row in range(rows):
            for col in range(cols):
                cell = (col, row)
                cx, cy = self._center(col, row, size, margin)
                fill = "#20b4e6"
                outline = "#b9f5ff"
                text = ""
                text_fill = "#071820"

                if cell in self.blocked:
                    fill = "#273448"
                    outline = "#141b27"
                    text = "막힘"
                    text_fill = "white"
                elif cell in self.repairs:
                    fill = "#7adf79"
                    outline = "#185c25"
                    text = f"수리\n+{self.repair_value_var.get()}"
                elif cell in slot_at:
                    fill = "#ffd166"
                    outline = "#784f00"
                    text = short_labels[slot_at[cell]]

                item = self.canvas.create_polygon(
                    *self._points(cx, cy, size - 1),
                    fill=fill,
                    outline=outline,
                    width=2,
                )
                self.cell_items[item] = cell
                self.cell_polygons[cell] = item

                label_items = []
                if text:
                    label_items.append(
                        self.canvas.create_text(
                            cx,
                            cy,
                            text=text,
                            fill=text_fill,
                            font=("맑은 고딕", max(8, int(size / 3.3)), "bold"),
                            justify="center",
                        )
                    )
                label_items.append(
                    self.canvas.create_text(
                        cx,
                        cy + size * 0.62,
                        text=f"{col},{row}",
                        fill="#07384c",
                        font=("Arial", max(6, int(size / 5))),
                    )
                )
                for text_item in label_items:
                    self.cell_items[text_item] = cell
                self.cell_texts[cell] = label_items

    def _cell_from_event(self, event) -> Optional[tuple[int, int]]:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        items = self.canvas.find_overlapping(x - 2, y - 2, x + 2, y + 2)
        for item in reversed(items):
            cell = self.cell_items.get(item)
            if cell is not None:
                return cell
        return None

    def _set_cell(self, cell: tuple[int, int], mode: str):
        if mode == "바다":
            self.blocked.discard(cell)
            self.repairs.discard(cell)
            for label, assigned in list(self.slots.items()):
                if assigned == cell:
                    self.slots[label] = None
        elif mode == "통행 불가":
            self.blocked.add(cell)
            self.repairs.discard(cell)
            for label, assigned in list(self.slots.items()):
                if assigned == cell:
                    self.slots[label] = None
        elif mode == "수리":
            self.repairs.add(cell)
            self.blocked.discard(cell)
            for label, assigned in list(self.slots.items()):
                if assigned == cell:
                    self.slots[label] = None
        elif mode in MAP_SLOT_LABELS:
            self.blocked.discard(cell)
            self.repairs.discard(cell)
            for label, assigned in list(self.slots.items()):
                if assigned == cell:
                    self.slots[label] = None
            self.slots[mode] = cell
        self._redraw()

    def _on_left_click(self, event):
        cell = self._cell_from_event(event)
        if cell is None:
            return
        self._set_cell(cell, self.mode_var.get())
        self.status_var.set(f"{cell[0]},{cell[1]} → {self.mode_var.get()}")

    def _on_right_click(self, event):
        cell = self._cell_from_event(event)
        if cell is None:
            return
        self._set_cell(cell, "바다")
        self.status_var.set(f"{cell[0]},{cell[1]} → 바다")

    def _resize_grid(self):
        try:
            cols = min(20, max(4, int(self.cols_var.get())))
            rows = min(20, max(4, int(self.rows_var.get())))
        except Exception:
            messagebox.showerror("입력 오류", "행과 열은 4~20 사이 정수로 입력하세요.", parent=self)
            return
        self.cols_var.set(str(cols))
        self.rows_var.set(str(rows))

        valid = lambda cell: cell is not None and 0 <= cell[0] < cols and 0 <= cell[1] < rows
        self.blocked = {cell for cell in self.blocked if valid(cell)}
        self.repairs = {cell for cell in self.repairs if valid(cell)}
        for label, cell in list(self.slots.items()):
            if not valid(cell):
                self.slots[label] = None
        self._redraw()

    def _clear_dynamic(self):
        """통행 불가와 수리 타일만 제거하고 나머지 맵 설정은 그대로 둔다."""
        blocked_count = len(self.blocked)
        repair_count = len(self.repairs)

        if blocked_count == 0 and repair_count == 0:
            self.status_var.set(
                "초기화할 통행 불가 또는 수리 타일이 없습니다. "
                "격자와 섬 위치는 그대로 유지됩니다."
            )
            return

        confirmed = messagebox.askyesno(
            "타일 초기화",
            "통행 불가 타일과 수리 타일만 모두 일반 바다로 되돌릴까요?\n\n"
            f"통행 불가: {blocked_count}개\n"
            f"수리 타일: {repair_count}개\n\n"
            "격자 크기, 6개 섬 위치, 수리량, 이동 내구도 설정은 유지됩니다.",
            parent=self,
        )
        if not confirmed:
            return

        self.blocked.clear()
        self.repairs.clear()
        self._redraw()
        self.status_var.set(
            "통행 불가·수리 타일만 초기화했습니다. "
            "격자와 섬 위치는 유지됩니다. 저장을 눌러 적용하세요."
        )

    def _reset_default(self):
        if not messagebox.askyesno("전체 초기화", "격자, 섬 위치, 장애물, 수리 설정을 모두 기본값으로 되돌릴까요?", parent=self):
            return
        value = normalize_hex_map(DEFAULT_HEX_MAP)
        self.config_value = value
        self.cols_var.set(str(value["cols"]))
        self.rows_var.set(str(value["rows"]))
        self.repair_value_var.set(str(value["repair_value"]))
        self.move_cost_var.set(str(value["move_durability_cost"]))
        self.repeatable_var.set(bool(value["repair_repeatable"]))
        self.enabled_var.set(bool(value["enabled"]))
        self.blocked = {parse_cell(item) for item in value["blocked"]}
        self.blocked.discard(None)
        self.repairs = {parse_cell(item) for item in value["repairs"]}
        self.repairs.discard(None)
        self.slots = {label: parse_cell(cell) for label, cell in value["slots"].items()}
        self._redraw()

    def _save(self):
        try:
            cols = min(20, max(4, int(self.cols_var.get())))
            rows = min(20, max(4, int(self.rows_var.get())))
            repair_value = int(self.repair_value_var.get().replace(",", "").strip())
            move_cost = int(self.move_cost_var.get().replace(",", "").strip())
            if not (0 <= repair_value <= 9999):
                raise ValueError("수리량")
            if not (1 <= move_cost <= 9999):
                raise ValueError("이동 내구도")

            missing = [label for label in MAP_SLOT_LABELS if self.slots.get(label) is None]
            if missing:
                messagebox.showerror(
                    "섬 위치 미지정",
                    "다음 물리적 섬 위치를 모두 타일에 지정하세요.\n\n" + " · ".join(missing),
                    parent=self,
                )
                return

            slot_values = list(self.slots.values())
            if len(set(slot_values)) != 6:
                messagebox.showerror("섬 위치 중복", "6개 물리적 섬 위치는 서로 다른 타일이어야 합니다.", parent=self)
                return

            value = {
                "enabled": bool(self.enabled_var.get()),
                "orientation": "pointy_top_odd_r",
                "cols": cols,
                "rows": rows,
                "blocked": sorted(cell_key(cell) for cell in self.blocked),
                "repairs": sorted(cell_key(cell) for cell in self.repairs),
                "slots": {
                    label: [self.slots[label][0], self.slots[label][1]]
                    for label in MAP_SLOT_LABELS
                },
                "repair_value": repair_value,
                "move_durability_cost": move_cost,
                "repair_repeatable": bool(self.repeatable_var.get()),
                "max_pair_options": int(self.config_value.get("max_pair_options", 12)),
            }
            value = normalize_hex_map(value)
            self.on_save(value)
            self.destroy()
        except ValueError as exc:
            messagebox.showerror(
                "입력 오류",
                f"수리량은 0~9,999, 이동 1칸 내구도는 1~9,999 사이 정수로 입력하세요.\n\n확인: {exc}",
                parent=self,
            )
