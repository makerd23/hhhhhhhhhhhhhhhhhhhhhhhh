# -*- coding: utf-8 -*-
"""
Bejeweled Blitz one-file assistant (Windows + Python 3.12; STANDARD LIBRARY ONLY)

No pip, OpenCV, Pillow, NumPy, data files or installation of modules required.
F2: record first and last gem centres (one press each), again to recalibrate.
F4: print a one-time 8x8 recognition snapshot.
F8: enable / pause autoplay (start after the timer is running).
F9: quit. Move mouse into the top-left corner to halt all bot clicks.

Each game and each move is freshly screen-read. Only manual start of the next
round is supported; the bot can watch for the new round automatically.
Does not bypass device policy or guarantee support on school-managed PCs.
"""
from __future__ import annotations

import colorsys
import ctypes
from ctypes import wintypes
from collections import Counter
from dataclasses import dataclass
import os
import sys
import time

if sys.platform != 'win32':
    print('此程式只能在 Windows 執行。')
    raise SystemExit(1)

# Configure DPI mapping before reading coordinates or screen pixels.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)

# Never allow ctypes to truncate 64-bit Windows handles.
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wintypes.HDC,
                           ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                               wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                               wintypes.UINT]

VK_F2, VK_F4, VK_F8, VK_F9 = 0x71, 0x73, 0x77, 0x78
MOUSE_DOWN, MOUSE_UP = 0x0002, 0x0004
SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
N = 8
COLORS = frozenset('YOGBPRW')


def log(message: str) -> None:
    print(time.strftime('[%H:%M:%S] '), message, flush=True)


def cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(p)):
        raise RuntimeError('無法取得滑鼠座標')
    return p.x, p.y


def panic_corner() -> bool:
    """Fail-safe: mouse in top-left 5x5 pixels of primary screen."""
    x, y = cursor_pos()
    return x <= 4 and y <= 4


class KeyEdges:
    """Global hotkeys without additional packages or hooks."""
    def __init__(self):
        self.last = {k: False for k in (VK_F2, VK_F4, VK_F8, VK_F9)}

    def poll(self) -> set[int]:
        pressed = set()
        for key in self.last:
            state = bool(user32.GetAsyncKeyState(key) & 0x8000)
            if state and not self.last[key]:
                pressed.add(key)
            self.last[key] = state
        return pressed


@dataclass(frozen=True)
class Grid:
    x0: float
    y0: float
    dx: float
    dy: float
    hwnd: int

    @classmethod
    def from_corners(cls, a: tuple[int, int], b: tuple[int, int], hwnd: int):
        dx, dy = (b[0]-a[0])/7, (b[1]-a[1])/7
        if not (18 <= dx <= 160 and 18 <= dy <= 160):
            raise ValueError('格子間距不正常。請選第1列第1格和第8列第8格的中心。')
        if abs(dx-dy)/max(dx, dy) > 0.18:
            raise ValueError('棋盤應接近正方格，請重新校準。')
        if a[0]-dx/2 < 0 or a[1]-dy/2 < 0:
            raise ValueError('棋盤太靠近螢幕左上角，請移動遊戲視窗。')
        if not hwnd:
            raise ValueError('找不到遊戲視窗。')
        return cls(float(a[0]), float(a[1]), dx, dy, hwnd)

    def center(self, r: int, c: int) -> tuple[int, int]:
        return round(self.x0 + self.dx*c), round(self.y0 + self.dy*r)

    def capture_rect(self) -> tuple[int, int, int, int]:
        return (round(self.x0-self.dx/2), round(self.y0-self.dy/2),
                round(8*self.dx), round(8.7*self.dy))


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


class RGBQuad(ctypes.Structure):
    _fields_ = [('b', ctypes.c_ubyte), ('g', ctypes.c_ubyte),
                ('r', ctypes.c_ubyte), ('reserved', ctypes.c_ubyte)]


class BitmapInfo(ctypes.Structure):
    _fields_ = [('header', BitmapInfoHeader), ('colors', RGBQuad*1)]


