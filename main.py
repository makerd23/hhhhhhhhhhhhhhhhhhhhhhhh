"""CLI entrypoint for Windows / VS Code / Python 3.12."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import cv2

from vision import Geometry, Recognizer, annotate

ROOT = Path(__file__).resolve().parent


def add_logging():
    from live import LOG
    ROOT.joinpath('logs').mkdir(exist_ok=True)
    logfile = ROOT / 'logs' / 'bot.log'
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s | %(levelname)s | %(message)s',
                        datefmt='%H:%M:%S',
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(logfile, encoding='utf-8')])
    LOG.info('日誌：%s', logfile)


def offline_sample(mode='balanced'):
    from core import matches, rank_moves
    reference = ROOT / 'assets' / 'reference_board.png'
    rgb = cv2.cvtColor(cv2.imread(str(reference)), cv2.COLOR_BGR2RGB)
    geo = Geometry(20, 20, 40, 40)
    obs = Recognizer(geo, ROOT/'assets').analyze(rgb)
    print(f'辨識可信度 {obs.confidence:.3f}；未知格 {obs.unknown}；倒數條 {obs.timer_fill:.2%}')
    for row in obs.board:
        print(' '.join(row))
    print(f'尚在消除中的寶石數量：{len(matches(obs.board))}')
    for i, mv in enumerate(rank_moves(obs.board, mode)[:5], 1):
        print(f'#{i} {mv.src} -> {mv.dst} | score={mv.score} | {mv.kind}')
    annotate(obs, geo, ROOT/'debug'/'sample_diagnostic.png', mode)
    print('診斷圖片：debug/sample_diagnostic.png')


def main():
    ap = argparse.ArgumentParser(description='Bejeweled Blitz 智慧自動消除 | Python 3.12')
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('calibrate', help='記錄棋盤左上與右下中心（只需一次，視窗縮放變更需重做）')
    learn = sub.add_parser('learn', help='截取實際特殊寶石，改善高能寶石辨識')
    learn.add_argument('kind', choices=('hypercube','flame','star'))
    sample = sub.add_parser('sample', help='用內附截圖離線測試辨識、演算法（不操作滑鼠）')
    inspect = sub.add_parser('inspect', help='讀取現在遊戲畫面，儲存診斷圖（不操作滑鼠）')
    play = sub.add_parser('play', help='即時監視倒數條，開始後自動選步並交換')
    for parser in (sample, inspect, play):
        parser.add_argument('--mode', choices=('speed','balanced','power'), default='balanced')
    play.add_argument('--drag', type=float, default=.105, help='拖曳時間（秒），預設 0.105')
    play.add_argument('--once', action='store_true', help='只玩目前/下一局')
    play.add_argument('--dry-run', action='store_true', help='僅顯示預定交換，不實際拖曳')
    play.add_argument('--save-debug', action='store_true', help='將每次診斷截圖寫入 debug（大量檔案）')
    args = ap.parse_args()
    try:
        if args.command == 'sample':
            offline_sample(args.mode)
            return
        from live import Calibration, Screen, Player, calibrate, dpi_awareness
        dpi_awareness()
        if args.command == 'calibrate':
            calibrate()
            return
        if args.command == 'learn':
            from live import learn_special
            learn_special(args.kind)
            return
        cal = Calibration.load()
        if args.command == 'inspect':
            screen = Screen(cal)
            try:
                obs = Recognizer(cal.geo(), ROOT/'assets').analyze(screen.capture())
                annotate(obs, cal.geo(), ROOT/'debug'/'live_diagnostic.png', args.mode)
                print('棋盤：')
                for row in obs.board:
                    print(' '.join(row))
                print(f'辨識可信度 {obs.confidence:.3f}，未知 {obs.unknown}，計時條 {obs.timer_fill:.2%}')
                if obs.specials:
                    print(f'辨識到特殊寶石：{obs.specials}')
                print('圖片：debug/live_diagnostic.png')
                if obs.unknown > 2 or obs.confidence < .65:
                    print('警告：辨識品質低，請檢查校準/縮放，先不要開始 play。')
            finally:
                screen.close()
            return
        if args.drag < .055 or args.drag > .6:
            ap.error('--drag 必須在 0.055 到 0.6 秒之間')
        add_logging()
        Player(cal, mode=args.mode, drag_seconds=args.drag,
               dry_run=args.dry_run, max_rounds=1 if args.once else 0,
               save_debug=args.save_debug).run()
    except KeyboardInterrupt:
        print('\n已按 Ctrl+C 停止')
    except Exception as exc:
        # Include traceback in log for debugging real browser/game variants.
        try:
            import pyautogui
            if isinstance(exc, pyautogui.FailSafeException):
                print('\n已觸發 PyAutoGUI 緊急停止（滑鼠左上角）')
                return
        except Exception:
            pass
        logging.exception('執行失敗：%s', exc)
        print(f'執行失敗：{exc}')
        sys.exit(1)


if __name__ == '__main__':
    main()
