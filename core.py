"""Pure-python match-three engine. Independent of GUI/screenshot libraries."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

N = 8
COLORS = frozenset('YOGBPRW')
Board = tuple[tuple[str, ...], ...]
Cell = tuple[int, int]
MoveMode = Literal['speed', 'balanced', 'power']


@dataclass(frozen=True)
class Move:
    src: Cell
    dst: Cell
    score: float
    removed: int
    kind: str


def as_board(rows) -> Board:
    result = tuple(tuple(row) for row in rows)
    if len(result) != N or any(len(row) != N for row in result):
        raise ValueError('board must be 8x8')
    return result


def runs(board: Board) -> list[frozenset[Cell]]:
    """Return each horizontal/vertical run >=3, excluding unknown/hypercube."""
    output = []
    for horizontal in (True, False):
        for fixed in range(N):
            i = 0
            while i < N:
                at = (fixed, i) if horizontal else (i, fixed)
                v = board[at[0]][at[1]]
                j = i + 1
                while j < N:
                    other = (fixed, j) if horizontal else (j, fixed)
                    if board[other[0]][other[1]] != v:
                        break
                    j += 1
                if v in COLORS and j - i >= 3:
                    output.append(frozenset((fixed, k) if horizontal else (k, fixed)
                                            for k in range(i, j)))
                i = j
    return output


def matches(board: Board) -> frozenset[Cell]:
    groups = runs(board)
    return frozenset().union(*groups) if groups else frozenset()


def swap_board(board: Board, a: Cell, b: Cell) -> Board:
    arr = [list(row) for row in board]
    arr[a[0]][a[1]], arr[b[0]][b[1]] = arr[b[0]][b[1]], arr[a[0]][a[1]]
    return as_board(arr)


def cascade_estimate(board: Board, cleared: frozenset[Cell]) -> int:
    """Look one gravity step ahead without guessing unknown incoming gems.

    This is only a heuristic: real cascades depend on random gems from above.
    """
    arr = [list(row) for row in board]
    for c in range(N):
        surviving = [board[r][c] for r in range(N - 1, -1, -1) if (r, c) not in cleared]
        for i in range(N):
            arr[N - 1 - i][c] = surviving[i] if i < len(surviving) else '?'
    return len(matches(as_board(arr)))


def move_value(board: Board, a: Cell, b: Cell,
               mode: MoveMode = 'balanced',
               specials: Mapping[Cell, str] | None = None) -> Move | None:
    if abs(a[0] - b[0]) + abs(a[1] - b[1]) != 1:
        return None
    x, y = board[a[0]][a[1]], board[b[0]][b[1]]
    if x == '?' or y == '?':
        return None
    # Two adjacent hypercubes are the one same-symbol swap that is legal.
    if x == 'H' or y == 'H':
        target = y if x == 'H' else x
        if target == 'H':
            return Move(a, b, 150.0, 64, 'two-hypercubes')
        count = sum(cell == target for row in board for cell in row)
        return Move(a, b, 32 + 3.4 * count, count, 'hypercube')
    if x == y:
        return None

    swapped = swap_board(board, a, b)
    groups = runs(swapped)
    hit = frozenset().union(*groups) if groups else frozenset()
    # Old matches from an unfinished animation must never authorize an action.
    if not hit or not (a in hit or b in hit):
        return None
    if any(cell not in COLORS for cell in (x, y)):
        return None
    straight_five = any(len(group) >= 5 for group in groups)
    four = any(len(group) == 4 for group in groups)
    crosses = any(len(groups[i] & groups[j]) > 0
                  for i in range(len(groups)) for j in range(i + 1, len(groups)))
    special_boost = 0.0
    if specials:
        for cell in hit:
            special_boost += {'flame': 11.0, 'star': 24.0}.get(specials.get(cell), 0.0)
    kinds = []
    if straight_five:
        kinds.append('5-in-line')
    if crosses:
        kinds.append('T/L/cross')
    if four:
        kinds.append('4-in-line')
    if not kinds:
        kinds.append('3-match')

    if mode == 'power':
        length_weight, five_weight, cross_weight, four_weight, gravity_weight = 1.8, 29, 23, 12, .65
    elif mode == 'speed':
        length_weight, five_weight, cross_weight, four_weight, gravity_weight = 1.25, 12, 10, 5, .2
    else:
        length_weight, five_weight, cross_weight, four_weight, gravity_weight = 1.5, 20, 17, 8, .45
    score = (length_weight * len(hit) + five_weight * straight_five
             + cross_weight * crosses + four_weight * four
             + special_boost + gravity_weight * cascade_estimate(swapped, hit))
    # Tiny deterministic tie-breaker: lower rows usually disturb more existing gems.
    score += .006 * max(a[0], b[0])
    return Move(a, b, round(score, 3), len(hit), '+'.join(kinds))


def rank_moves(board: Board, mode: MoveMode = 'balanced',
               specials: Mapping[Cell, str] | None = None,
               banned: set[tuple[Cell, Cell]] | None = None) -> list[Move]:
    banned = banned or set()
    result = []
    for r in range(N):
        for c in range(N):
            for dr, dc in ((0, 1), (1, 0)):
                rr, cc = r + dr, c + dc
                if rr >= N or cc >= N:
                    continue
                a, b = (r, c), (rr, cc)
                if (a, b) in banned:
                    continue
                mv = move_value(board, a, b, mode=mode, specials=specials)
                if mv is not None:
                    result.append(mv)
    result.sort(key=lambda move: (-move.score, -move.removed, move.src, move.dst))
    return result
