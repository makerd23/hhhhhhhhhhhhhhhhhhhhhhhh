# -*- coding: utf-8 -*-
"""Chrome Dino v5. Windows + Python 3.12 standard library only.

Run: python dino_70000_v5.py
F8 or Ctrl+C: stop. One Chrome debug profile, reused across runs.

v5 deliberately does NOT use pixel fallback: the v4 fallback misidentified
scenery as obstacles and caused repeated jumps. If native game state cannot
be captured, the bot fails safely instead of blindly pressing jump.
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

DINO_URL = 'chrome://dino/'

HOOK_JS = r'''(() => {
  // Runs BEFORE Chromium's offline game script. Capture its existing Runner
  // through the method it binds for its high-score callback.
  if (window.__dinoCaptureInstalled) return;
  Object.defineProperty(window, '__dinoCaptureInstalled', {value:true});
  const nativeBind = Function.prototype.bind;
  Function.prototype.bind = function(...args) {
    const owner = args[0];
    try {
      if (!window.__dinoNativeRunner && owner && typeof owner === 'object' &&
          (this.name === 'initializeHighScore' ||
           ('distanceRan' in owner && 'horizon' in owner &&
            'tRex' in owner && 'currentSpeed' in owner))) {
        window.__dinoNativeRunner = owner;
        // Leave the browser's native bind untouched after the one-time capture.
        Function.prototype.bind = nativeBind;
      }
    } catch (_) {}
    return Reflect.apply(nativeBind, this, args);
  };
})()'''

BOT_JS = r'''(() => {
  if (window.__dinoBot && window.__dinoBot.status === 'running')
    return 'already active';
  const B = window.__dinoBot = {
    version:'5.0', status:'loading', mode:'native', action:'wait',
    score:0, highScore:0, jumps:0, ducks:0, speed:0,
    obstacle:'-', error:'', stop:false, frames:0,
    halt() { this.stop=true; this.status='stopped'; }
  };
  let stopped = false;
  (async () => {
    try {
      const captureStart = performance.now();
      let R;
      while (performance.now() - captureStart < 6000 && !B.stop) {
        R = window.__dinoNativeRunner || window.Runner?.instance_ || null;
        if (R && R.tRex && R.horizon && Array.isArray(R.horizon.obstacles)) break;
        await new Promise(r=>setTimeout(r, 60));
      }
      if (B.stop) return;
      if (!R || !R.tRex || !R.horizon || !Array.isArray(R.horizon.obstacles)) {
        B.status='error';
        B.error='未取得遊戲原生障礙物資料，安全停止（不會改用可能誤判的像素辨識）。請提供終端機畫面與 Chrome 版本。';
        return;
      }
      const T=R.tRex;
      const JUMPBOX=[[22,0,17,16],[1,18,30,9],[10,35,14,8],[1,24,29,5],[5,30,21,4],[9,34,15,4]];
      const DUCKBOX=[[1,18,55,25]];
      const HOLD_FRAMES=[3,5,7,9,12,15,19];
      let activeJump=null, jumpFrames=0, jumpHold=0, released=false, dropped=false;
      let handledObstacle=null, lastDucking=false;
      let previousTime=performance.now();

      function overlap(x1,y1,w1,h1,x2,y2,w2,h2) {
        return x1<x2+w2 && x1+w1>x2 && y1<y2+h2 && y1+h1>y2;
      }
      function collisions(o,x,y,duck) {
        if (!overlap(T.xPos+1,y+1,T.config.width-2,T.config.height-2,
                     x+1,o.yPos+1,o.width-2,o.typeConfig.height-2)) return false;
        const tBoxes=duck?DUCKBOX:JUMPBOX;
        for (const t of tBoxes) for (const b of o.collisionBoxes || []) {
          if (overlap(T.xPos+1+t[0],y+1+t[1],t[2],t[3],
                      x+1+b.x,o.yPos+1+b.y,b.width,b.height)) return true;
        }
        return false;
      }
      function isMidBird(o) {
        return o.typeConfig.type==='PTERODACTYL' && o.yPos>61 && o.yPos<91;
      }
      function isHighBird(o) {
        return o.typeConfig.type==='PTERODACTYL' && o.yPos<=61;
      }
      function getObstacles() {
        return R.horizon.obstacles.filter(o => o && o.typeConfig &&
          o.typeConfig.type!=='collectable' &&
          o.xPos+o.width>T.xPos+1 && o.xPos<T.xPos+750)
          .sort((a,b)=>a.xPos-b.xPos).slice(0,3);
      }
      function speedOf(o) { return Math.max(1,R.currentSpeed+(o.speedOffset||0)); }
      function predict(obs,delay,hold) {
        // 56 future frames (~0.93 s); same jump dynamics and collision boxes.
        const cfg=T.config, ground=T.groundYPos;
        let y=ground,v=cfg.initialJumpVelocity-R.currentSpeed/10;
        let airborne=false, minHeight=false;
        for (let f=1;f<=56;f++) {
          if (f===delay+1) airborne=true;
          if (airborne) {
            y+=Math.round(v); v+=cfg.gravity;
            if (y<ground-cfg.minJumpHeight) minHeight=true;
            if ((f-delay>=hold || y<cfg.maxJumpHeight) &&
                minHeight && v<cfg.dropVelocity) v=cfg.dropVelocity;
            if (y>=ground) {y=ground;airborne=false;}
          }
          for (const o of obs) {
            const x=o.xPos-speedOf(o)*f;
            if (x>T.xPos+90 || x+o.width<T.xPos-6) continue;
            // Mid-height birds are safely ducked while on the ground.
            const duck=!airborne && isMidBird(o);
            if (collisions(o,x,y,duck)) return false;
          }
        }
        return true;
      }
      function currentScore() {
        try {return Math.floor(R.distanceMeter.getActualDistance(Math.ceil(R.distanceRan)));}
        catch (_) {return Math.round((R.distanceRan||0)*0.025);}
      }
      function clearControls() {
        try { if (T.ducking&&!T.jumping) T.setDuck(false); } catch (_) {}
      }
      function frame(now) {
        if (B.stop || !document.querySelector('canvas.runner-canvas, canvas')) {
          clearControls(); B.status='stopped'; return;
        }
        try {
          B.frames++;
          if (R.crashed) {
            B.score=currentScore(); B.highScore=Math.max(B.highScore,B.score);
            B.action='game over'; B.status='crashed';return;
          }
          B.status=R.playing&&!R.playingIntro&&!R.paused?'running':'starting';
          B.speed=Number(R.currentSpeed.toFixed(2));
          if (B.frames%12===0) {
            B.score=currentScore();B.highScore=Math.max(B.highScore,B.score);
          }
          if (R.playing && !R.playingIntro && !R.paused) {
            const obs=getObstacles(), first=obs[0]||null;
            B.obstacle=first?first.typeConfig.type+' x='+Math.round(first.xPos)+' y='+Math.round(first.yPos):'-';
            if (handledObstacle && handledObstacle.xPos+handledObstacle.width<T.xPos-8)
              handledObstacle=null;
            if (T.jumping) {
              jumpFrames++;
              if (!released && jumpFrames>=jumpHold) {T.endJump();released=true;}
              if (activeJump && !dropped &&
                  activeJump.xPos+activeJump.width<T.xPos-4 &&
                  T.yPos<T.groundYPos-5) {
                T.setSpeedDrop();dropped=true;
              }
              B.action=dropped?'fall':'jump';
            } else {
              activeJump=null; jumpFrames=0; released=false; dropped=false;
              // Only this specific bird altitude is handled by crouching.
              const mid=first && isMidBird(first) &&
                  first.xPos<T.xPos+180 && first.xPos+first.width>T.xPos;
              if (mid) {
                if (!T.ducking){T.setDuck(true);B.ducks++;}
                B.action='duck';
              } else {
                if (T.ducking) T.setDuck(false);
                B.action=first?'tracking':'no obstacle';
                // No obstacle => NO ACTION. An obstacle is processed only once.
                if (first && first!==handledObstacle && !isHighBird(first) &&
                    first.xPos<T.xPos+420 && obs.length &&
                    !predict(obs,1000,0)) {
                  let bestDelay=-1,holdPick=8;
                  // Prefer the latest safe launch, not a jump at first sight.
                  for(let delay=32;delay>=0;delay--) {
                    for(const hold of HOLD_FRAMES) {
                      if(predict(obs,delay,hold)){
                        bestDelay=delay;holdPick=hold;break;
                      }
                    }
                    if(bestDelay>=0)break;
                  }
                  // Emergency jump only when obstacle is physically close.
                  const near=first.xPos<T.xPos+Math.min(180,Math.max(85,R.currentSpeed*12));
                  if ((bestDelay===0 || (bestDelay<0&&near)) && !T.jumping) {
                    T.startJump(R.currentSpeed);
                    B.jumps++;B.action=bestDelay===0?'jump':'emergency';
                    activeJump=first;handledObstacle=first;
                    jumpFrames=0;jumpHold=holdPick;released=false;dropped=false;
                  }
                }
              }
            }
          }
        } catch(e) {
          clearControls(); B.status='error';B.error=String(e?.stack||e);return;
        }
        requestAnimationFrame(frame);
      }
      B.status='starting'; B.action='原生障礙物已連線';
      if (!R.playing && !R.crashed) {
        // One start keypress only (not a periodic jump generator).
        const e=(type)=>{
          const v=new KeyboardEvent(type,{key:' ',code:'Space',bubbles:true,cancelable:true});
          Object.defineProperty(v,'keyCode',{get:()=>32});
          Object.defineProperty(v,'which',{get:()=>32});
          return v;
        };
        R.onKeyDown(e('keydown'));
        setTimeout(()=>{try{R.onKeyUp(e('keyup'));}catch(_){}},110);
      }
      requestAnimationFrame(frame);
    } catch(e){B.status='error';B.error=String(e?.stack||e);}
  })();
  return 'native obstacle detector initializing';
})()'''

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


def read_owned_debugger(profile):
    """Find a live DevTools endpoint created for *this* dedicated profile."""
    port_file = profile / 'DevToolsActivePort'
    try:
        port = int(port_file.read_text(encoding='utf-8').splitlines()[0])
        if not (1 <= port <= 65535):
            return None
        tabs = devtools_tabs(port)
        if not isinstance(tabs, list):
            return None
        return port, tabs
    except (OSError, ValueError, IndexError, urllib.error.URLError, TimeoutError):
        return None


def single_instance_mutex():
    """Prevent pressing Run again in VS Code from spawning more Chrome windows."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateMutexW(None, 1, r'Local\Dino_70000_Python_OnlyOne_2026')
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        return kernel, None
    return kernel, handle