class Screen:
    """Native Windows GDI capture; top-down BGRA pixels, no image libraries."""
    def __init__(self, grid: Grid):
        self.grid = grid
        self.left, self.top, self.width, self.height = grid.capture_rect()
        self.desktop = user32.GetDC(None)
        if not self.desktop:
            raise RuntimeError('Windows GetDC 失敗')
        self.dc = gdi32.CreateCompatibleDC(self.desktop)
        self.bitmap = gdi32.CreateCompatibleBitmap(self.desktop, self.width, self.height)
        if not self.dc or not self.bitmap:
            self.close()
            raise RuntimeError('Windows 建立 GDI 擷取資源失敗')
        self.buffer = (ctypes.c_ubyte*(self.width*self.height*4))()
        self.info = BitmapInfo()
        self.info.header.biSize = ctypes.sizeof(BitmapInfoHeader)
        self.info.header.biWidth = self.width
        self.info.header.biHeight = -self.height  # top-down
        self.info.header.biPlanes = 1
        self.info.header.biBitCount = 32
        self.info.header.biCompression = 0  # BI_RGB
        self.info.header.biSizeImage = self.width*self.height*4

    def grab(self):
        old = gdi32.SelectObject(self.dc, self.bitmap)
        if not old:
            raise RuntimeError('Windows SelectObject 失敗')
        try:
            ok = gdi32.BitBlt(self.dc, 0, 0, self.width, self.height,
                              self.desktop, self.left, self.top,
                              SRCCOPY | CAPTUREBLT)
        finally:
            gdi32.SelectObject(self.dc, old)
        if not ok:
            raise RuntimeError('Windows BitBlt 擷取失敗')
        lines = gdi32.GetDIBits(self.dc, self.bitmap, 0, self.height,
                                self.buffer, ctypes.byref(self.info), 0)
        if lines != self.height:
            raise RuntimeError(f'Windows GetDIBits 失敗 ({lines})')
        return self

    def pixel(self, sx: int, sy: int) -> tuple[int, int, int]:
        x, y = sx-self.left, sy-self.top
        if not (0 <= x < self.width and 0 <= y < self.height):
            return 0, 0, 0
        i = (y*self.width+x)*4
        data = self.buffer
        # Raw Windows DIB has B, G, R, unused.
        return data[i+2], data[i+1], data[i]

    def close(self):
        if getattr(self, 'bitmap', None):
            gdi32.DeleteObject(self.bitmap)
            self.bitmap = None
        if getattr(self, 'dc', None):
            gdi32.DeleteDC(self.dc)
            self.dc = None
        if getattr(self, 'desktop', None):
            user32.ReleaseDC(None, self.desktop)
            self.desktop = None


# Screenshot-calibrated RGB hue boundaries for the game's seven normal gems.
# Real-world animation/special effects may be less reliable than the reference.
def classify_rgb(rgb: tuple[int, int, int]) -> str | None:
    r, g, b = rgb
    hi, lo = max(r, g, b), min(r, g, b)
    d = hi-lo
    if hi < 90:
        return None  # dark board/background
    if d < 43:
        return 'W' if hi > 135 else None  # silver / white
    # `colorsys` is part of Python's standard library.
    h, s, _ = colorsys.rgb_to_hsv(r/255.0, g/255.0, b/255.0)
    if s < 0.28:
        return None
    hue = h*360
    if hue < 11 or hue >= 333:
        return 'R'
    if hue < 43:
        return 'O'
    if hue < 83:
        return 'Y'
    if hue < 170:
        return 'G'
    if hue < 260:
        return 'B'
    return 'P'


@dataclass(frozen=True)
class Observation:
    board: tuple[str, ...]
    certainty: float
    unknown: int
    timer: float

    @property
    def good(self) -> bool:
        return self.unknown == 0 and self.certainty >= .72


