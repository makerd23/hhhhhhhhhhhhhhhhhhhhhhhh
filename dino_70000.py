# -*- coding: utf-8 -*-
"""
Chrome Dino predictive autoplayer (Windows / Python 3.12 / standard library only).

RUN: py -3.12 dino_70000.py   (or python dino_70000.py)
Chrome is launched with a SEPARATE temporary profile for localhost debugging.
No pip, packages, browser extensions, downloads, external websites, or score hacks.
This is a normal-speed autonomous run; 70,000 points are a goal, not guaranteed.
CTRL+C stops the bot. Closing the separate Chrome window stops the game.
"""
import base64
import ctypes
import threading
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


BOT_JS = r'''
(() => {
  if (window.__dinoBot && window.__dinoBot.status === 'running')
    return 'already active';
  const B = window.__dinoBot = {
    version: '2.0', status: 'loading', score: 0, highScore: 0,
    jumps: 0, ducks: 0, speed: 0, obstacle: '-', action: 'start',
    error: '', stop: false, frames: 0,
    halt() { this.stop = true; this.status = 'stopped'; }
  };

  (async () => {
    try {
      // Chromium 2025+ exports Runner from a JS module, not window.Runner.
      // Discover the URL of the loaded module relative to the page's scripts.
      const paths = [
        './dino_game/offline.js',
        'chrome://dino/dino_game/offline.js',
        'chrome://network-errors/dino_game/offline.js',
        'chrome-error://chromewebdata/dino_game/offline.js'
      ];
      for (const s of document.querySelectorAll('script[src]')) {
        try {
          paths.push(new URL('./dino_game/offline.js', s.src).href);
          paths.push(new URL('../dino_game/offline.js', s.src).href);
        } catch (_) {}
      }
      let R = null, lastError = '';
      if (window.Runner && window.Runner.instance_) {
        R = window.Runner.instance_;
      } else {
        for (let attempt=0; attempt<24 && !R; attempt++) {
          for (const path of [...new Set(paths)]) {
            try {
              const mod = await import(path);
              if (mod.Runner && typeof mod.Runner.getInstance === 'function') {
                R = mod.Runner.getInstance();
                break;
              }
            } catch (e) { lastError = String(e); }
          }
          if (!R) await new Promise(resolve => setTimeout(resolve, 250));
        }
      }
      if (!R || !R.tRex || !R.horizon) {
        throw Error('無法讀取 Chrome Dino 模組。可能是 Chrome 版本或校方政策限制。' + lastError);
      }
      B.status = 'starting';
      const T = R.tRex;
      const RUNBOX = [[22,0,17,16],[1,18,30,9],[10,35,14,8],[1,24,29,5],[5,30,21,4],[9,34,15,4]];
      const DUCKBOX = [[1,18,55,25]];
      const HORIZON = 60;
      const HOLDS = [3,5,7,9,12,15,19,25];
      let jumpClock = -1, jumpHold = 0, endedJump = false;
      let jumpedObstacle = null, speedDropped = false;

      function overlap(ax, ay, aw, ah, bx, by, bw, bh) {
        // Match the game's axis-aligned collision boxes with ~1 px margin.
        return ax < bx + bw + 1 && ax + aw + 1 > bx &&
               ay < by + bh + 1 && ay + ah + 1 > by;
      }
      function crashedWith(o, ox, dy, crouch) {
        const tw = T.config.width, th = T.config.height;
        if (!overlap(T.xPos+1,dy+1,tw-2,th-2,
                     ox+1,o.yPos+1,o.width-2,o.typeConfig.height-2)) return false;
        const boxes = crouch ? DUCKBOX : RUNBOX;
        const oboxes = o.collisionBoxes || [];
        for (const a of boxes) {
          for (const c of oboxes) {
            if (overlap(T.xPos+1+a[0],dy+1+a[1],a[2],a[3],
                        ox+1+c.x,o.yPos+1+c.y,c.width,c.height)) return true;
          }
        }
        return false;
      }

      function visibleObstacles() {
        return R.horizon.obstacles.filter(o =>
          o && o.typeConfig && o.typeConfig.type !== 'collectable' &&
          o.xPos + o.width > T.xPos - 15 && o.xPos < T.xPos + 850)
          .sort((a,b) => a.xPos-b.xPos).slice(0,3);
      }
      function speedFor(o) {
        return Math.max(1, R.currentSpeed + (o.speedOffset || 0));
      }

      // Simulate the published jump equations, the actual collision boxes,
      // and up to 3 upcoming obstacles before committing to a jump.
      function predict(obs, delay, hold, allowDuck) {
        const cfg = T.config;
        const ground = T.groundYPos;
        const jumpV = cfg.initialJumpVelocity - R.currentSpeed / 10;
        let y = ground, v = 0, inAir = false, minimum = false;
        let wasAirborne = false;
        for (let frame = 1; frame <= HORIZON; frame++) {
          if (frame === delay + 1 && !inAir) {
            inAir = true;
            wasAirborne = true;
            v = jumpV;
            minimum = false;
          }
          if (inAir) {
            y += Math.round(v);
            v += cfg.gravity;
            if (y < ground - cfg.minJumpHeight) minimum = true;
            if ((frame - delay >= hold || y < cfg.maxJumpHeight) &&
                minimum && v < cfg.dropVelocity) v = cfg.dropVelocity;
            if (y > ground) { y = ground; inAir = false; }
          }
          for (const o of obs) {
            let futureX = o.xPos - speedFor(o) * frame;
            if (futureX > T.xPos + 70 || futureX + o.width < T.xPos - 5) continue;
            const duck = allowDuck && !inAir && o.typeConfig.type === 'PTERODACTYL'
              && o.yPos >= 65 && o.yPos <= 90;
            if (crashedWith(o, futureX, y, duck))
              return {safe:false, frame, blocked:o};
          }
        }
        return {safe:true, airborne:wasAirborne};
      }

      function scoreNow() {
        try {
          return Math.floor(R.distanceMeter.getActualDistance(Math.ceil(R.distanceRan)));
        } catch (_) { return Math.floor(R.distanceRan * 0.025); }
      }
      function frame() {
        if (B.stop || !document.contains(R.canvas || document.querySelector('canvas'))) {
          B.status = 'stopped';
          if (T.ducking && !T.jumping) T.setDuck(false);
          return;
        }
        try {
          B.frames++;
          if (R.crashed) {
            B.score = scoreNow();
            B.highScore = Math.max(B.score, B.highScore);
            B.status = 'crashed';
            B.action = 'game over';
            return;
          }
          B.status = R.playing && !R.playingIntro ? 'running' : 'starting';
          B.speed = Math.round(R.currentSpeed*100)/100;
          if ((B.frames % 15) === 0) {
            B.score = scoreNow();
            B.highScore = Math.max(B.score, B.highScore);
          }
          if (R.playing && !R.playingIntro && !R.paused) {
            const obs = visibleObstacles();
            const closest = obs[0];
            B.obstacle = closest ? (closest.typeConfig.type + ' @' +
              Math.round(closest.xPos - T.xPos) + 'px') : '-';
            if (T.jumping) {
              // Short jumps matter: release as soon as the chosen height is safe.
              jumpClock++;
              if (!endedJump && jumpClock >= jumpHold) {
                T.endJump(); endedJump = true;
              }
              // End airborne time after the obstacle has completely passed.
              // Without a fast drop, high-speed jumps can overlap the next cactus.
              if (!speedDropped && jumpedObstacle &&
                  jumpedObstacle.xPos + jumpedObstacle.width < T.xPos + 1 &&
                  T.yPos < T.groundYPos - 5) {
                T.setSpeedDrop();
                speedDropped = true;
              }
              B.action = speedDropped ? 'fast drop' : 'jump';
            } else {
              jumpClock = -1;
              endedJump = false;
              jumpedObstacle = null; speedDropped = false;
              const isMidBird = closest && closest.typeConfig.type === 'PTERODACTYL'
                && closest.yPos >= 65 && closest.yPos <= 90;
              // Prefer crouching for mid-height birds; avoids a risky long jump.
              const shouldDuck = isMidBird &&
                closest.xPos < T.xPos + 190 &&
                closest.xPos + closest.width > T.xPos + 2;
              if (shouldDuck) {
                if (!T.ducking) { T.setDuck(true); B.ducks++; }
                B.action = 'duck';
              } else {
                if (T.ducking) T.setDuck(false);
                B.action = 'run';
                if (obs.length && !predict(obs, 1000, 0, true).safe) {
                  let bestDelay = -1, bestHold = 9;
                  for (let delay = 0; delay <= 42; delay++) {
                    for (const hold of HOLDS) {
                      if (predict(obs, delay, hold, true).safe) {
                        if (delay > bestDelay || (delay === bestDelay && hold < bestHold)) {
                          bestDelay = delay;
                          bestHold = hold;
                        }
                        break;
                      }
                    }
                  }
                  // If no perfect plan exists, use a short emergency jump.
                  if (bestDelay <= 1) {
                    let emergency = bestDelay < 0;
                    if (!emergency || (closest && closest.xPos < T.xPos +
                      R.currentSpeed * 12 + 55)) {
                      T.startJump(R.currentSpeed);
                      B.jumps++;
                      jumpClock = 0;
                      jumpHold = bestDelay < 0 ? 8 : bestHold;
                      endedJump = false;
                      jumpedObstacle = closest; speedDropped = false;
                      B.action = emergency ? 'emergency jump' : 'jump';
                    }
                  }
                }
              }
            }
          }
        } catch (e) {
          B.status = 'error'; B.error = String(e && e.stack || e);
          B.stop = true;
          return;
        }
        requestAnimationFrame(frame);
      }

      // Start with a real KeyboardEvent, routed to the game's own controller.
      // Do not modify speed, score, collisions, gravity, or game-over rules.
      if (!R.playing) {
        let e = new KeyboardEvent('keydown', {key:' ', code:'Space',
          bubbles:true, cancelable:true, keyCode:32, which:32});
        if (e.keyCode !== 32)
          Object.defineProperty(e, 'keyCode', {get:()=>32});
        R.onKeyDown(e);
        setTimeout(() => {
          try {
            let u = new KeyboardEvent('keyup', {key:' ',code:'Space',
              bubbles:true,cancelable:true,keyCode:32,which:32});
            if (u.keyCode !== 32)
              Object.defineProperty(u,'keyCode',{get:()=>32});
            R.onKeyUp(u);
          } catch (_) {}
        }, 125);
      }
      requestAnimationFrame(frame);
    } catch (e) {
      B.status = 'error'; B.error = String(e && e.stack || e);
    }
  })();
  return 'bot initializing';
})()
'''


