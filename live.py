"""Realtime screen reader and mouse controller. No game-memory modification."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from core import matches, rank_moves
from vision import Geometry, Observation, Recognizer, annotate, stable

LOG = logging.getLogger('bejeweled')
ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / 'calibration.json'


def dpi_awareness():
    """Avoid Windows display scaling shifting screenshot vs mouse coordinates."""
    import sys
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def mouse_library():
    dpi_awareness()
    import pyautogui as pg
    pg.FAILSAFE = True
    pg.PAUSE = .005
    return pg


@dataclass
class Calibration:
    x0: float
    y0: float
    dx: float
    dy: float
    screen_width: int
    screen_height: int

    def geo(self) -> Geometry:
        return Geometry(self.x0, self.y0, self.dx, self.dy)

    def save(self):
        SETTINGS.write_text(json.dumps(asdict(self), indent=2), encoding='utf-8')

    @classmethod
    def load(cls):
        if not SETTINGS.exists():
            raise FileNotFoundError('尚未校準，請先執行 python main.py calibrate')
        data = json.loads(SETTINGS.read_text(encoding='utf-8'))
        return cls(**data)


def calibrate() -> Calibration:
    pg = mouse_library()
    print('請先讓遊戲棋盤顯示完整、瀏覽器縮放固定在預計遊玩的比例。')
    input('把滑鼠移至第 1 列第 1 顆寶石的中心，再按 Enter ... ')
    x0, y0 = pg.position()
    print(f'第一顆：({x0}, {y0})')
    input('把滑鼠移至第 8 列第 8 顆寶石的中心，再按 Enter ... ')
    x7, y7 = pg.position()
    geo = Geometry(x0, y0, (x7-x0)/7, (y7-y0)/7)
    geo.validate()
    left, top, width, height = geo.rect()
    s = pg.size()
    if left < 0 or top < 0 or left+width > s.width or top+height > s.height:
        raise ValueError('棋盤/倒數條超出主要螢幕擷取範圍；請調整遊戲位置。')
    cal = Calibration(geo.x0, geo.y0, geo.dx, geo.dy, s.width, s.height)
    cal.save()
    print(f'校準成功：間距 {geo.dx:.2f}×{geo.dy:.2f}；已儲存 {SETTINGS.name}')
    return cal


class Screen:
    """Fast MSS capture with PyAutoGUI screenshot fallback."""
    def __init__(self, cal: Calibration):
        self.cal = cal
        self.geo = cal.geo()
        self.geo.validate()
        self.rect = self.geo.rect()
        pg = mouse_library()
        current = pg.size()
        if (cal.screen_width, cal.screen_height) != (current.width, current.height):
            raise RuntimeError('螢幕解析度已變更，請重新執行 calibrate')
        self.pg = pg
        self.sct = None
        try:
            import mss
            self.sct = mss.mss()
            LOG.info('截圖方式：MSS')
        except Exception as exc:
            LOG.warning('MSS 不可用，改用 PyAutoGUI：%s', exc)

    def capture(self) -> np.ndarray:
        left, top, w, h = self.rect
        if self.sct is not None:
            # MSS BGRA -> RGB
            shot = np.asarray(self.sct.grab({'left':left, 'top':top, 'width':w, 'height':h}))
            return cv2.cvtColor(shot, cv2.COLOR_BGRA2RGB)
        return np.asarray(self.pg.screenshot(region=(left, top, w, h)).convert('RGB'))

    def close(self):
        if self.sct is not None:
            self.sct.close()

    def swap(self, src: tuple[int, int], dst: tuple[int, int], drag_seconds: float):
        a = self.geo.center(*src)
        b = self.geo.center(*dst)
        # Explicit mouseDown/mouseUp and finally to avoid a held mouse button.
        self.pg.moveTo(*a)
        try:
            self.pg.mouseDown(button='left')
            self.pg.moveTo(*b, duration=drag_seconds)
        finally:
            self.pg.mouseUp(button='left')


class Hotkeys:
    """Global F8 pause/resume; F9/ESC stop; F6 take diagnostic snapshot."""
    def __init__(self):
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.snapshot = threading.Event()
        self.listener = None
        self._last_toggle = 0.0
        try:
            from pynput import keyboard
        except ImportError:
            LOG.warning('pynput 缺失，全域熱鍵不可用；Ctrl+C/滑鼠左上角仍可停止')
            return

        def pressed(key):
            if key in (keyboard.Key.f9, keyboard.Key.esc):
                self.stop.set()
            elif key == keyboard.Key.f6:
                self.snapshot.set()
            elif key == keyboard.Key.f8:
                now = time.monotonic()
                if now - self._last_toggle > .35:
                    self._last_toggle = now
                    if self.paused.is_set():
                        self.paused.clear()
                        LOG.info('繼續')
                    else:
                        self.paused.set()
                        LOG.info('暫停（遊戲倒數不會停止）')
        try:
            self.listener = keyboard.Listener(on_press=pressed)
            self.listener.start()
        except Exception as exc:
            LOG.warning('無法註冊全域熱鍵：%s', exc)

    def close(self):
        if self.listener:
            self.listener.stop()


class Player:
    def __init__(self, cal: Calibration, mode='balanced', drag_seconds=.105,
                 dry_run=False, max_rounds=0, save_debug=False):
        self.screen = Screen(cal)
        self.geo = cal.geo()
        self.detector = Recognizer(self.geo, ROOT / 'assets')
        self.hotkeys = Hotkeys()
        self.mode = mode
        self.drag_seconds = drag_seconds
        self.dry_run = dry_run
        self.max_rounds = max_rounds
        self.save_debug = save_debug
        self.nrounds = 0
        self._debug_counter = 0

    def observe(self) -> Observation:
        return self.detector.analyze(self.screen.capture())

    def maybe_save(self, obs: Observation, prefix: str):
        if self.save_debug or self.hotkeys.snapshot.is_set():
            self.hotkeys.snapshot.clear()
            self._debug_counter += 1
            dest = ROOT / 'debug' / f'{prefix}_{self._debug_counter:04d}.png'
            annotate(obs, self.geo, dest, self.mode)
            LOG.info('已儲存診斷圖：%s', dest)

    def should_stop(self):
        return self.hotkeys.stop.is_set()

    def pause_if_needed(self):
        while self.hotkeys.paused.is_set() and not self.should_stop():
            time.sleep(.10)

    def await_stable(self, timeout=2.5, baseline: Optional[Observation] = None):
        deadline = time.monotonic() + timeout
        prior = None
        observed_different = baseline is None
        last = None
        while time.monotonic() < deadline and not self.should_stop():
            self.pause_if_needed()
            obs = self.observe()
            last = obs
            self.maybe_save(obs, 'snap')
            if baseline is not None and obs.board != baseline.board:
                observed_different = True
            if observed_different and prior is not None and stable(prior, obs, self.geo):
                if not matches(obs.board):
                    return obs
            prior = obs
            time.sleep(.067)
        if last is not None:
            self.maybe_save(last, 'timeout')
        return None

    def _play_one(self, start: Observation):
        self.nrounds += 1
        round_num = self.nrounds
        LOG.info('第 %d 局：重新辨識棋盤，模式=%s，倒數條=%.0f%%',
                 round_num, self.mode, start.timer_fill * 100)
        for row in start.board:
            LOG.info('  %s', ' '.join(row))
        started = time.monotonic()
        board = start
        prev_board = board.board
        banned = set()
        moves_made = 0
        invalid_moves = 0
        last_progress = started
        timer_low_frames = 0
        completed_by_timer = False

        while not self.should_stop() and time.monotonic() - started < 65:
            self.pause_if_needed()
            # Refresh even when unchanged; never reuse an old board for a new decision.
            if board is None:
                board = self.await_stable(timeout=1.45)
                if board is None:
                    obs = self.observe()
                    if obs.timer_fill < .022:
                        timer_low_frames += 1
                    if timer_low_frames >= 2:
                        completed_by_timer = True
                        break
                    continue
            if board.board != prev_board:
                banned.clear()
                prev_board = board.board

            if board.timer_fill < .025:
                timer_low_frames += 1
                if timer_low_frames >= 2:
                    completed_by_timer = True
                    break
                board = None
                continue
            timer_low_frames = 0
            if self.dry_run:
                ranked = rank_moves(board.board, self.mode, specials=board.specials)
                if ranked:
                    LOG.info('[DRY RUN] 最佳走法 %s → %s；%.1f 分（%s）',
                             ranked[0].src, ranked[0].dst, ranked[0].score, ranked[0].kind)
                self.maybe_save(board, 'dry_run')
                time.sleep(.65)
                board = None
                continue

            ranked = rank_moves(board.board, self.mode, specials=board.specials, banned=banned)
            if not ranked:
                LOG.warning('目前沒有未嘗試的有效交換；重新等候棋盤變化')
                # Do not repeatedly click a failed move on an unchanged board.
                time.sleep(.18)
                board = self.await_stable(timeout=.35, baseline=board)
                if time.monotonic() - last_progress > 7:
                    LOG.warning('長時間無進展，本局停止操作，避免無限誤點')
                    break
                continue

            move = ranked[0]
            LOG.info('第 %03d 步：%s → %s，預估 %.1f [%s]（倒數條 %.0f%%）',
                     moves_made+1, move.src, move.dst, move.score, move.kind,
                     board.timer_fill*100)
            before = board
            self.screen.swap(move.src, move.dst, self.drag_seconds)
            moves_made += 1
            # Wait for gem movement to finish. If board did not change, try a
            # different legal action rather than blindly repeating the drag.
            board = self.await_stable(timeout=1.6, baseline=before)
            if board is None:
                now = self.observe()
                if now.active and now.board == before.board:
                    banned.add((move.src, move.dst))
                    invalid_moves += 1
                    LOG.warning('未確認交換成功，暫時排除這一步；失敗次數=%d', invalid_moves)
                    board = self.await_stable(timeout=.4)
                else:
                    LOG.debug('棋盤可能仍在動畫中，繼續等待')
            else:
                last_progress = time.monotonic()
                invalid_moves = 0
            if invalid_moves >= 10:
                LOG.error('交換失敗過多，停止本局；檢查縮放比例與座標')
                break
        elapsed = time.monotonic() - started
        LOG.info('第 %d 局結束；嘗試 %d 次交換；經過 %.1f 秒', round_num, moves_made, elapsed)
        return completed_by_timer

    def run(self):
        LOG.info('F8 暫停/繼續，F6 儲存診斷圖，F9 或 ESC 停止；滑鼠到左上角緊急停止')
        LOG.info('不會自動按開始下一局，也不會點選商城/購買按鈕。')
        was_low = True
        prev = None
        initial = True
        try:
            while not self.should_stop():
                self.pause_if_needed()
                obs = self.observe()
                self.maybe_save(obs, 'waiting')
                if obs.timer_fill < .15:
                    was_low = True
                # On process launch we can join an already-running game.
                can_enter = initial or (was_low and obs.timer_fill >= .65)
                if can_enter and prev is not None and stable(prev, obs, self.geo) and not matches(obs.board):
                    if initial or obs.timer_fill >= .65:
                        initial = False
                        was_low = False
                        completed = self._play_one(obs)
                        if completed:
                            was_low = True
                        prev = None
                        if self.max_rounds and self.nrounds >= self.max_rounds:
                            break
                        continue
                prev = obs
                time.sleep(.14)
        finally:
            self.hotkeys.close()
            self.screen.close()
        LOG.info('控制程式已結束')


def learn_special(kind: str):
    """Save a visual template of an actual special gem, explicitly chosen by user."""
    pg = mouse_library()
    cal = Calibration.load()
    screen = Screen(cal)
    try:
        print(f'教學模式：{kind}；只在寶石靜止時擷取。')
        print('請把滑鼠移到目標「特殊寶石」正中央（不要按滑鼠鍵）。')
        input('保持位置，按 Enter 擷取樣本... ')
        mx, my = pg.position()
        col = round((mx - cal.x0) / cal.dx)
        row = round((my - cal.y0) / cal.dy)
        if not (0 <= row < 8 and 0 <= col < 8):
            raise ValueError('游標不在棋盤內，請重新執行 learn')
        rgb = screen.capture()
        obs = Recognizer(cal.geo(), ROOT/'assets').analyze(rgb)
        label = obs.board[row][col]
        xx = round((col+.5) * cal.dx)
        yy = round((row+.5) * cal.dy)
        side = max(10, round(min(cal.dx, cal.dy) * .85))
        tile = rgb[yy-side//2:yy-side//2+side, xx-side//2:xx-side//2+side]
        if tile.shape[:2] != (side, side):
            raise ValueError('寶石樣本超出畫面')
        gray = cv2.cvtColor(cv2.resize(tile, (32,32)), cv2.COLOR_RGB2GRAY)
        location = ROOT/'assets'
        index = sum(1 for f in location.glob(f'special_{kind}_*.png'))
        filename = f'special_{kind}_{index}.png'
        cv2.imwrite(str(location/filename), gray)
        meta = location/'specials.json'
        data = json.loads(meta.read_text(encoding='utf-8')) if meta.exists() else []
        data.append({'kind':kind, 'color':label, 'file':filename})
        meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'已學習：{kind}，棋盤格 ({row+1},{col+1})，顏色 {label}，樣本 {filename}')
        print('提示：特殊寶石有動畫與顏色變化，樣本辨識並非百分之百；可用 inspect 檢驗。')
    finally:
        screen.close()