def run_one_session():
    chrome = locate_chrome()
    if not chrome:
        print('找不到 Google Chrome。請確認電腦已安裝。')
        return 1

    # Same controlled profile across all runs. An existing browser is REUSED,
    # never launched again just because VS Code starts another session.
    profile = Path(tempfile.gettempdir()) / 'DinoBot70000ChromeProfile_v4'
    profile.mkdir(parents=True, exist_ok=True)
    print('Chrome Dino v5 | 原生障礙物偵測 | 失敗就安全停止 | 無需安裝模組', flush=True)
    print('只會開一個 Chrome 遊戲視窗；有現成的就重複利用。', flush=True)
    print('自動進入 chrome://dino/；不使用容易誤判的像素辨識。', flush=True)
    print('F8 停止 | Ctrl+C 停止 | 保持 Chrome 視窗不要最小化。\n', flush=True)

    proc = None
    reused = False
    debugger = read_owned_debugger(profile)
    if debugger:
        port, tabs = debugger
        reused = True
        print('偵測到先前的遊戲 Chrome：直接沿用，不再開新視窗。', flush=True)
    else:
        # Remove a stale port file only after verifying the endpoint is dead.
        try:
            (profile / 'DevToolsActivePort').unlink(missing_ok=True)
        except OSError:
            pass
        print('第一次啟動：開啟一個獨立的 Chrome 視窗...', flush=True)
        proc = subprocess.Popen([
            chrome, '--remote-debugging-port=0',
            '--remote-debugging-address=127.0.0.1',
            f'--user-data-dir={profile}',
            '--no-first-run', '--no-default-browser-check',
            '--new-window', DINO_URL
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
           stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 16
        debugger = None
        while time.monotonic() < deadline:
            debugger = read_owned_debugger(profile)
            if debugger:
                break
            if proc.poll() is not None:
                # Existing Chrome may own this profile without DevTools,
                # in which case launching another window cannot fix it.
                raise CDPError('Chrome 很快結束，可能已有舊遊戲視窗使用同一個資料夾。請先關閉舊遊戲視窗。')
            time.sleep(.15)
        if not debugger:
            raise CDPError('Chrome 無法啟動本機除錯功能；已停止重試，不會再開新視窗。')
        port, tabs = debugger

    ws = None
    stop_hotkey = threading.Event()
    stop_watcher_done = threading.Event()
    initialized = False

    def monitor_f8():
        api = ctypes.WinDLL('user32', use_last_error=True)
        api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        api.GetAsyncKeyState.restype = ctypes.c_short
        while not stop_watcher_done.wait(.03):
            if api.GetAsyncKeyState(0x77) & 0x8000:
                stop_hotkey.set()
                return

    threading.Thread(target=monitor_f8, daemon=True).start()
    try:
        # Explicit navigation is the key fix. Chrome sometimes initially shows
        # Google/New Tab despite the CLI asking for chrome://dino/.
        tab = next((t for t in tabs if t.get('type') == 'page' and
                    'chrome://dino' in t.get('url','')), None)
        if tab is None:
            tab = next((t for t in tabs if t.get('type') == 'page' and
                        t.get('webSocketDebuggerUrl')), None)
        if tab is None or not tab.get('webSocketDebuggerUrl'):
            raise CDPError('Chrome 已啟動，但沒有找到可控制的分頁。')

        ws = DevtoolsSocket(tab['webSocketDebuggerUrl'])
        ws.call('Page.enable')
        ws.call('Runtime.enable')
        # Install before a clean reload; Chromium's Runner binds a high-score
        # callback during construction. Capture that exact running instance.
        ws.call('Page.addScriptToEvaluateOnNewDocument', {'source':HOOK_JS})
        # Chrome internally renders chrome://dino/ as a *network error page*.
        # CDP therefore reports ERR_INTERNET_DISCONNECTED even for a working game.
        # Always check the actual loaded page instead of treating that as fatal.
        tabs_now = devtools_tabs(port)
        current = next((t for t in tabs_now if t.get('id') == tab.get('id')), {})
        if 'chrome://dino' in current.get('url', ''):
            ws.call('Page.reload', {'ignoreCache':True})
        else:
            navigation = ws.call('Page.navigate', {'url': DINO_URL})
            problem = navigation.get('errorText', '')
            if problem and problem != 'net::ERR_INTERNET_DISCONNECTED':
                raise CDPError('Chrome 無法開啟小恐龍：'+problem)
        page_ready = False
        last_state = None
        deadline = time.monotonic() + 14
        while time.monotonic() < deadline:
            if stop_hotkey.is_set():
                print('F8：已停止。')
                return 0
            try:
                state = ws.eval("""({
                    url:location.href, title:document.title,
                    ready:document.readyState,
                    canvas:!!document.querySelector('canvas'),
                    gameAssets:!!document.getElementById('offline-resources'),
                    text:document.body?.innerText.slice(0,150) || ''
                })""")
                last_state = state
                remote_tabs = devtools_tabs(port)
                actual = next((t for t in remote_tabs if t.get('id') == tab.get('id')), {})
                dino_tab = 'chrome://dino' in actual.get('url','')
                # location.href may be chrome-error://chromewebdata/!
                has_game = bool(state and (state.get('canvas') or
                                (state.get('gameAssets') and
                                 ('space' in state.get('text','').lower() or
                                  'dino' in state.get('title','').lower()))))
                if dino_tab and has_game and state.get('ready') in ('interactive','complete'):
                    page_ready = True
                    break
            except (CDPError, TimeoutError, OSError, urllib.error.URLError):
                pass
            time.sleep(.25)
        if not page_ready:
            raise CDPError('Chrome 沒有成功顯示小恐龍畫面。診斷：'+repr(last_state)[:450])

        print('已進入 chrome://dino/，載入 AI 避障控制...', flush=True)
        print('初始化：', ws.eval(BOT_JS), flush=True)
        initialized = True
        previous = None
        errors = 0
        while True:
            if stop_hotkey.is_set():
                try: ws.eval('window.__dinoBot && window.__dinoBot.halt()')
                except Exception: pass
                print('\nF8：已停止自動控制。', flush=True)
                break
            if proc is not None and proc.poll() is not None:
                print('\nChrome 視窗已關閉。', flush=True)
                break
            time.sleep(.7)
            try:
                report = ws.eval("JSON.stringify(window.__dinoBot ? ({status:__dinoBot.status,score:__dinoBot.score,high:__dinoBot.highScore,jumps:__dinoBot.jumps,ducks:__dinoBot.ducks,speed:__dinoBot.speed,action:__dinoBot.action,obstacle:__dinoBot.obstacle,error:__dinoBot.error}) : {status:'not loaded'})")
                data = json.loads(report)
            except (CDPError, OSError, ValueError) as exc:
                errors += 1
                if errors >= 3:
                    raise CDPError('讀取遊戲狀態失敗：'+str(exc))
                continue
            errors = 0
            state = data.get('status')
            if state == 'error':
                raise CDPError(data.get('error') or '未知遊戲腳本錯誤')
            if state == 'crashed':
                print('\n挑戰結束。得分', data['score'], '| 跳躍',data['jumps'],
                      '| 蹲下',data['ducks'], flush=True)
                print('視窗保留供你查看成績。', flush=True)
                break
            if state == 'stopped':
                print('\n已停止。', flush=True)
                break
            if state == 'loading' and previous == 'loading':
                print('正在等待 Dino 模組載入...', flush=True)
            elif state == 'running':
                score = str(data['score']) if data['score'] is not None else '畫面顯示'
                sys.stdout.write('\r分數 {:>7} | 速度 {:>4} | 跳 {:>4} | 蹲 {:>4} | {:<13} | 障礙 {:<28} '.format(
                    score, data['speed'], data['jumps'], data['ducks'],
                    data['action'],data.get('obstacle','-')))
                sys.stdout.flush()
            elif state != previous:
                print('狀態：', state, flush=True)
            previous = state
        return 0
    except KeyboardInterrupt:
        print('\nCtrl+C：已停止。', flush=True)
        if ws:
            try: ws.eval('window.__dinoBot && window.__dinoBot.halt()')
            except Exception: pass
        return 0
    except (CDPError, OSError, ValueError) as exc:
        print('\n[錯誤]', exc, flush=True)
        print('已停止，不會重新開啟 Chrome。請把上面的錯誤訊息截圖傳給我。', flush=True)
        # Never leave the newly created erroneous Chrome window behind.
        # Do not kill a previously existing/reused browser session.
        # Do not close Chrome upon a diagnostic error; user can inspect the game.
        # Repeated Python runs reuse the same dedicated profile/Chrome window.
        return 1
    finally:
        stop_watcher_done.set()
        if ws: ws.close()
        print('\n本次程式已結束。', flush=True)


def main():
    if sys.platform != 'win32':
        print('此程式只支援 Windows 10/11。')
        return 1
    kernel, mutex = single_instance_mutex()
    if mutex is None:
        print('小恐龍程式已經在執行！本次不再開啟 Chrome。')
        print('請回到之前的 VS Code 終端機；若要停止可按 F8。')
        return 0
    try:
        return run_one_session()
    finally:
        kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)

if __name__ == '__main__':
    try: sys.exit(main())
    except KeyboardInterrupt: print('\n已停止。')