def recognize(grid: Grid, screen: Screen) -> Observation:
    screen.grab()
    rows = []
    confidence = []
    radius = max(7, round(min(grid.dx, grid.dy)*.35))
    step = max(3, round(min(grid.dx, grid.dy)*.085))
    offsets = [(x, y) for y in range(-radius, radius+1, step)
               for x in range(-radius, radius+1, step)
               if x*x+y*y <= radius*radius]
    for r in range(N):
        line = []
        for c in range(N):
            cx, cy = grid.center(r, c)
            count = Counter()
            for ox, oy in offsets:
                result = classify_rgb(screen.pixel(cx+ox, cy+oy))
                if result:
                    count[result] += 1
            total = sum(count.values())
            if total < max(5, len(offsets)//5):
                line.append('?')
                confidence.append(0.0)
            else:
                color, votes = count.most_common(1)[0]
                purity = votes/total
                line.append(color if purity >= .50 else '?')
                confidence.append(purity)
        rows.append(''.join(line))

    # Cyan countdown bar is directly below the 8th row.
    bar_y = round(grid.y0 + (7+.865)*grid.dy)
    left = round(grid.x0-.43*grid.dx)
    right = round(grid.x0+7.42*grid.dx)
    samples = 60
    on = 0
    for i in range(samples):
        x = round(left+(right-left)*i/(samples-1))
        rr, gg, bb = screen.pixel(x, bar_y)
        if gg > 105 and bb > 135 and bb > 1.4*rr and gg > 1.4*rr:
            on += 1
    return Observation(tuple(rows), sum(confidence)/64,
                       sum(row.count('?') for row in rows), on/samples)


@dataclass(frozen=True)
class Move:
    a: tuple[int, int]
    b: tuple[int, int]
    score: float
    removed: int
    kind: str


def runs(board: tuple[str, ...]) -> list[set[tuple[int, int]]]:
    groups = []
    for horizontal in (True, False):
        for fixed in range(N):
            i = 0
            while i < N:
                ch = board[fixed][i] if horizontal else board[i][fixed]
                j = i+1
                while j < N and (board[fixed][j] if horizontal else board[j][fixed]) == ch:
                    j += 1
                if ch in COLORS and j-i >= 3:
                    groups.append({(fixed, k) if horizontal else (k, fixed)
                                   for k in range(i, j)})
                i = j
    return groups


def best_moves(board: tuple[str, ...], banned: set) -> list[Move]:
    results = []
    for r in range(N):
        for c in range(N):
            for nr, nc in ((r+1, c), (r, c+1)):
                if nr >= N or nc >= N:
                    continue
                a, b = (r, c), (nr, nc)
                if (a, b) in banned:
                    continue
                va, vb = board[r][c], board[nr][nc]
                if va == vb or va not in COLORS or vb not in COLORS:
                    continue
                work = [list(row) for row in board]
                work[r][c], work[nr][nc] = work[nr][nc], work[r][c]
                groups = runs(tuple(''.join(row) for row in work))
                hits = set().union(*groups) if groups else set()
                if not hits or (a not in hits and b not in hits):
                    continue
                special5 = any(len(g) >= 5 for g in groups)
                special4 = any(len(g) == 4 for g in groups)
                cross = any(groups[i]&groups[j] for i in range(len(groups))
                            for j in range(i+1, len(groups)))
                score = (1.6*len(hits)+23*special5+13*cross+8*special4)
                score += .015*max(r, nr)
                kind = ('5連' if special5 else '交叉' if cross else
                        '4連' if special4 else '3連')
                results.append(Move(a, b, score, len(hits), kind))
    results.sort(key=lambda m: (-m.score, -m.removed, m.a, m.b))
    return results


def move_mouse(grid: Grid, move: Move) -> None:
    """Drag, not click. Re-check focus and panic before holding button."""
    if user32.GetForegroundWindow() != grid.hwnd or panic_corner():
        return
    ax, ay = grid.center(*move.a)
    bx, by = grid.center(*move.b)
    user32.SetCursorPos(ax, ay)
    try:
        user32.mouse_event(MOUSE_DOWN, 0, 0, 0, None)
        time.sleep(.035)
        for i in range(1, 5):
            if user32.GetForegroundWindow() != grid.hwnd:
                break
            user32.SetCursorPos(round(ax+(bx-ax)*i/4),
                                round(ay+(by-ay)*i/4))
            time.sleep(.016)
    finally:
        user32.mouse_event(MOUSE_UP, 0, 0, 0, None)


class Bot:
    def __init__(self):
        self.keys = KeyEdges()
        self.first = None
        self.first_hwnd = None
        self.grid = None
        self.screen = None
        self.enabled = False
        self.phase = 'waiting'  # waiting, playing, next_wait
        self.last_observation = None
        self.board_repeat = 0
        self.phase_started = 0.0
        self.last_move_time = 0.0
        self.last_move_board = None
        self.last_move = None
        self.banned = set()
        self.move_num = 0
        self.low_timer_frames = 0
        self.next_ready_frames = 0
        self.last_report = 0.0
        self.round_num = 0

    def reset_observation(self):
        self.last_observation = None
        self.board_repeat = 0

    def calibrate(self):
        point = cursor_pos()
        hwnd = user32.GetForegroundWindow()
        if self.first is None:
            self.first = point
            self.first_hwnd = hwnd
            self.enabled = False
            log(f'記下左上寶石中心 {point}；請移到右下第8列第8顆，再按 F2。')
            return
        try:
            if hwnd != self.first_hwnd:
                raise ValueError('兩次校準必須在同一個瀏覽器視窗。')
            grid = Grid.from_corners(self.first, point, hwnd)
            screen = Screen(grid)
            test = recognize(grid, screen)
        except Exception as exc:
            log(f'校準失敗：{exc}，請重新從左上第1顆開始。')
            self.first = None
            return
        if self.screen:
            self.screen.close()
        self.screen = screen
        self.grid = grid
        self.first = None
        self.phase = 'waiting'
        self.enabled = False
        self.banned.clear()
        self.reset_observation()
        log(f'校準成功，格距 {grid.dx:.1f}×{grid.dy:.1f}；辨識信心 {test.certainty:.0%}，未知 {test.unknown} 格。')
        for row in test.board:
            print('      '+' '.join(row), flush=True)
        log('手動開始一局後，在遊戲視窗按 F8 開啟自動消除。')

    def snapshot(self):
        if not self.screen:
            log('請先按兩次 F2 校準。')
            return
        obs = recognize(self.grid, self.screen)
        log(f'8×8 棋盤：信心 {obs.certainty:.0%}，未知 {obs.unknown} 格，倒數條 {obs.timer:.0%}')
        for row in obs.board:
            print('      '+' '.join(row), flush=True)
        opts = best_moves(obs.board, set())
        if opts:
            m = opts[0]
            log(f'目前最佳：{m.a[0]+1}排{m.a[1]+1}格 → {m.b[0]+1}排{m.b[1]+1}格 ({m.kind})')

    def toggle(self):
        if not self.screen:
            log('請先把滑鼠移到左上第1顆按 F2，再到右下第8顆按 F2。')
            return
        self.enabled = not self.enabled
        self.reset_observation()
        if self.enabled:
            # F8 arms the bot; do NOT automatically click the new game button.
            self.phase = 'waiting'
            self.phase_started = time.monotonic()
            self.banned.clear()
            self.last_move_board = None
            self.last_move = None
            log('自動模式開啟：偵測當局棋盤；換下一局時會重新掃描。')
        else:
            log('已暫停：不會再操作滑鼠。')

    def round_start(self, obs):
        self.round_num += 1
        self.move_num = 0
        self.phase = 'playing'
        self.phase_started = time.monotonic()
        self.last_move_board = None
        self.last_move = None
        self.last_move_time = 0.0
        self.banned.clear()
        self.low_timer_frames = 0
        self.reset_observation()
        log(f'第 {self.round_num} 局開始：已重新辨識 64 格；倒數條 {obs.timer:.0%}。')

    def round_end(self, reason):
        log(f'第 {self.round_num} 局結束：{reason}；共操作 {self.move_num} 步。')
        log('下一局請手動按遊戲的 Play；程式會自動偵測新的滿格倒數條。')
        self.phase = 'next_wait'
        self.next_ready_frames = 0
        self.reset_observation()
        self.last_move_board = None
        self.banned.clear()

    def tick(self):
        if not self.enabled or not self.grid or not self.screen:
            return
        # Strict window focus guard; never input to Word/VS Code/other apps.
        if user32.GetForegroundWindow() != self.grid.hwnd:
            return
        if panic_corner():
            self.enabled = False
            log('觸發滑鼠左上角緊急停止；再按 F8 才能恢復。')
            return
        now = time.monotonic()
        obs = recognize(self.grid, self.screen)
        if self.phase == 'waiting':
            if obs.good and obs.timer > .12 and not runs(obs.board):
                self.round_start(obs)
            return
        if self.phase == 'next_wait':
            if obs.good and obs.timer >= .85 and not runs(obs.board):
                self.next_ready_frames += 1
                if self.next_ready_frames >= 3:
                    self.round_start(obs)
            else:
                self.next_ready_frames = 0
            return

        # Never continue one 60s round indefinitely.
        if now-self.phase_started > 64:
            self.round_end('時間限制')
            return
        if obs.timer < .025:
            self.low_timer_frames += 1
        else:
            self.low_timer_frames = 0
        if self.low_timer_frames >= 3:
            self.round_end('倒數條歸零')
            return
        if not obs.good or runs(obs.board):
            self.reset_observation()
            return
        # Wait for drag and falling/combination animation to end.
        if now-self.last_move_time < .22:
            return
        if self.last_observation is not None and obs.board == self.last_observation.board:
            self.board_repeat += 1
        else:
            self.board_repeat = 1
            self.last_observation = obs
        if self.board_repeat < 3:
            return
        if self.last_move_board is not None:
            if obs.board == self.last_move_board:
                if now-self.last_move_time < .75:
                    return
                if self.last_move is not None:
                    self.banned.add((self.last_move.a, self.last_move.b))
                    log('剛才操作後棋盤未變，跳過該交換並重算。')
            else:
                self.banned.clear()
            self.last_move_board = None
            self.last_move = None
        opts = best_moves(obs.board, self.banned)
        if not opts:
            if now-self.last_report > 2.0:
                log('目前找不到確定可用的交換，等待棋盤變動。')
                self.last_report = now
            return
        move = opts[0]
        self.last_move_board = obs.board
        self.last_move = move
        log(f'步驟 {self.move_num+1:03d}：({move.a[0]+1},{move.a[1]+1})→({move.b[0]+1},{move.b[1]+1})，{move.kind}，預估消除 {move.removed} 顆')
        move_mouse(self.grid, move)
        self.last_move_time = time.monotonic()
        self.move_num += 1
        self.reset_observation()

    def run(self):
        log('Bejeweled Blitz 單檔版 / Windows / 純 Python 標準函式庫')
        log('F2 校準兩個角（兩次） ｜ F4 查看辨識 ｜ F8 開始/暫停 ｜ F9 結束')
        log('請讓遊戲在瀏覽器中保持完整顯示，且不要移動或縮放視窗。')
        try:
            while True:
                keys = self.keys.poll()
                if VK_F9 in keys:
                    log('收到 F9，結束程式。')
                    break
                if VK_F2 in keys:
                    self.calibrate()
                if VK_F4 in keys:
                    self.snapshot()
                if VK_F8 in keys:
                    self.toggle()
                if self.enabled:
                    self.tick()
                    time.sleep(.060)
                else:
                    time.sleep(.075)
        except KeyboardInterrupt:
            log('Ctrl+C 結束。')
        except Exception as exc:
            log(f'執行錯誤：{type(exc).__name__}：{exc}')
            log('請保留此錯誤訊息，回報以便修正。')
            time.sleep(5)
        finally:
            if self.screen:
                self.screen.close()


if __name__ == '__main__':
    Bot().run()