class CDPError(RuntimeError):
    pass


class DevtoolsSocket:
    """Minimal RFC6455 WebSocket client for Chrome DevTools, no pip."""
    def __init__(self, url):
        from urllib.parse import urlsplit
        u = urlsplit(url)
        if u.hostname not in ('127.0.0.1', 'localhost'):
            raise CDPError('只允許連線到本機除錯服務。')
        self.sock = socket.create_connection((u.hostname, u.port), timeout=5)
        self.sock.settimeout(8)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = (f'GET {u.path}{"?" + u.query if u.query else ""} HTTP/1.1\r\n'
                   f'Host: {u.hostname}:{u.port}\r\n'
                   'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                   f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self.sock.sendall(request.encode('ascii'))
        reply = b''
        while b'\r\n\r\n' not in reply and len(reply) < 16384:
            reply += self.sock.recv(4096)
        headers, _, tail = reply.partition(b'\r\n\r\n')
        if b' 101 ' not in headers.split(b'\r\n', 1)[0]:
            raise CDPError('DevTools WebSocket 拒絕連線：' +
                           headers.decode('utf-8', 'replace')[:300])
        expected = base64.b64encode(hashlib.sha1((key +
            '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest())
        if expected.lower() not in headers.lower():
            raise CDPError('WebSocket 握手驗證失敗。')
        self.leftover = tail
        self.next_id = 0

    def read_exact(self, n):
        while len(self.leftover) < n:
            data = self.sock.recv(max(4096, n-len(self.leftover)))
            if not data:
                raise CDPError('Chrome 已關閉連線。')
            self.leftover += data
        data, self.leftover = self.leftover[:n], self.leftover[n:]
        return data

    def send_frame(self, payload, opcode=1):
        mask = secrets.token_bytes(4)
        n = len(payload)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.extend([0x80 | 126]); hdr.extend(struct.pack('!H', n))
        else:
            hdr.extend([0x80 | 127]); hdr.extend(struct.pack('!Q', n))
        hdr.extend(mask)
        hdr.extend(p ^ mask[i % 4] for i, p in enumerate(payload))
        self.sock.sendall(hdr)

    def recv_frame(self):
        while True:
            h = self.read_exact(2)
            op = h[0] & 15
            n = h[1] & 127
            if n == 126:
                n = struct.unpack('!H', self.read_exact(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self.read_exact(8))[0]
            masked = bool(h[1] & 128)
            mask = self.read_exact(4) if masked else b''
            payload = self.read_exact(n)
            if masked:
                payload = bytes(p ^ mask[i % 4] for i,p in enumerate(payload))
            if op == 8:
                raise CDPError('Chrome 已關閉 DevTools WebSocket。')
            if op == 9:
                self.send_frame(payload, 10)
                continue
            if op == 1:
                return json.loads(payload)

    def call(self, method, params=None):
        self.next_id += 1
        id_ = self.next_id
        self.send_frame(json.dumps({'id':id_, 'method':method,
                                   'params':params or {}}).encode('utf-8'))
        while True:
            message = self.recv_frame()
            if message.get('id') == id_:
                if 'error' in message:
                    raise CDPError(str(message['error']))
                return message.get('result', {})

    def eval(self, expression):
        result = self.call('Runtime.evaluate', {
            'expression':expression, 'returnByValue':True,
            'awaitPromise':False, 'userGesture':True,
            'includeCommandLineAPI':False})
        if result.get('exceptionDetails'):
            raise CDPError(str(result['exceptionDetails'])[:700])
        return result.get('result', {}).get('value')

    def close(self):
        try: self.sock.close()
        except OSError: pass


def locate_chrome():
    env = os.environ
    candidates = [
        Path(env.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
        Path(env.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
        Path(env.get('LOCALAPPDATA', str(Path.home()))) / 'Google/Chrome/Application/chrome.exe',
    ]
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
                        r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe'):
                try:
                    with winreg.OpenKey(root, sub) as handle:
                        val = winreg.QueryValueEx(handle, '')[0]
                    candidates.append(Path(val.strip('"')))
                except OSError:
                    pass
    except ImportError:
        pass
    return next((str(c) for c in candidates if c.is_file()), None)


def free_local_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def devtools_tabs(port):
    url = f'http://127.0.0.1:{port}/json/list'
    req = urllib.request.Request(url, headers={'Connection':'close'})
    with urllib.request.urlopen(req, timeout=1.0) as reply:
        return json.loads(reply.read())


def main():
    if sys.platform != 'win32':
        print('此程式為 Windows Chrome 設計；目前系統不是 Windows。')
        return 1
    chrome = locate_chrome()
    if not chrome:
        print('找不到 Google Chrome。請確認學校電腦有安裝 Chrome。')
        return 1
    port = free_local_port()
    profile = tempfile.mkdtemp(prefix='DinoBotProfile_')
    print('Chrome Dino 70K 挑戰版 | Python 標準函式庫 | 不用 pip')
    print('啟動獨立 Chrome 遊戲視窗（不會碰你的正常 Chrome 設定檔）。')
    print('遊戲中按 F8 可停止；也可以在終端按 Ctrl+C。')
    print('請保持遊戲視窗在前景，不要最小化。')
    print('此模式不改遊戲速度、分數或碰撞判定。\n')
    proc = subprocess.Popen([
        chrome,
        f'--remote-debugging-port={port}',
        '--remote-debugging-address=127.0.0.1',
        f'--user-data-dir={profile}',
        '--no-first-run', '--no-default-browser-check',
        '--new-window', 'chrome://dino/'
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
       stderr=subprocess.DEVNULL)

    ws = None
    stop_hotkey = threading.Event()
    stop_watcher_done = threading.Event()

    def monitor_f8():
        api = ctypes.WinDLL('user32', use_last_error=True)
        api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        api.GetAsyncKeyState.restype = ctypes.c_short
        while not stop_watcher_done.wait(.025):
            if api.GetAsyncKeyState(0x77) & 0x8000:  # F8
                stop_hotkey.set()
                return

    threading.Thread(target=monitor_f8, daemon=True).start()
    try:
        deadline = time.monotonic() + 12
        tab = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise CDPError('Chrome 啟動後立刻關閉。')
            try:
                tabs = devtools_tabs(port)
                tab = next((t for t in tabs if t.get('type') == 'page' and
                            ('dino' in t.get('url','').lower())), None)
                if tab and tab.get('webSocketDebuggerUrl'):
                    break
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(.25)
        if not tab:
            raise CDPError('無法連到 Chrome Dino 頁面；可能禁止遠端除錯。')
        ws = DevtoolsSocket(tab['webSocketDebuggerUrl'])
        ws.call('Runtime.enable')
        print('已連接 Chrome，載入預測演算法...')
        initial = ws.eval(BOT_JS)
        print('初始化結果：', initial)
        error_count = 0
        previous = None
        while True:
            if stop_hotkey.is_set():
                ws.eval('window.__dinoBot && window.__dinoBot.halt()')
                print('\nF8：已停止自動控制。')
                break
            if proc.poll() is not None:
                print('\nChrome 已關閉。'); break
            time.sleep(.7)
            try:
                report = ws.eval("JSON.stringify(window.__dinoBot ? ({status:__dinoBot.status,score:__dinoBot.score,high:__dinoBot.highScore,jumps:__dinoBot.jumps,ducks:__dinoBot.ducks,speed:__dinoBot.speed,action:__dinoBot.action,obstacle:__dinoBot.obstacle,error:__dinoBot.error}) : {status:'not loaded'})")
                data = json.loads(report)
            except (CDPError, OSError) as e:
                error_count += 1
                if error_count >= 3: raise CDPError('讀取遊戲狀態失敗：'+str(e))
                continue
            error_count = 0
            state = data.get('status')
            if state == 'error':
                raise CDPError(data.get('error') or '未知遊戲腳本錯誤')
            if state == 'crashed':
                print('\n挑戰結束，這一局分數：', data['score'], '；跳躍',data['jumps'],
                      '次，蹲下', data['ducks'], '次。')
                print('遊戲視窗會保留，方便你查看成績。')
                break
            if state == 'stopped':
                print('\n已停止。'); break
            if state == 'loading' and previous == 'loading':
                print('正在等待 Dino 模組載入...')
            elif state == 'running':
                sys.stdout.write('\r分數 {:>7} / 70000 | 速度 {:>4} | 跳 {:>4} | 蹲 {:>4} | {:<15} '.format(
                    data['score'], data['speed'], data['jumps'],data['ducks'],data['action']))
                sys.stdout.flush()
            elif state != previous:
                print('目前狀態：',state)
            previous = state
    except KeyboardInterrupt:
        print('\n使用者停止。')
        if ws:
            try: ws.eval('window.__dinoBot && window.__dinoBot.halt()')
            except Exception: pass
    except (CDPError, OSError, ValueError) as e:
        print('\n[無法執行]', e)
        print('請先確認 Chrome 可以開啟 chrome://dino，且沒有禁止遠端除錯。')
        return 1
    finally:
        stop_watcher_done.set()
        if ws: ws.close()
        print('\n關閉遊戲視窗即可完全結束；本程式沒有下載或安裝模組。')
    return 0


if __name__ == '__main__':
    try: sys.exit(main())
    except KeyboardInterrupt: print('\n已停止。')
