"""Coordinate-based Windows Dino bot. Python standard library only."""
import argparse
import ctypes
from ctypes import wintypes
import statistics
import sys
import time


def brightness(color):
    if color == 0xFFFFFFFF:
        raise OSError('無法讀取螢幕像素，請確認偵測點在螢幕內。')
    return sum((color >> shift) & 255 for shift in (0, 8, 16)) / 3


class Windows:
    def __init__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.gdi = ctypes.WinDLL('gdi32', use_last_error=True)
        self.user.GetDC.argtypes = [wintypes.HWND]
        self.user.GetDC.restype = wintypes.HDC
        self.user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE,
                                         wintypes.DWORD, ctypes.c_size_t]
        self.gdi.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        self.gdi.GetPixel.restype = wintypes.DWORD
        self.gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        self.gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        self.gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        self.gdi.SelectObject.restype = wintypes.HANDLE
        self.gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        self.gdi.DeleteDC.argtypes = [wintypes.HDC]
        self.gdi.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HDC,
                                   ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        self.user.SetProcessDPIAware()

    def pressed(self, code):
        return bool(self.user.GetAsyncKeyState(code) & 0x8000)

    def wait(self, code):
        while not self.pressed(code):
            if self.pressed(0x77):
                raise KeyboardInterrupt
            time.sleep(0.015)
        while self.pressed(code):
            if self.pressed(0x77):
                raise KeyboardInterrupt
            time.sleep(0.015)

    def chrome(self):
        window = self.user.GetForegroundWindow()
        title = ctypes.create_unicode_buffer(512)
        self.user.GetWindowTextW(window, title, len(title))
        if 'chrome' not in title.value.lower():
            raise ValueError('請先切到 Chrome 遊戲頁面，再按功能鍵。')
        return window

    def space(self, down):
        self.user.keybd_event(0x20, 0, 0 if down else 2, 0)


class PixelFrame:
    """One screen copy per iteration; all pixel reads use a memory bitmap."""
    def __init__(self, win, screen, points, lookahead=0):
        self.api = win.gdi
        self.gdi = self  # Same sampling interface as Windows.
        self.screen = screen
        self.left = min(p[0] for p in points)-20
        self.top = min(p[1] for p in points)-70
        self.width = max(p[0] for p in points)+21+lookahead-self.left
        self.height = max(p[1] for p in points)+6-self.top
        self.memory = self.api.CreateCompatibleDC(screen)
        self.bitmap = self.api.CreateCompatibleBitmap(screen, self.width, self.height)
        self.original = None
        if not self.memory or not self.bitmap:
            self.close()
            raise OSError('無法建立像素擷取緩衝區。')
        self.original = self.api.SelectObject(self.memory, self.bitmap)
        if not self.original or self.original == ctypes.c_void_p(-1).value:
            self.original = None
            self.close()
            raise OSError('無法選取像素擷取點陣圖。')

    def capture(self):
        if not self.api.BitBlt(self.memory, 0, 0, self.width, self.height,
                               self.screen, self.left, self.top, 0x00CC0020):
            raise OSError('無法擷取偵測區域。')

    def GetPixel(self, unused_dc, x, y):
        return self.api.GetPixel(self.memory, x-self.left, y-self.top)

    def close(self):
        if self.original:
            self.api.SelectObject(self.memory, self.original)
        if self.bitmap:
            self.api.DeleteObject(self.bitmap)
        if self.memory:
            self.api.DeleteDC(self.memory)
        self.memory = self.bitmap = self.original = None


def sample_hits(win, dc, point, contrast):
    x, y = point
    background = statistics.median(
        brightness(win.gdi.GetPixel(dc, x+offset, y-70))
        for offset in (-20, 0, 20))
    return sum(
        abs(brightness(win.gdi.GetPixel(dc, x+ox, y+oy))-background) >= contrast
        for oy in (-5, 0, 5) for ox in (-18, -12, -6, 0))


def choose_action(lower_hits, upper_hits, ready):
    return 'jump' if ready and (lower_hits >= 2 or upper_hits >= 2) else 'wait'


def scan_edges(frame, dc, point, contrast, reach=200):
    """Locate obstacle left edges along a narrow horizontal lane."""
    x, y = point
    background = statistics.median(
        brightness(frame.gdi.GetPixel(dc, x+offset, y-70))
        for offset in (-20, 0, 20))
    edges = []
    previous_hit = False
    for column in range(x-18, x+reach+1, 4):
        hit = sum(abs(brightness(frame.gdi.GetPixel(dc, column, y+dy))-
                      background) >= contrast for dy in (-5, 0, 5)) >= 2
        if hit and not previous_hit:
            edges.append(column)
        previous_hit = hit
    return edges


def estimate_speed(previous, current, dt, speed, left_bounds):
    if not 0.035 <= dt <= 0.2:
        return speed
    measurements = []
    for old_lane, new_lane, boundary in zip(previous, current, left_bounds):
        for old_x in old_lane:
            candidates = [(old_x-x)/dt for x in new_lane
                          if x > boundary+4 and 120 <= (old_x-x)/dt <= 1800]
            if candidates:
                measured = min(candidates, key=lambda value: abs(value-speed))
                if 0.5*speed <= measured <= 1.8*speed:
                    measurements.append(measured)
    if measurements:
        return max(120, min(1800, 0.8*speed+0.2*statistics.median(measurements)))
    return speed


