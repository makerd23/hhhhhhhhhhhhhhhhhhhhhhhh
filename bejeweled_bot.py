"""Bejeweled Blitz screen-reading auto player (Python 3.12).

Requirements: pip install opencv-python numpy pyautogui pillow
Run: python bejeweled_bot.py

Move mouse to top-left screen corner to stop immediately (PyAutoGUI failsafe).
Only use this where automated play is permitted.
"""
import time
from dataclasses import dataclass

import cv2
import numpy as np
import pyautogui as pg

pg.FAILSAFE = True
pg.PAUSE = 0.015

N = 8
GAME_SECONDS = 60
# HSV hues on OpenCV's 0..179 scale, derived from the supplied screenshot.
PALETTE = {
    "O": 12,   # orange
    "Y": 29,   # yellow
    "G": 65,   # green
    "B": 106,  # blue
    "P": 150,  # purple
    "R": 176,  # red
}

@dataclass
class BoardGeometry:
    x0: float
    y0: float
    dx: float
    dy: float

    def center(self, row, col):
        return (round(self.x0 + col * self.dx), round(self.y0 + row * self.dy))

    def bbox(self):
        # Screenshot crop includes board area, not browser controls.
        left = round(self.x0 - self.dx / 2)
        top = round(self.y0 - self.dy / 2)
        return (left, top, round(8 * self.dx), round(8 * self.dy))


def calibrate():
    print("\n校準棋盤：先將滑鼠移到左上角第一顆寶石的正中央。")
    input("保持滑鼠不動，按 Enter 記錄位置：")
    x0, y0 = pg.position()
    print(f"左上角中心：{x0}, {y0}")
    print("\n接著移到右下角最後一顆寶石的正中央。")
    input("保持滑鼠不動，按 Enter 記錄位置：")
    x7, y7 = pg.position()
    dx, dy = (x7 - x0) / 7, (y7 - y0) / 7
    if not (15 <= dx <= 160 and 15 <= dy <= 160):
        raise ValueError("校準位置異常，請選棋盤左上角與右下角寶石的中心。")
    print(f"棋盤格距：{dx:.1f} x {dy:.1f} pixels")
    return BoardGeometry(x0, y0, dx, dy)


def classify_tile(hsv, cy, cx, halfsize):
    h, w = hsv.shape[:2]
    k = max(4, round(halfsize * 0.55))
    patch = hsv[max(0,cy-k):min(h,cy+k+1), max(0,cx-k):min(w,cx+k+1)]
    if patch.size == 0:
        return "?"
    median_h, median_s, median_v = np.median(patch.reshape(-1, 3), axis=0)
    if median_v < 90:
        return "?"  # animation/empty space
    if median_s < 70:
        return "W" if median_v > 140 else "?" # white gems
    hue_dist = lambda a, b: min(abs(a-b), 180-abs(a-b))
    label, distance = min(((c, hue_dist(median_h, center)) for c,center in PALETTE.items()), key=lambda t: t[1])
    return label if distance < 18 else "?"