def scan_region(frame, dc, bounds, contrast, reach=200):
    """Sample across the full height, rather than two horizontal lanes."""
    left, top, right, bottom = bounds
    background = statistics.median(
        brightness(frame.gdi.GetPixel(dc, x, top-15))
        for x in (left, (left+right)//2, right))
    edges = []
    previous_hit = False
    for x in range(left, right+reach+1, 4):
        count = 0
        for y in range(top, bottom+1, 3):
            if abs(brightness(frame.gdi.GetPixel(dc, x, y))-background) >= contrast:
                count += 1
                if count >= 2:
                    break
        hit = count >= 2
        if hit and not previous_hit:
            edges.append(x)
        previous_hit = hit
    return edges


def advance_pixels(speed, lead):
    # Calibration supplies a working starting distance; add only the increase.
    return round(min(160, max(0, (speed-360)*lead)))


def run(args):
    win = Windows()
    print('純 Python 內建功能版本，不需要 pip 或框選視窗。', flush=True)
    print('1. 開啟 chrome://dino，恐龍保持站立，使用預設縮放。', flush=True)
    print('2. 切到 Chrome，滑鼠放在恐龍前方偵測區塊的左上角，約地面上方 60 像素。', flush=True)
    print('3. 按 F9 記錄左上角。區塊不要包含恐龍、地面、分數或雲。F8 停止。', flush=True)
    win.wait(0x78)
    target = win.chrome()
    point = wintypes.POINT()
    if not win.user.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    left, top = point.x, point.y
    print('已記錄左上角 ({}, {})。'.format(left, top), flush=True)
    print('4. 滑鼠放到區塊右下角，建議寬 30–50、高 50–55 像素，下緣離地約 5–10 像素，按 F9。', flush=True)
    win.wait(0x78)
    if win.chrome() != target:
        raise ValueError('Chrome 視窗已更換，請重新執行。')
    if not win.user.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    right, bottom = point.x, point.y
    if not 12 <= right-left <= 400 or not 8 <= bottom-top <= 160:
        raise ValueError('請先選左上角，再選右下角；寬需 12–400、高需 8–160 像素。')
    bounds = (left, top, right, bottom)
    print('已記錄區塊 {}。移開滑鼠，再按 F9 開始；F8 停止。'.format(bounds), flush=True)
    win.wait(0x78)
    if win.chrome() != target:
        raise ValueError('Chrome 視窗已更換，請重新執行。')
    dc = win.user.GetDC(None)
    if not dc:
        raise ctypes.WinError(ctypes.get_last_error())
    release_at = 0.0
    jumps = frames = 0
    report_at = time.perf_counter()
    frame = None
    speed = 360.0
    previous_edges = ([],)
    measured_at = time.perf_counter()
    try:
        # PixelFrame includes a 70-pixel top margin for background sampling.
        frame = PixelFrame(win, dc, ((left, top), (right, bottom)), lookahead=200)
        win.space(True)
        time.sleep(0.05)
        win.space(False)
        last_jump = time.perf_counter()
        print('已送出開始按鍵。偵測中；F8 停止。', flush=True)
        while not win.pressed(0x77):
            started = time.perf_counter()
            if win.user.GetForegroundWindow() != target:
                print('前景視窗已改變，停止。', flush=True)
                break
            if release_at and started >= release_at:
                win.space(False)
                release_at = 0.0
            frame.capture()
            captured_at = time.perf_counter()
            edges = (scan_region(frame, dc, bounds, args.contrast),)
            if captured_at-measured_at >= 0.05:
                speed = estimate_speed(previous_edges, edges, captured_at-measured_at,
                                       speed, (left,))
                previous_edges = edges
                measured_at = captured_at
            advance = advance_pixels(speed, args.lead)
            hits = 2*sum(x <= right+advance for x in edges[0])
            action = choose_action(hits, 0,
                                   started-last_jump >= args.cooldown)
            if action == 'jump':
                win.space(True)
                release_at = time.perf_counter()+args.hold
                last_jump = started
                jumps += 1
                print('障礙物：送出第 {} 次跳躍'.format(jumps), flush=True)
            frames += 1
            if args.debug and started-report_at >= 2:
                print('偵測 {:.1f} 次/秒；區塊障礙物 {}；估速 {:.0f} px/s；提前 {} px'.format(
                    frames/(started-report_at), hits//2,
                    speed, advance), flush=True)
                frames = 0
                report_at = started
            time.sleep(max(0, args.interval-(time.perf_counter()-started)))
    finally:
        win.space(False)
        if frame is not None:
            frame.close()
        win.user.ReleaseDC(None, dc)


def main():
    parser = argparse.ArgumentParser(description='小恐龍座標偵測：只用 Python 內建功能（Windows）')
    parser.add_argument('--hold', type=float, default=0.18, help='空白鍵按住秒數')
    parser.add_argument('--cooldown', type=float, default=0.55, help='兩次跳躍最短間隔秒數')
    parser.add_argument('--interval', type=float, default=0.015, help='偵測迴圈最短秒數')
    parser.add_argument('--contrast', type=int, default=65, help='與背景的亮度差門檻')
    parser.add_argument('--debug', action='store_true', help='輸出偵測頻率')
    parser.add_argument('--lead', type=float, default=0.22, help='加速後額外提前秒數')
    args = parser.parse_args()
    if not 0.05 <= args.lead <= 0.5:
        parser.error('lead 應介於 0.05–0.5 秒')
    if not 0.03 <= args.hold < args.cooldown <= 2:
        parser.error('需滿足 0.03 <= hold < cooldown <= 2')
    if not 0.005 <= args.interval <= 0.2 or not 10 <= args.contrast <= 200:
        parser.error('interval 應介於 0.005–0.2，contrast 應介於 10–200')
    if sys.platform != 'win32':
        parser.error('目前只支援 Windows')
    try:
        run(args)
    except KeyboardInterrupt:
        print('已停止。')
    except (OSError, ValueError) as exc:
        print('無法執行：', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