def read_board(geo):
    left, top, width, height = geo.bbox()
    # Screenshot region must be fully inside the screen.
    if left < 0 or top < 0:
        raise ValueError("棋盤超出畫面，請重新校準。")
    rgb = np.asarray(pg.screenshot(region=(left, top, width, height)).convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    grid = []
    for r in range(N):
        row = []
        for c in range(N):
            cy = round((r + 0.5) * geo.dy)
            cx = round((c + 0.5) * geo.dx)
            row.append(classify_tile(hsv, cy, cx, min(geo.dx, geo.dy) / 2))
        grid.append(row)
    return grid


def find_matches(board):
    matched = set()
    for r in range(N):
        c = 0
        while c < N:
            end = c + 1
            while end < N and board[r][end] == board[r][c]:
                end += 1
            if board[r][c] != "?" and end - c >= 3:
                matched.update((r, i) for i in range(c, end))
            c = end
    for c in range(N):
        r = 0
        while r < N:
            end = r + 1
            while end < N and board[end][c] == board[r][c]:
                end += 1
            if board[r][c] != "?" and end - r >= 3:
                matched.update((i, c) for i in range(r, end))
            r = end
    return matched


def score_move(board, a, b):
    (r1,c1),(r2,c2) = a,b
    if board[r1][c1] == "?" or board[r2][c2] == "?":
        return -1
    if board[r1][c1] == board[r2][c2]:
        return -1
    board[r1][c1], board[r2][c2] = board[r2][c2], board[r1][c1]
    matches = find_matches(board)
    # A valid swap must cause a match involving one of the moved positions.
    valid = a in matches or b in matches
    score = -1
    if valid:
        score = len(matches)
        if len(matches) >= 4:
            score += 3
        if len(matches) >= 5:
            score += 5
        # Prefer lower swaps on ties: can cascade falling gems.
        score += max(r1, r2) * 0.015
    board[r1][c1], board[r2][c2] = board[r2][c2], board[r1][c1]
    return score


def choose_move(board):
    best = None
    for r in range(N):
        for c in range(N):
            for dr,dc in ((0,1),(1,0)):
                rr, cc = r+dr, c+dc
                if rr >= N or cc >= N:
                    continue
                score = score_move(board, (r,c), (rr,cc))
                if score >= 0 and (best is None or score > best[0]):
                    best = (score, (r,c), (rr,cc))
    return best


def do_swap(geo, a, b):
    start = geo.center(*a)
    end = geo.center(*b)
    pg.moveTo(*start, duration=0.035)
    pg.dragTo(*end, duration=0.12, button="left")


def print_board(board):
    for row in board:
        print(" ".join(row))


def play_round(geo, round_number):
    """每局從當前螢幕重新辨識，絕不沿用上一局棋盤。"""
    print(f"\n===== 第 {round_number} 局：重新辨識 64 格 =====")
    # 允許開場掉落動畫完成；每次都取得全新截圖。
    board = None
    for attempt in range(20):
        candidate = read_board(geo)
        unknown = sum(tile == "?" for row in candidate for tile in row)
        if unknown <= 3 and not find_matches(candidate) and choose_move(candidate) is not None:
            board = candidate
            break
        time.sleep(0.15)
    if board is None:
        print("尚未辨識到穩定棋盤；請確定遊戲已開始、沒有動畫，然後重試。")
        return
    print_board(board)
    started = time.monotonic()
    moves = 0
    previous_board = None
    while time.monotonic() - started < GAME_SECONDS:
        # 每一步都從螢幕重新讀取；不使用上一局或上一步的排列當作決策依據。
        board = read_board(geo)
        if sum(tile == "?" for row in board for tile in row) > 3:
            time.sleep(0.12)
            continue
        if find_matches(board):
            time.sleep(0.14)
            continue
        if previous_board is not None and board == previous_board:
            time.sleep(0.12)
        move = choose_move(board)
        if move is None:
            time.sleep(0.14)
            continue
        score, a, b = move
        print(f"第 {round_number} 局 / 第 {moves + 1:03d} 步: {a} -> {b}, 估計={score:.2f}")
        do_swap(geo, a, b)
        moves += 1
        previous_board = [row[:] for row in board]
        time.sleep(0.32)
    print(f"第 {round_number} 局結束：嘗試 {moves} 次交換。")


def main():
    print("Bejeweled Blitz 多局自動消除 (Python 3.12)")
    print("緊急停止：移動滑鼠到螢幕最左上角，或按 Ctrl+C。")
    print("請固定遊戲視窗大小與縮放比例。")
    geo = calibrate()
    round_number = 0
    while True:
        answer = input("\n請在遊戲中開始新的一局；等寶石出現後，按 Enter 重新掃描（輸入 q 結束）：").strip().lower()
        if answer == "q":
            break
        round_number += 1
        play_round(geo, round_number)
        print("如要繼續下一局，請先在遊戲中手動點選 Play Again。")


if __name__ == "__main__":
    try:
        main()
    except pg.FailSafeException:
        print("\n已透過滑鼠左上角緊急停止。")
    except KeyboardInterrupt:
        print("\n已按 Ctrl+C 停止。")
