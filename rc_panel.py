#!/usr/bin/env python3
"""rc_panel — session control for the Mac mini, served over Tailscale.

Fills the gap the Claude app leaves: there is no way to END a live session from
a card, so the box drifts to its 13-session ceiling and the queue stops moving.
This serves the same catalogue with three working buttons per row.

Binds to the Tailscale address only. That interface IS the perimeter: the
tailnet is authenticated, and nothing here should ever answer on 0.0.0.0.

Actions are threaded because a restore holds the connection for ~30 s while it
waits for the app link, and a single-threaded server would freeze the page.
"""

import gzip
import hashlib
import html
import json
import os
import sys as _sys_cfg
_sys_cfg.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rc_config as CFG  # noqa: E402
import time
import re
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, CFG.HERE)
import rc_control as C  # noqa: E402
try:
    import rc_game as GAME  # noqa: E402
except Exception as _exc:          # the panel must come up without the game
    GAME = None
    sys.stderr.write("[game] disabled: %r\n" % (_exc,))
try:
    # search across every session (titles, summaries, conversation text)
    import rc_search as SEARCH  # noqa: E402
except Exception as _exc:          # the panel must come up without it
    SEARCH = None
    sys.stderr.write("[search] disabled: %r\n" % (_exc,))
# Not part of this edition; kept as None so old branches stay inert.
FUNNELS = REELS = LINKS = PROD = STYLES = None

BIND = CFG.PANEL_BIND
PORT = CFG.PANEL_PORT

CSS = """
:root{--ground:#f4f6f8;--surface:#fff;--surface-2:#eef1f5;--ink:#161a21;--ink-2:#3d4756;
--muted:#69748a;--line:#dde2ea;--live:#0d6f5c;--live-bg:#e2f2ee;--queue:#8a5d0d;
--queue-bg:#f7eeda;--idle:#55617a;--warn:#8a2f1d;--warn-bg:#fbeae6;--warn-line:#c2543c;
--accent:#b8801d;--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
--sans:ui-sans-serif,-apple-system,"Helvetica Neue",Arial,sans-serif}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){
--ground:#0e1116;--surface:#161b23;--surface-2:#1b212b;--ink:#e7ebf2;--ink-2:#b7c0ce;
--muted:#8b95a7;--line:#262d39;--live:#4cc3a3;--live-bg:#12302a;--queue:#e0ac4c;
--queue-bg:#332614;--idle:#93a0b5;--warn:#f0917a;--warn-bg:#331813;--warn-line:#a8503a;
--accent:#e0ac4c}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.5}
.wrap{max-width:1000px;margin:0 auto;padding:30px 18px 70px;display:flex;flex-direction:column;gap:24px}
header{display:flex;flex-direction:column;gap:9px;border-bottom:2px solid var(--ink);padding-bottom:16px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
h1{margin:0;font-size:clamp(22px,3.6vw,30px);letter-spacing:-.02em;font-weight:640}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:1px;
background:var(--line);border:1px solid var(--line);border-radius:9px;overflow:hidden}
.stat{background:var(--surface);padding:11px 13px;display:flex;flex-direction:column;gap:2px}
.stat b{font-family:var(--mono);font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
.stat span{font-size:10.5px;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;font-family:var(--mono)}
.stat.hot b{color:var(--warn)}
.warn{background:var(--warn-bg);border:1px solid var(--warn-line);border-left:3px solid var(--warn-line);
border-radius:8px;padding:11px 14px;color:var(--warn);font-size:13px}
#flash{display:none;cursor:pointer;border-radius:8px;padding:11px 14px;font-size:13px;font-family:var(--mono);
background:var(--surface-2);border:1px solid var(--line);color:var(--ink)}
#flash.on{display:block}
.grp{display:flex;flex-direction:column;gap:8px}
.grp h2{margin:0;font-size:12px;font-family:var(--mono);letter-spacing:.1em;text-transform:uppercase;
display:flex;gap:9px;align-items:baseline;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:6px}
.gcount{color:var(--muted);text-transform:none;letter-spacing:.03em}
.stat.busy b{color:var(--live)}
.bidle{font-family:var(--mono);font-size:10px;color:var(--muted)}
.opt{font-family:var(--mono);font-size:10px;color:var(--accent);border:1px solid var(--line);
border-radius:4px;padding:1px 5px}
.acts input{font:inherit;font-size:12px;padding:5px 8px;border-radius:6px;
border:1px solid var(--line);background:var(--surface);color:var(--ink)}
#authurl{word-break:break-all}
#bulkbar select{font:inherit;font-size:12px;padding:4px 6px;border-radius:6px;
border:1px solid var(--line);background:var(--surface);color:var(--ink)}
.meta{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.uid,.meta time{font-family:var(--mono);font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.uid{color:var(--ink);opacity:.85}
.ctx{margin:0;color:var(--ink-2);font-size:13px}
.acts{display:flex;gap:6px;flex-wrap:wrap}
button{font-family:var(--mono);font-size:11px;letter-spacing:.03em;padding:4px 10px;border-radius:5px;
border:1px solid var(--line);background:var(--surface-2);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--muted)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button[disabled]{opacity:.45;cursor:progress}
button.danger{color:var(--warn);border-color:var(--warn-line)}
button.go{color:var(--live);border-color:var(--live)}
a.lnk{font-family:var(--mono);font-size:11px;color:var(--live)}
pre#report{margin:0;overflow-x:auto;background:var(--surface);border:1px solid var(--line);
border-radius:7px;padding:11px 13px;font-family:var(--mono);font-size:11.5px;color:var(--ink);
white-space:pre;line-height:1.45}
footer{color:var(--muted);font-size:11.5px;font-family:var(--mono);border-top:1px solid var(--line);padding-top:12px}
.pick{margin:0 2px 0 0;accent-color:var(--accent);cursor:pointer}
.allbox{font-family:var(--mono);font-size:10.5px;color:var(--muted);text-transform:none;
letter-spacing:.03em;display:inline-flex;align-items:center;gap:4px;cursor:pointer;margin-left:auto}
.allbox input{accent-color:var(--accent);cursor:pointer}
/* the selection lives above the fold, the bar has to follow the reader down */
#bulkbar{position:fixed;left:50%;transform:translateX(-50%);bottom:16px;z-index:9;
display:none;align-items:center;gap:8px;flex-wrap:wrap;justify-content:center;
background:var(--surface);border:1px solid var(--ink-2);border-radius:9px;
padding:9px 13px;box-shadow:0 6px 22px rgba(0,0,0,.22);max-width:calc(100% - 24px)}
#bulkbar.on{display:flex}
#bulkbar b{font-family:var(--mono);font-size:12px;font-variant-numeric:tabular-nums}
.dig{font-size:12px;color:var(--muted);font-family:var(--mono);line-height:1.5;
border-left:2px solid var(--line);padding-left:9px;margin:0;cursor:pointer}
.dig:hover{border-left-color:var(--accent);color:var(--ink-2)}
.dig b{color:var(--ink-2);font-weight:600}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.34);z-index:19;display:none}
#scrim.on{display:block}
/* правая шторка: читать переписку, не уходя со страницы */
#drawer{position:fixed;top:0;right:0;height:100%;width:min(640px,100%);z-index:20;
background:var(--ground);border-left:1px solid var(--ink-2);
box-shadow:-10px 0 30px rgba(0,0,0,.24);transform:translateX(102%);
transition:transform .18s ease-out;display:flex;flex-direction:column}
#drawer.on{transform:translateX(0)}
#drhead{padding:13px 16px;border-bottom:1px solid var(--line);display:flex;
flex-direction:column;gap:6px;background:var(--surface)}
#drtitle{font-size:14px;font-weight:640;line-height:1.35}
#drmeta{font-family:var(--mono);font-size:11px;color:var(--muted)}
#drbusy{font-family:var(--mono);font-size:11px;color:var(--live);display:none}
#drbusy.on{display:block}
#drbody{overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:10px}
#drsend{border-top:1px solid var(--line);padding:10px 12px;display:flex;gap:8px;
/* см. .composer ниже — рамка теперь на обёртке, кнопки внутри неё */
align-items:flex-end;background:var(--surface)}
/* Высоту считает taGrow(); потолок в vh, а не в пикселях, чтобы на
   большом экране поле росло вместе с ним. Дальше — прокрутка. */
#drtext{flex:1;min-height:38px;max-height:45vh;resize:none;overflow-y:auto;
padding:8px 10px;
border:1px solid var(--line);border-radius:7px;background:var(--ground);color:var(--ink);
font-family:var(--sans);font-size:13.5px;line-height:1.45}
#drtext:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
#drmic{display:none}
#newbox{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}
#newtext{flex:1;min-width:260px;min-height:38px;max-height:150px;resize:vertical;
padding:8px 10px;border:1px solid var(--line);border-radius:7px;background:var(--surface);
color:var(--ink);font-family:var(--sans);font-size:13.5px;line-height:1.45}
#newtext:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
#drmic.rec{color:var(--warn);border-color:var(--warn-line);background:var(--warn-bg);
animation:rcpulse 1.1s ease-in-out infinite}
#drmic.busy{opacity:.6}
/* Кнопки живут ВНУТРИ поля ввода, как в мессенджерах: рамка на обёртке,
   у самой textarea её нет. */
/* Пока открыт вопрос, на экране сессии меню, а не поле ввода, и отправленный
   текст сначала снимет вопрос. Человек должен знать это ДО отправки, а не по
   факту. */
#askwarn{display:none;font-size:11.5px;color:var(--queue);border:1px solid var(--queue);
background:var(--queue-bg);border-radius:8px;padding:5px 9px;margin:0 0 7px}
#askwarn.on{display:block}
/* 10-25: the dialog a session waits on (a background agent's permission
   prompt), read off its screen. It sits over the composer: a send to this
   session is held until the dialog is answered. */
#permbox:empty{display:none}
#permbox{margin:0 0 7px}
#permbox .ask{padding:7px 10px}
#permbox pre.permtext{max-height:190px;overflow:auto;margin:4px 0 7px;padding:6px 8px;
font-family:var(--mono);font-size:11px;line-height:1.35;white-space:pre;
background:var(--surface);border:1px solid var(--line);border-radius:6px}
#permbox .permbtns{display:flex;gap:6px;flex-wrap:wrap}
#permbox .permbtns button.o.pick-opt{display:inline-flex;align-items:center;gap:6px;width:auto}
#netwarn{display:none;font-size:11.5px;color:var(--warn);border:1px solid var(--warn-line);
background:var(--warn-bg);border-radius:8px;padding:5px 9px;margin:0 0 7px}
#netwarn.on{display:block}
#attachbar{display:none;flex-wrap:wrap;gap:6px;padding:0 0 7px}
#attachbar.on{display:flex}
.att{position:relative;display:inline-flex;align-items:center;gap:6px;max-width:230px;
background:var(--surface-2);border:1px solid var(--line);border-radius:9px;
padding:3px 4px 3px 4px;font-family:var(--mono);font-size:10.5px;color:var(--ink-2)}
.att img{width:26px;height:26px;object-fit:cover;border-radius:6px;display:block}
.att .ic{width:26px;height:26px;display:inline-flex;align-items:center;
justify-content:center;background:var(--surface);border-radius:6px;font-style:normal}
.att b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.att.up{opacity:.75}
.att .pct{font-style:normal;font-family:var(--mono);font-size:10px;color:var(--muted);
flex:0 0 auto}
.att .bar{position:absolute;left:0;right:0;bottom:0;height:2px;background:var(--line);
border-radius:0 0 8px 8px;overflow:hidden}
.att .bar u{display:block;height:100%;background:var(--accent);text-decoration:none;
transition:width .18s linear}
.txfoot{display:block}
.att button{padding:0 5px;line-height:1.5;font-size:13px;border-radius:6px}
#drsend{flex-direction:column;align-items:stretch}
.sendrow{display:flex;gap:8px;align-items:flex-end}
.composer{flex:1;display:flex;align-items:flex-end;gap:4px;min-width:0;
border:1px solid var(--line);border-radius:10px;background:var(--surface);
padding:4px 4px 4px 10px}
.composer:focus-within{border-color:var(--accent)}
.composer textarea{flex:1;min-width:0;border:0!important;background:none!important;
padding:7px 0!important;resize:none;outline:none}
.composer textarea:focus-visible{outline:none!important}
.iconbtn{flex:0 0 auto;width:30px;height:30px;padding:0;display:inline-flex;
align-items:center;justify-content:center;border-radius:8px;font-size:15px;line-height:1}
.iconbtn.go{background:var(--live);border-color:var(--live);color:#fff}
.iconbtn.go:hover{filter:brightness(1.08)}
.iconbtn:disabled{opacity:.45}
.msg{border:1px solid var(--line);border-radius:7px;padding:9px 11px;font-size:13.5px;
overflow-wrap:anywhere}
.msg pre{background:var(--surface-2);border:1px solid var(--line);border-radius:6px;
padding:8px 10px;margin:7px 0;overflow-x:auto;font-family:var(--mono);font-size:12px;
white-space:pre}
.msg code{background:var(--surface-2);border-radius:4px;padding:1px 4px;
font-family:var(--mono);font-size:12px}
.msg pre code{background:none;padding:0;font-size:inherit}
.msg ul{margin:6px 0;padding-left:19px}
.msg li{margin:2px 0}
.msg .mdh{font-weight:640;font-size:14px;margin:9px 0 4px}
.msg hr{border:0;border-top:1px solid var(--line);margin:9px 0}
.msg blockquote{margin:8px 0;padding:1px 0 1px 13px;border-left:2px solid var(--line);
color:var(--ink-2)}
.msg blockquote strong{color:var(--ink)}
.msg a{color:var(--accent)}
.msg table{border-collapse:collapse;margin:7px 0;font-size:12.5px;display:block;
overflow-x:auto;max-width:100%}
.msg th,.msg td{border:1px solid var(--line);padding:4px 8px;text-align:left;
vertical-align:top}
.msg th{background:var(--surface-2);font-weight:600;white-space:nowrap}
.ask{border:1px solid var(--queue);border-left:3px solid var(--queue);border-radius:7px;
background:var(--queue-bg);padding:9px 11px;font-size:13.5px;overflow-wrap:anywhere}
.ask .qh{font-family:var(--mono);font-size:10.5px;color:var(--queue);letter-spacing:.05em;
text-transform:uppercase;margin-bottom:6px}
.ask .q{font-weight:640;margin:8px 0 5px}
.ask .hd{font-family:var(--mono);font-size:10px;color:var(--queue);border:1px solid var(--line);
border-radius:4px;padding:1px 5px;margin-right:5px;text-transform:uppercase;font-weight:600}
.ask .o{font-size:12.5px;color:var(--ink-2);padding:3px 0 3px 11px;
border-left:2px solid var(--line);margin:4px 0}
button.o.pick-opt{display:block;width:100%;text-align:left;background:var(--surface);
border:1px solid var(--line);border-left:2px solid var(--queue);border-radius:7px;
padding:6px 9px;cursor:pointer;font-family:var(--sans);font-size:12.5px}
button.o.pick-opt:hover{border-color:var(--accent);border-left-color:var(--accent)}
button.o.pick-opt:disabled{opacity:.5;cursor:default}
/* Выбранное видно сразу. Раньше клик не менял на экране НИЧЕГО: ответ уезжал
   в сессию, а отметить его было нечем, и человек не понимал, сработало ли. */
button.o.pick-opt.chosen{border-color:var(--accent);
border-left-color:var(--accent);background:var(--surface-2);font-weight:600}
button.o.pick-opt.chosen .onum{background:var(--accent);color:#fff;
border-color:var(--accent)}
/* TUI спрашивает по одному. Клик по следующему вопросу, пока не ушёл
   предыдущий, отправил бы цифру в ТЕКУЩИЙ — поэтому он закрыт. */
.qgroup{margin:0 0 6px}
.qgroup.locked{opacity:.4}
.qgroup.locked button.o.pick-opt{pointer-events:none}
.qgroup.done{opacity:.62}
.qconfirm{margin:7px 0 2px;padding:4px 11px;font-size:12px}
.qdone{font-family:var(--mono);font-size:10px;color:var(--live);margin:3px 0 0}
.onum{font-family:var(--mono);font-size:9.5px;color:var(--queue);border:1px solid var(--line);
border-radius:4px;padding:0 4px;margin-right:6px}
.qnote{font-family:var(--mono);font-size:10px;color:var(--warn);margin:2px 0 6px}
.ask .o i{color:var(--muted);font-style:normal;display:block;font-size:11.5px;margin-top:1px}
.ask.pend{border-color:var(--warn-line);border-left-color:var(--warn-line);
background:var(--warn-bg)}
.ask.pend .qh,.ask.pend .hd{color:var(--warn)}
.msg.answer .p{margin:5px 0}
.msg.answer .mtext .p b{display:block;font-size:11px;color:var(--muted);font-weight:500;
margin-bottom:1px}
.wait{color:var(--warn)}
.ctxbar{display:flex;align-items:center;gap:8px;margin:5px 0 0;
font-family:var(--mono);font-size:10.5px;color:var(--muted)}
.ctxbar .tr{flex:0 0 110px;height:5px;border-radius:3px;background:var(--surface-2);
border:1px solid var(--line);overflow:hidden}
.ctxbar .tr i{display:block;height:100%;background:var(--live)}
.ctxbar b{font-weight:600;font-variant-numeric:tabular-nums}
.ctxbar.warm .tr i{background:var(--accent)}
.ctxbar.hot .tr i{background:var(--warn-line)}
.ctxbar.hot{color:var(--warn)}
.rowopt{font:inherit;font-size:11px;font-family:var(--mono);padding:3px 5px;
border-radius:6px;border:1px solid var(--line);background:var(--surface);
color:var(--ink-2);cursor:pointer}
.rowopt:focus-visible{outline:2px solid var(--accent);outline-offset:1px}

/* --- две панели: слева колонка сессий, справа постоянно открытый чат --- */
html,body{height:100%}
body{overflow:hidden}
/* Ширины колонок — переменные, а не жёсткие minmax: тянуть мышью надо
   именно их, иначе пришлось бы пересчитывать ширины трёх соседей разом. */
#shell{display:grid;grid-template-columns:var(--wside,300px) 6px 1fr;
height:100vh}
.gutter{cursor:col-resize;background:var(--line);opacity:.45;
transition:opacity .12s,background .12s}
.gutter:hover,body.resizing .gutter{opacity:1;background:var(--accent)}
/* Во время перетаскивания курсор и выделение перехватываем на весь
   документ: иначе мышь уходит с узкой полоски и тяга обрывается. */
body.resizing{cursor:col-resize;user-select:none}
#g2{display:none}
body.filed #g2{display:block}
/* Открытый файл — третья колонка: чат сужается, но не прячется, потому что
   файл почти всегда читают, продолжая разговор о нём. */
body.filed #shell{grid-template-columns:var(--wside,300px) 6px 1fr 6px
var(--wfile,420px)}
#filepane{display:none;flex-direction:column;min-width:0;height:100vh;
background:var(--surface);border-left:1px solid var(--line)}
body.filed #filepane{display:flex}
#fhead{flex:0 0 auto;padding:10px 14px;border-bottom:1px solid var(--line);
display:flex;align-items:center;gap:8px}
#fname{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;font-family:var(--mono);font-size:11.5px;color:var(--ink-2)}
#fbody{flex:1 1 auto;min-height:0;overflow:auto;padding:12px 14px}
#fbody pre{margin:0;font-family:var(--mono);font-size:11.5px;line-height:1.5;
white-space:pre-wrap;overflow-wrap:anywhere}
#fbody img{max-width:100%;border-radius:8px}
#fbody iframe{width:100%;height:100%;border:0;background:#fff}
.steplink{color:var(--accent);cursor:pointer;text-decoration:underline;
text-underline-offset:2px}
.stepnote{color:var(--muted)}
.filechips{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}
.fchip{display:inline-flex;align-items:center;gap:5px;max-width:260px;cursor:pointer;
background:var(--surface-2);border:1px solid var(--line);border-radius:9px;
padding:3px 8px;font-family:var(--mono);font-size:10.5px;color:var(--ink-2)}
.fchip:hover{border-color:var(--accent);color:var(--accent)}
.fchip b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fchip.art{border-color:var(--queue);background:var(--queue-bg)}
/* Фотографию показываем фотографией: клипса с именем файла — это обещание
   картинки, а не картинка. Ширина по содержимому, высота ограничена, чтобы
   скриншот на всю страницу не выдавливал переписку. */
.fshot{display:inline-flex;flex-direction:column;gap:4px;cursor:pointer;
  max-width:260px;padding:4px;border:1px solid var(--line);border-radius:10px}
.fshot:hover{border-color:var(--accent)}
.fshot img{display:block;max-width:100%;max-height:190px;width:auto;height:auto;
  border-radius:6px;object-fit:contain}
.fshot b{font-weight:500;font-size:11px;opacity:.7;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
#drfiles{flex:0 0 auto}
#flist{display:flex;flex-direction:column;gap:6px}
#flist .fchip{max-width:none}
@media(max-width:1100px){
  body.filed #shell{grid-template-columns:1fr}
  body.filed #side,body.filed #main{display:none}
  body.filed #filepane{height:100vh}
  .gutter{display:none}
}
#side{overflow-y:auto;overscroll-behavior:contain;background:var(--ground);
border-right:1px solid var(--line);display:flex;flex-direction:column;gap:9px;
padding:12px 11px 90px;max-width:none;margin:0}
#side .grp{gap:5px}
#side footer{font-size:10px;padding:0;border:0;margin:0}
/* ⛔ #side — флекс-колонка, и без этого дети сжимаются: сетка статусов
   схлопывалась в 2px, а плитки по 101px срезал overflow:hidden. */
#side>*{flex:0 0 auto}
.devbar{display:flex;gap:6px;align-items:center}
.devbar select{flex:1 1 auto;min-width:0;font:inherit;font-size:12px;font-weight:640;
padding:5px 7px;border-radius:8px;border:1px solid var(--line);
background:var(--surface);color:var(--ink);cursor:pointer}
.devbar button{flex:0 0 auto;padding:5px 9px;line-height:1}
.sortbar{display:flex;align-items:center;gap:5px;font-family:var(--mono);
font-size:10px;color:var(--muted);margin:-1px 0 4px}
.sortbar button{padding:2px 8px;font-size:10.5px;border-radius:6px;
font-family:var(--mono)}
.sortbar button.on{border-color:var(--accent);color:var(--accent);
background:var(--queue-bg)}
.limits{display:flex;flex-direction:column;gap:3px}\n.limits+.limits{margin-top:7px;padding-top:7px;\nborder-top:1px solid var(--line)}
.lim{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;
color:var(--muted)}
.lim .lname{flex:0 0 38px}
.accs .lim .lname{flex:0 0 92px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.accs .lim.cur .lname{font-weight:700;color:var(--ink)}
.lim .tr{flex:1 1 auto;height:5px;border-radius:3px;background:var(--surface-2);
border:1px solid var(--line);overflow:hidden}
.lim .tr i{display:block;height:100%;background:var(--live)}
.lim b{flex:0 0 62px;text-align:right;color:var(--ink);font-size:10.5px}
.lim .lres{flex:0 0 auto;font-style:normal;font-size:9.5px;opacity:.75;
min-width:64px;text-align:right}
.limage{font-family:var(--mono);font-size:9.5px;color:var(--muted);
text-align:right;padding:3px 0 0}
.lim.warm .tr i{background:var(--accent)}
.lim.hot .tr i{background:var(--warn-line)}
.lim.hot,.lim.hot b{color:var(--warn)}
.lim.clk{cursor:pointer}\n.lim.clk:hover .lname{color:var(--ink);text-decoration:underline}
.procs{margin-top:6px;font-family:var(--mono);font-size:10px;color:var(--muted);
display:flex;flex-direction:column;gap:2px;max-height:46vh;overflow:auto}
.procs .phead{display:flex;justify-content:space-between;gap:6px;padding:2px 0 3px;
border-bottom:1px solid var(--line);margin-bottom:2px;color:var(--ink)}
.prow{display:flex;align-items:center;gap:5px;line-height:1.4}
.prow .pm{flex:0 0 44px;text-align:right;color:var(--ink)}
.prow .pc{flex:0 0 36px;text-align:right}
.prow .pn{flex:1 1 auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prow .pn.sess{color:var(--ink)}
.prow button{flex:0 0 auto;padding:0 5px;font-size:10px;line-height:15px;border-radius:5px}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{font-family:var(--mono);font-size:10px;color:var(--muted);background:var(--surface);
border:1px solid var(--line);border-radius:6px;padding:3px 6px;white-space:nowrap}
.chip b{color:var(--ink);font-size:11px;font-weight:700}
.chip.busy b{color:var(--live)}
.chip.hot{border-color:var(--warn-line);background:var(--warn-bg);color:var(--warn)}
.chip.hot b{color:var(--warn)}
.topbar{display:flex;gap:6px;align-items:center;flex-wrap:nowrap}
.topbar .acct{font-family:var(--mono);font-size:10px;color:var(--muted);flex:1 1 0;
min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#accscrim{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:29;display:none}
#accscrim.on{display:block}
#accmodal{position:fixed;z-index:30;left:50%;top:50%;transform:translate(-50%,-50%);
width:min(560px,92vw);max-height:86vh;overflow-y:auto;background:var(--surface);
border:1px solid var(--line);border-radius:12px;padding:18px 20px;
display:none;flex-direction:column;gap:12px;box-shadow:0 18px 50px rgba(0,0,0,.3)}
#accmodal.on{display:flex}
#side h1{font-size:19px}
#side header{padding-bottom:11px}
#main{display:flex;flex-direction:column;min-width:0;height:100vh;background:var(--surface)}
#drhead{flex:0 0 auto}
#drbody{flex:1 1 auto;min-height:0}
#mainempty{margin:auto;text-align:center;color:var(--muted);font-size:14px;padding:40px}

/* --- строка-таб --- */
.ri{padding:0 5px;font-size:12px;line-height:1.5;border-radius:5px;
font-family:var(--mono)}
.ri.danger{color:var(--warn);border-color:var(--warn-line)}
.row.gone{opacity:.42}
.row.gone .dot{background:var(--line)!important;animation:none!important}
.row.dragging{opacity:.4}
#main{position:relative}
#dropzone{position:absolute;inset:8px;z-index:25;display:none;pointer-events:none;
align-items:center;justify-content:center;flex-direction:column;gap:6px;
border:2px dashed var(--accent);border-radius:14px;background:var(--queue-bg);
font-family:var(--mono);font-size:13px;color:var(--accent);text-align:center}
body.dropping #dropzone{display:flex}
#dropzone b{font-size:15px}
/* Полоса сверху вместо замершего экрана: действие идёт секунды, и без неё
   панель выглядит зависшей, а потом рывком перерисовывается. */
#toploader{position:fixed;top:0;left:0;right:0;height:2px;z-index:60;display:none;
background:var(--surface-2);overflow:hidden}
body.busy #toploader{display:block}
#toploader i{display:block;height:100%;width:34%;background:var(--accent);
animation:tlmove 1.05s ease-in-out infinite}
@keyframes tlmove{0%{transform:translateX(-100%)}100%{transform:translateX(330%)}}
.row.busyact{opacity:.5}
.row.busyact .dot{background:var(--accent);animation:rcpulse .7s ease-in-out infinite}
.msg.pending{opacity:.7;border-style:dashed}
.typing{display:inline-flex;gap:4px;align-items:center;vertical-align:middle;
margin-right:6px}
.typing i{width:5px;height:5px;border-radius:50%;background:var(--muted);
animation:tdot 1s ease-in-out infinite}
.typing i:nth-child(2){animation-delay:.15s}
.typing i:nth-child(3){animation-delay:.3s}
@keyframes tdot{0%,60%,100%{opacity:.25;transform:translateY(0)}
30%{opacity:1;transform:translateY(-3px)}}
@media (prefers-reduced-motion:reduce){
  #toploader i,.typing i,.row.busyact .dot{animation:none}}
.fold.dropok{outline:2px dashed var(--accent);outline-offset:-2px}
/* Не переносим: с flex-wrap кнопки × и ↧ съезжали на отдельную строку,
   как только рядом появлялся бейдж проекта (видно при сортировке по
   recent), и карточка вырастала вдвое. Вместо переноса ужимаем бейдж —
   он и так с многоточием. */
.tabtop{display:flex;align-items:center;gap:6px;flex-wrap:nowrap;min-width:0}
/* Имя сессии — в одну строку и с многоточием: перенос съедал бы место у
   описания, ради которого строка и существует. Заданное руками помечаем
   точкой у края, иначе не отличить его от угаданного. */
.tabedit{width:100%;font:600 13px/1.3 inherit;color:var(--ink);padding:1px 4px;
border:1px solid var(--accent);border-radius:5px;background:var(--bg)}
.tflag{font-family:var(--mono);font-size:9.5px;padding:1px 4px;border-radius:4px;
border:1px solid var(--line)}
.proj{font-family:var(--mono);font-size:9.5px;padding:1px 5px;border-radius:4px;
background:var(--surface-2);border:1px solid var(--line);color:var(--muted);
flex:0 1 auto;min-width:24px;max-width:120px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.fold .proj{display:none}
.tflag.wait{color:var(--warn);border-color:var(--warn-line)}
/* Статус самого Claude Code — снят с экрана TUI. Он говорит и тогда, когда
   транскрипт молчит: компакт, длинный ход, очередь. */
.tflag.tui{color:var(--live);border-color:var(--live)}
.tflag.tui.queued{color:var(--queue);border-color:var(--queue)}
.tflag.tui.ready{color:var(--muted);border-color:var(--line)}
#drtui{font-family:var(--mono);font-size:11px;padding:1px 6px;border-radius:5px;
border:1px solid var(--live);color:var(--live);white-space:nowrap;flex:0 0 auto}
#drtui.queued{color:var(--queue);border-color:var(--queue)}
#drtui.ready{color:var(--muted);border-color:var(--line)}
#drtui:empty{display:none}
.tctx{font-family:var(--mono);font-size:9.5px;margin-left:auto;color:var(--muted)}
.tctx.warm{color:var(--accent)}
.tctx.hot{color:var(--warn);font-weight:700}
.tflags{display:contents}
@keyframes rcpulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.72)}}
.chip.busy.on b::after{content:"●";color:var(--live);margin-left:4px;
display:inline-block;animation:rcpulse 1.15s ease-in-out infinite}
.row time{font-size:10.5px}
@media (prefers-reduced-motion:reduce){
  .row.busyrow .dot,.chip.busy.on b::after{animation:none}}

@media(max-width:860px){
  html,body{height:auto}
  body{overflow:auto}
  #shell{grid-template-columns:1fr;height:auto}
  #side{height:auto;overflow:visible;padding-bottom:20px}
  #main{height:auto;min-height:74vh;border-top:2px solid var(--ink)}
  .gutter{display:none}
}
/* Реплики человека справа, ответы слева — как в самом Claude. Ширину режем,
   иначе на широком экране «пузырь» растягивается во всю панель и разница
   сторон перестаёт читаться. */
.msg{max-width:80%;border:0;padding:0;background:none}
/* Пузырь только у человека. У ответов Claude рамки нет — она режет длинный
   текст на карточки и мешает его читать; в самом Claude так же. */
.msg.user{margin-left:auto}
.msg.user .mtext{background:var(--surface-2);border-radius:14px;
padding:11px 15px}
.msg.assistant{margin-right:auto;max-width:92%}
.msg.assistant .mtext{padding:2px 0}
.msg.answer{margin-left:auto}
.msg.answer .mtext{background:var(--surface-2);border-radius:14px;padding:11px 15px}
.ask{max-width:92%;margin-right:auto}
@media(max-width:900px){.msg{max-width:96%}}

/* Время и «копировать» — под сообщением и только по наведению: постоянная
   строка со временем у каждой реплики забивает ленту сильнее самих реплик. */
.mfoot{display:flex;align-items:center;gap:6px;margin-top:3px;
font-family:var(--mono);font-size:10px;color:var(--muted);
opacity:0;transition:opacity .12s}
.msg:hover .mfoot,.mfoot:focus-within{opacity:1}
.msg.user .mfoot{justify-content:flex-end}
.mfoot button{padding:1px 6px;font-size:11px;line-height:1.4;border-radius:6px;
font-family:var(--mono)}
.mfoot button.done{color:var(--live);border-color:var(--live)}
@media (hover:none){.mfoot{opacity:1}}
.step{font-family:var(--mono);font-size:11.5px;color:var(--muted);line-height:1.45;
padding:2px 0 2px 10px;border-left:2px solid var(--line);white-space:pre-wrap;
overflow-wrap:anywhere;margin:0}
.step b{color:var(--accent);font-weight:600}
details.steps{border-left:2px solid var(--line);padding:0 0 0 10px;margin:0}
details.steps>summary{font-family:var(--mono);font-size:11.5px;color:var(--muted);
cursor:pointer;list-style:none;padding:3px 0;overflow-wrap:anywhere}
details.steps>summary::-webkit-details-marker{display:none}
details.steps>summary::before{content:'▸ ';color:var(--accent)}
details.steps[open]>summary::before{content:'▾ '}
details.steps[open]>summary{color:var(--ink-2)}
details.steps>.step{border-left:0;padding-left:0}
.think{font-size:12.5px;color:var(--muted);line-height:1.5;padding:6px 10px;
border-left:2px dashed var(--line);white-space:pre-wrap;overflow-wrap:anywhere;margin:0}
.hidden{display:none}
#drtop{display:flex;align-items:center;gap:8px;min-width:0}
/* Заголовок НЕ растягиваем: с flex:1 он занимал всю строку, и «files N»
   с «…» улетали к правому краю через полэкрана пустоты. Теперь они
   стоят сразу за названием, а ужимается оно только когда не влезает. */
#drtop #drtitle{flex:0 1 auto;min-width:0;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
#dracts{display:flex;gap:6px;flex:0 0 auto}
#drmenu{flex:0 0 auto;max-width:180px}
#drdot{flex:0 0 auto}
#drdot{width:8px;height:8px;border-radius:50%;background:var(--line);display:inline-block}
#drdot.live{background:var(--live)}
#drdot.busy{animation:rcpulse 1.15s ease-in-out infinite}
#drextra{display:none;gap:12px;flex-wrap:wrap;align-items:center;
font-family:var(--mono);font-size:11px;color:var(--muted);padding-top:2px}
#drextra.on{display:flex}
#drmid{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
#drmid #drmeta{flex:0 1 auto;min-width:0}
/* Разделитель вместо пустоты: слева — что за сессия, справа — чем
   считает. Раньше эти две группы разносило по краям экрана. */
.hsep{flex:0 0 auto;width:1px;height:15px;background:var(--line)}
/* Модель и эффорт наружу: это не «настройка раз в день», а состояние сессии —
   по ним видно, на чём она считает прямо сейчас. */
#drextraopts{display:flex;gap:6px;flex-wrap:wrap;flex:0 0 auto}
#tgwrap{display:inline-flex;align-items:center;gap:4px;cursor:pointer;flex:0 0 auto;
font-family:var(--mono);font-size:11px;color:var(--muted)}
#tgwrap input{accent-color:var(--accent);cursor:pointer}
#drextra a,#drextra span{font-family:var(--mono);font-size:11px}
.ctxmini{display:inline-block;width:52px;height:4px;border-radius:2px;
background:var(--surface-2);border:1px solid var(--line);overflow:hidden;
vertical-align:middle;margin-right:4px}
.ctxmini i{display:block;height:100%;background:var(--live)}
.ctxmini.warm i{background:var(--accent)}
.ctxmini.hot i{background:var(--warn-line)}
.warnnote{color:var(--warn)}
/* Неподтверждённая реплика: пузырь остаётся на экране, пока человек сам не
   решит, что с ним делать. */
.msg.user.pending.unsure .mtext{border:1px solid var(--warn-line);
background:var(--warn-bg)}
@media (prefers-reduced-motion:reduce){#drdot.busy{animation:none}}
.mh{font-family:var(--mono);font-size:10.5px;color:var(--muted);margin-bottom:5px;
text-transform:uppercase;letter-spacing:.06em}
/* Родной якорь браузера включён: картинки выше экрана догружаются позже
   и без него толкали текст вниз (27-09, 500 px после «Show earlier»).
   txPatch меряет положение ПОСЛЕ правки, так что двойной поправки нет. */
#drbody .txmore{display:flex;gap:10px;align-items:center;justify-content:center;
flex-wrap:wrap;font-size:12px;color:var(--muted)}
.rowopt.saving{opacity:.6}
.iconbtn.stopbtn{color:var(--warn);border-color:var(--warn-line);font-size:13px}
.iconbtn.stopbtn.saving{opacity:.55}
#drbody .stopnote{align-self:center;font-family:var(--mono);font-size:11px;color:var(--muted);
padding:2px 10px;border:1px dashed var(--line);border-radius:999px}

/* ==== левая колонка: список сессий (09-24) ====
   Три вопроса, на которые она обязана отвечать с одного взгляда: где я
   (синий — только у открытой сессии), что работает (зелёная полоса, заливка
   и слово), кто ждёт меня (оранжевая полоса). Всё остальное — тише. */
:root{--sel:#2458c9;--sel-bg:#e6edfb;--work-bg:#e1f3ec}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){
--sel:#86adff;--sel-bg:#19243a;--work-bg:#10281f}}
/* ⛔ Строки теперь в одну строку с многоточием, и их min-content — длина
   самого длинного пересказа. В узкой раскладке колонка — трек 1fr сетки, а
   он не бывает уже min-content своего ребёнка: колонка вылезала за экран
   телефона. min-width:0 разрешает ей сжиматься, многоточие делает остальное. */
#side,#sidelist,#side .grp,.fold,.rows{min-width:0}
#side .grp{gap:3px}
#side .grp h2{font-size:10.5px;letter-spacing:.12em;color:var(--muted);border-bottom:0;
padding:12px 4px 3px;align-items:center;font-weight:600}
#side .grp h2 .gcount{color:var(--ink-2);letter-spacing:0;font-weight:700}
.rows{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:1px}
.row{--rowbg:var(--ground);position:relative;display:grid;
grid-template-columns:14px minmax(0,1fr);column-gap:7px;align-items:start;
padding:4px 8px 5px 7px;border-radius:7px;border:1px solid transparent;
background:var(--rowbg);cursor:pointer;transition:background-color .12s,border-color .12s}
.row:hover{--rowbg:var(--surface-2)}
.lead{height:18px;display:flex;align-items:center;justify-content:center}
.row .dot{width:9px;height:9px;border-radius:50%;box-sizing:border-box;flex:0 0 auto;
border:1.5px solid var(--idle);background:transparent;display:block}
.row .pick{display:none;margin:0;accent-color:var(--sel)}
.rmain{min-width:0;display:flex;flex-direction:column;gap:0}
.rline{display:flex;align-items:center;gap:6px;min-width:0}
.tabtitle{flex:1 1 auto;min-width:0;font-size:13px;font-weight:600;line-height:18px;
color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .tctx{flex:0 0 auto;margin:0;font-size:10px}
.rstat{display:flex;align-items:center;gap:5px;min-width:0;height:16px;line-height:16px;
font-family:var(--mono);font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden}
.rstat .sep{opacity:.55}
.rstat .proj{font-size:9.5px;max-width:110px}
.st{font-weight:700;flex:0 0 auto}
.st-work{color:var(--live)}
.st-wait{color:var(--warn)}
.st-queue{color:var(--queue)}
.st-idle,.st-ret,.st-stop{color:var(--muted);font-weight:500}
.rstat .proj{flex:0 1 auto}
/* пересказ дописан в строку статуса: две строки на таб вместо трёх (09-24,
   «табы стали слишком высокими») */
.tabname{flex:1 1 auto;min-width:0;font-family:var(--sans);font-size:12px;line-height:16px;
color:var(--ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* состояния */
.row.live .dot{border-color:var(--live)}
.row.queue .dot{background:var(--queue);border-color:var(--queue)}
.row.ret{opacity:.55}
.row.ret:hover{opacity:.9}
.row.ret .dot,.row.idle .dot{border-color:var(--line)}
.row.busyrow{--rowbg:var(--work-bg);box-shadow:inset 3px 0 0 var(--live)}
.row.busyrow .dot{background:var(--live);border-color:var(--live);
animation:rcpulse 1.15s ease-in-out infinite}
.row.busyrow .rstat time,.row.busyrow .rstat .sep.t{display:none}
.row.waitrow{box-shadow:inset 3px 0 0 var(--warn-line)}
.row.waitrow .dot{background:var(--warn);border-color:var(--warn);animation:none}
/* открытая — поверх любого состояния: рамка, заливка и синее имя */
.row.sel,.row.sel:hover{--rowbg:var(--sel-bg);border-color:var(--sel);
box-shadow:inset 3px 0 0 var(--sel)}
.row.sel .tabtitle{color:var(--sel)}
.row.sel.ret{opacity:1}
/* Кнопки всплывают поверх правого края, а не встают в строку: появление в
   потоке меняло ширину строки, и она дёргалась под курсором (была причина
   держать их видимыми всегда — и шумными). */
.rowacts{position:absolute;top:3px;right:6px;display:flex;gap:3px;visibility:hidden;
padding-left:12px;background:linear-gradient(to right,transparent,var(--rowbg) 12px)}
/* только при наведении: на открытой строке кнопки навсегда закрывали её
   проект и время */
.row:hover .rowacts,.rowacts:focus-within{visibility:visible}
.ri{padding:0 6px;font-size:12px;line-height:17px;border-radius:5px;background:var(--surface)}
/* чекбокс встаёт на место точки: при наведении и пока что-то выбрано */
.row:hover .pick,#side.picking .row .pick{display:block}
.row:hover .lead .dot,#side.picking .row .lead .dot{display:none}
.approw .lead .dot{display:block!important}
@media(hover:none){.rowacts{visibility:visible}.row .pick{display:block}.row .lead .dot{display:none}}
/* папки проектов — заголовок строкой, без рамок вокруг */
.fold{margin:2px 0 4px}
.fold>summary{cursor:pointer;display:flex;align-items:center;gap:7px;list-style:none;
padding:6px 6px 4px;font-size:12px;font-weight:650;color:var(--ink-2);
border-bottom:1px solid var(--line);margin-bottom:3px}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary::before{content:"▸";color:var(--muted);font-size:10px;width:9px}
.fold[open]>summary::before{content:"▾"}
.fold .fname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fcount{font-family:var(--mono);font-size:10.5px;color:var(--muted);font-weight:400}
.fbusy{margin-left:auto;display:inline-flex;align-items:center;gap:5px;flex:0 0 auto;
font-family:var(--mono);font-size:10px;font-weight:700;color:var(--live)}
.fbusy::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--live);
animation:rcpulse 1.15s ease-in-out infinite}
.fold .rows{padding:0;background:transparent}
.fold .proj{display:none}
/* «сейчас работают» */
.nowbar{display:flex;flex-wrap:wrap;gap:5px;align-items:center;padding:8px 9px 9px;
border-radius:10px;background:var(--surface);border:1px solid var(--line)}
.nowbar .nh{width:100%;font-family:var(--mono);font-size:10px;letter-spacing:.1em;
text-transform:uppercase;color:var(--muted);margin-bottom:1px}
.nowbar .nh b{color:var(--live);font-size:11px}
.nowbar .nh b.w{color:var(--warn)}
.nowbar.none{font-family:var(--mono);font-size:10.5px;color:var(--muted);padding:7px 10px}
.np{display:inline-flex;align-items:center;gap:6px;max-width:100%;min-width:0;
padding:3px 10px 3px 8px;border-radius:999px;font-family:var(--sans);font-size:12px;
font-weight:600;letter-spacing:0;border:1px solid var(--live);background:var(--work-bg);
color:var(--ink)}
.np i{width:7px;height:7px;border-radius:50%;background:var(--live);flex:0 0 auto;
animation:rcpulse 1.15s ease-in-out infinite}
.np span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.np.wait{border-color:var(--warn-line);background:var(--warn-bg)}
.np.wait i{background:var(--warn);animation:none}
.np.sel{border-color:var(--sel);box-shadow:0 0 0 1px var(--sel);color:var(--sel)}
.np:hover{border-color:var(--ink-2)}
@media (prefers-reduced-motion:reduce){.fbusy::before,.np i{animation:none}}
/* Таб, третий заход (09-24): «idle — бесполезное слово, время слишком
   длинное, не видно проекта и описания». Строка 1 — имя, справа проект и
   короткое время («2h», полное — в подсказке); строка 2 — пересказ. Слово
   статуса остаётся только там, где оно что-то говорит: working, compacting,
   waiting for you. Контекст — только от 60%. */
.rmeta{flex:0 0 auto;min-width:0;display:flex;align-items:center;gap:4px;
font-family:var(--mono);font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden}
.rmeta time{flex:0 0 auto;font-size:10.5px}
.rmeta .proj{flex:0 0 auto;min-width:0;max-width:100px;padding:0;border:0;border-radius:0;
background:none;font-size:10.5px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.rmeta .sep{opacity:.55;flex:0 0 auto}
.fold .rmeta .proj,.fold .rmeta .proj+.sep{display:none}
.rmeta .tctx{flex:0 0 auto;font-size:10.5px;font-weight:700;margin-right:2px}
.rsum{display:flex;align-items:center;gap:5px;min-width:0;height:16px;line-height:16px;
white-space:nowrap;overflow:hidden}
.rsum .st{font-family:var(--mono);font-size:10.5px}
.rsum .sep{font-family:var(--mono);font-size:10.5px;color:var(--muted);opacity:.55;flex:0 0 auto}
.rsum .st-idle,.rsum .st-queue,.rsum .st-ret,.rsum .st-stop,.rsum .st:empty,
.rsum .st-idle+.sep,.rsum .st-queue+.sep,.rsum .st-ret+.sep,.rsum .st-stop+.sep,
.rsum .st:empty+.sep{display:none}
.st-act{color:var(--ink-2)}
/* --- HQ embed: one agent's chat inside /play (?embed=1) -------------------
   The game page owns the session list, the HUD and the agent header; the
   iframe keeps the proven chat engine (send retries, uploads, voice, asks). */
html.embed #side,html.embed .gutter{display:none}
html.embed #shell{grid-template-columns:1fr}
html.embed body.filed #shell{grid-template-columns:1fr 6px var(--wfile,420px)}
html.embed body.filed #g2{display:block}
html.embed #flash{position:fixed;top:auto;bottom:86px;left:50%;transform:translateX(-50%);z-index:60;
max-width:min(560px,92vw);box-shadow:0 8px 30px rgba(0,0,0,.35)}
html.embed #drtitle,html.embed #drdot{display:none}
@media(max-width:1100px){html.embed body.filed #shell{grid-template-columns:1fr}
html.embed body.filed #main{display:none}}
@media(max-width:860px){
html.embed,html.embed body{height:100%;overflow:hidden}
html.embed #shell{height:100vh;height:100dvh}
html.embed #main{height:100vh;height:100dvh;min-height:0;border-top:0}}
html.game:root{--ground:#100d26;--surface:#171338;--surface-2:#241d55;--ink:#f3f0ff;
--ink-2:#cdc6f0;--muted:#8f88bf;--line:#30296b;--live:#9dff57;--live-bg:#1b3417;
--queue:#ffd23f;--queue-bg:#3a2f10;--idle:#a49ccc;--warn:#ff8193;--warn-bg:#3a1320;
--warn-line:#ff4d6d;--accent:#ff8a3d}
html.game body{background:radial-gradient(900px 500px at 85% -10%,#2b1e63 0,transparent 60%),
radial-gradient(700px 400px at -10% 110%,#3a1747 0,transparent 55%),var(--ground)}
html.game #main{background:transparent}
html.game #drhead{background:rgba(20,16,50,.82);border-bottom:2px solid var(--line)}
html.game #drsend{background:rgba(20,16,50,.92);border-top:2px solid var(--line)}
html.game .msg.user .mtext,html.game .msg.answer .mtext{background:linear-gradient(135deg,#3b2574,#2a1f60);
border:2px solid #5b40b4;border-radius:6px;box-shadow:3px 3px 0 #07061a}
html.game .msg.assistant .mtext{border-left:3px solid rgba(255,138,61,.55);padding-left:12px}
html.game #drtext{background:#0d0b22;border:2px solid #3f3592;border-radius:6px}
html.game #drtext:focus-visible{outline:2px solid var(--accent);outline-offset:0}
html.game button{border-radius:4px}
html.game button.go{background:var(--accent);color:#1d1000;border-color:#ffb27a;
box-shadow:2px 2px 0 #07061a;font-weight:700}
html.game .ask{border:2px solid var(--queue);background:#262010;box-shadow:4px 4px 0 #07061a}
@media(max-width:860px){html.embed #drextraopts,html.embed #tgwrap{display:none}}
/* 11-17 iPhone: the effort select ran off the right edge; the composer at
   13.5px made Safari zoom the page on focus (anything under 16px does). */
@media(max-width:860px){html.embed #drhead select[data-opt=model]{max-width:116px}
html.embed #drhead select[data-opt=effort]{max-width:84px}}
@media(hover:none) and (pointer:coarse){#drtext,.composer textarea{font-size:16px}}
html{touch-action:manipulation}
/* 10-16 «панель перегружена»: in HQ the game's agent bar already says who,
   what and how far. The chat's own header said it again (model twice,
   context twice, status twice) plus pid and counters. It is one click away
   now: ⋯ in the agent bar opens it as a card over the chat. */
/* 10-16 «эту панель сделать всегда видимой, но минималистично»: one slim row
   under the agent bar — files, actions, model, effort, steps. The counters
   (id, age, messages, steps, pid) open with ⋯. */
html.embed #drhead{display:flex;flex-direction:row;align-items:center;flex-wrap:nowrap;gap:8px;
padding:5px 12px;min-height:38px;overflow-x:auto;scrollbar-width:none;background:rgba(20,16,50,.72)}
html.embed #drhead::-webkit-scrollbar{display:none}
html.embed #drtop,html.embed #drmid{display:contents}
html.embed #drmeta,html.embed #drtui,html.embed #drmid .hsep{display:none}
html.embed body.hqmore #drmeta{display:block;order:9;white-space:nowrap;font-size:10.5px}
html.embed #drfiles{order:1}
html.embed #drmenu{order:2;max-width:50px}
html.embed #drextraopts{order:3;display:flex;flex-wrap:nowrap}
html.embed #tgwrap{order:4;display:flex;white-space:nowrap}
html.embed #dracts{order:5;display:flex;gap:6px}
html.embed #drextra{order:6}
html.embed #drhead select,html.embed #drhead button{font-size:11px;padding:3px 8px;height:26px;border-radius:4px}
html.embed #drfiles{display:inline-flex;align-items:center;gap:5px}
html.embed #drfiles .ci{width:14px;height:14px;display:inline-block;vertical-align:-3px}
html.embed #drfiles{white-space:nowrap}
html.game #flash{background:#1d1846;border:2px solid var(--accent);color:var(--ink)}
html.game ::selection{background:rgba(255,138,61,.35)}
.ci{width:18px;height:18px;display:block;shape-rendering:crispEdges}
.att button .ci{width:12px;height:12px}
.att .ic .ci{width:16px;height:16px}
/* attachments: a picture is a tile with the picture, a file is an icon and a short name */
.att.img{padding:0;width:66px;height:54px;overflow:hidden}
.att.img img{width:100%;height:100%;border-radius:0;object-fit:cover}
.att.img .pct{position:absolute;left:4px;bottom:5px;color:#fff;text-shadow:0 0 3px #000}
.att.img button{position:absolute;top:3px;right:3px;width:20px;height:20px;padding:0;display:flex;
align-items:center;justify-content:center;background:rgba(10,8,32,.82);color:#fff;
border:1px solid rgba(255,255,255,.25);border-radius:5px;opacity:0;transition:opacity .15s}
.att.img:hover button{opacity:1}
@media(hover:none){.att.img button{opacity:1}}
.att.file b{max-width:120px}
html.game .att{border-radius:6px;border:2px solid var(--line);background:#0d0b22}
html.game .att.img{border-color:#3f3592}
html.game .att.up{opacity:1}
html.game .att .bar u{background:linear-gradient(90deg,#4de1ff,#9dff57)}
html.embed .composer{position:relative}
html.embed #hqrec{position:absolute;top:0;bottom:0;left:0;right:148px;z-index:3;display:flex;align-items:center;gap:10px;
padding:0 12px;pointer-events:none;border-radius:6px;
background:repeating-linear-gradient(0deg,rgba(77,225,255,.05) 0 1px,transparent 1px 3px),#0d0b22;animation:hqrecin .2s ease-out}
html.embed #hqrec canvas{flex:1;min-width:0;height:36px}
html.embed #hqrec .rd{width:10px;height:10px;border-radius:50%;background:#ff4d6d;box-shadow:0 0 10px #ff4d6d;
animation:rcpulse 1s ease-in-out infinite;flex:0 0 auto}
html.embed #hqrec b{font-family:var(--mono);font-size:11px;color:#ff8193;letter-spacing:.12em;flex:0 0 auto}
html.embed #hqrec em{font-style:normal;font-family:var(--mono);font-size:11px;color:var(--ink-2);flex:0 0 auto}
html.embed #hqrec.done canvas{opacity:.55;animation:hqscan 1.1s linear infinite}
html.embed #hqrec.done .rd{background:var(--accent);box-shadow:0 0 10px var(--accent)}
html.embed #hqrec.done b{color:var(--accent)}
html.embed #hqrec.gone{opacity:0;transition:opacity .3s}
@keyframes hqrecin{from{opacity:0;transform:scaleY(.2)}}
@keyframes hqscan{50%{opacity:.2}}
html.embed #drmic.rec{color:#fff;background:#ff4d6d;border-color:#ff4d6d}
#hqst{display:none}
html.embed #drsend{position:relative}
html.embed #hqst.on{display:flex;align-items:center;gap:7px;position:absolute;left:14px;bottom:calc(100% - 13px);z-index:5;
max-width:min(520px,calc(100% - 28px));padding:4px 10px 4px 8px;border-radius:4px;cursor:pointer;
font-family:var(--mono);font-size:11.5px;line-height:1.35;color:var(--ink-2);background:rgba(13,11,34,.94);
border:1px solid var(--line);box-shadow:2px 2px 0 #07061a;animation:hqstin .18s ease-out}
html.embed #hqst .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
html.embed #hqst .ci{width:13px;height:13px;flex:0 0 auto}
html.embed #hqst.ok{color:#d6ffc2;border-color:#3f7a22}
html.embed #hqst.ok .ci{color:var(--live)}
html.embed #hqst.bad{color:#ffd6dc;border-color:#7a2a3a}
html.embed #hqst.bad .t{white-space:normal}
html.embed #hqst.bad .ci{color:var(--warn-line)}
html.embed #hqst.busy{border-color:#2a5a7a}
.hqspin{display:inline-flex;gap:2px;flex:0 0 auto}
.hqspin i{width:3px;height:3px;background:#4de1ff;animation:hqdot 1s steps(1) infinite}
.hqspin i:nth-child(2){animation-delay:.33s}
.hqspin i:nth-child(3){animation-delay:.66s}
@keyframes hqdot{0%{opacity:1}50%{opacity:.2}}
@keyframes hqstin{from{opacity:0;transform:translateY(4px)}}
/* the bubble tells how its send went: sending, then delivered */
.dlv{display:inline-flex;align-items:center;gap:4px;color:var(--live)}
.dlv .ci{width:12px;height:12px}
.sndg{color:var(--muted)}
html.embed .msg.user.pending .mfoot{font-family:var(--mono);font-size:11px}
/* 12.11 search across every session: a box over the column, results replace it */
#sbox{position:relative;margin:2px 0 0}
#sq{width:100%;font:inherit;font-size:13.5px;padding:7px 112px 7px 10px;border-radius:7px;
border:1px solid var(--line);background:var(--surface);color:var(--ink)}
#sq:focus-visible{outline:2px solid var(--sel);outline-offset:1px}
#sqn{position:absolute;right:34px;top:50%;transform:translateY(-50%);font-family:var(--mono);
font-size:10.5px;color:var(--muted);pointer-events:none}
#sres{min-width:0}
.snone{font-size:12.5px;color:var(--muted);padding:10px 6px}
.srows{list-style:none;margin:6px 0 0;padding:0;display:flex;flex-direction:column;gap:2px}
.srow{min-width:0;padding:6px 8px 7px;border-radius:7px;border:1px solid transparent;cursor:pointer;
background:var(--ground)}
.srow:hover{background:var(--surface-2)}
.srow.sel{background:var(--sel-bg);border-color:var(--sel)}
.srow.ret{opacity:.75}
.sline{display:flex;align-items:center;gap:7px;min-width:0}
.sdot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;border:1.5px solid var(--line)}
.srow.live .sdot{border-color:var(--live)}
.srow.busyrow .sdot{background:var(--live);border-color:var(--live)}
.srow.queue .sdot{background:var(--queue);border-color:var(--queue)}
.stitle{flex:1 1 auto;min-width:0;font-size:13px;font-weight:600;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.srow.sel .stitle{color:var(--sel)}
.smeta{flex:0 1 auto;min-width:0;max-width:55%;font-family:var(--mono);font-size:10.5px;
color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ssub{font-size:12px;color:var(--ink-2);margin:1px 0 0 15px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.shit{font-size:12px;line-height:1.4;color:var(--ink-2);margin:3px 0 0 15px;
display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;
overflow-wrap:anywhere}
.srole{font-family:var(--mono);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.smore{font-family:var(--mono);font-size:10px;color:var(--muted);margin:2px 0 0 15px}
#sres mark,#sbox mark{background:var(--queue-bg);color:inherit;border-radius:2px;padding:0 1px;
box-shadow:0 0 0 1px var(--queue-bg)}
html.embed #sbox,html.embed #sres{display:none}

"""

JS = """
// Текущее устройство живёт в адресе: перезагрузка после действия должна
// вернуть на то же устройство, а не сбросить на своё.
const DEV=(new URLSearchParams(location.search)).get('dev')||'';
function dq(sep){ return (DEV && DEV!=='local') ? sep+'dev='+encodeURIComponent(DEV) : ''; }
// HQ (/play) shows this page in an iframe with ?embed=1 and listens for these
// events: which chat is open, a send went out or failed, a flash message.
const IN_HQ=(function(){ try{ return window.parent!==window
  && document.documentElement.classList.contains('embed'); }catch(e){ return false; } })();
function gameEmit(kind, data){
  if(!IN_HQ) return;
  try{ window.parent.postMessage(Object.assign({rc:kind}, data||{}), location.origin); }catch(e){}
}
function rcHas(id){ return typeof ROWS!=='undefined' && !!ROWS[id]; }
// Pixel icons for the chat inside HQ (pixelarticons, MIT; mic drawn for HQ).
const CI={"check":"M18 6h2v2h-2V6zm-2 4V8h2v2h-2zm-2 2v-2h2v2h-2zm-2 2h2v-2h-2v2zm-2 2h2v-2h-2v2zm-2 0v2h2v-2H8zm-2-2h2v2H6v-2zm0 0H4v-2h2v2z","paperclip":"M5 5h16v10H7V9h10v2H9v2h10V7H5v10h14v2H3V5h2z","mic":"M8 0h8v2h-8zM6 2h2v2h-2zM16 2h2v2h-2zM6 4h2v2h-2zM16 4h2v2h-2zM6 6h2v2h-2zM16 6h2v2h-2zM6 8h2v2h-2zM16 8h2v2h-2zM2 10h2v2h-2zM6 10h2v2h-2zM16 10h2v2h-2zM20 10h2v2h-2zM2 12h2v2h-2zM8 12h8v2h-8zM20 12h2v2h-2zM4 14h2v2h-2zM18 14h2v2h-2zM6 16h12v2h-12zM10 18h4v2h-4zM10 20h4v2h-4zM6 22h12v2h-12z","debug-stop":"M6 2h2v2H6V2Zm10 2h-2v2h-4V4H8v2H6v3H4V7H2v2h2v2h2v2H2v2h4v2H4v2H2v2h2v-2h2v3h8v-2H8V8h8v6h2v-3h2V9h2V7h-2v2h-2V6h-2V4Zm0 0V2h2v2h-2Zm-6 7h4v2h-4v-2Zm4 4h-4v2h4v-2Zm8 1h-6v6h6v-6Z","arrow-up":"M11 20h2V8h2V6h-2V4h-2v2H9v2h2v12zM7 10V8h2v2H7zm0 0v2H5v-2h2zm10 0V8h-2v2h2zm0 0v2h2v-2h-2z","close":"M5 5h2v2H5V5zm4 4H7V7h2v2zm2 2H9V9h2v2zm2 0h-2v2H9v2H7v2H5v2h2v-2h2v-2h2v-2h2v2h2v2h2v2h2v-2h-2v-2h-2v-2h-2v-2zm2-2v2h-2V9h2zm2-2v2h-2V7h2zm0 0V5h2v2h-2z","file-alt":"M21 22H3V2h12v2h2v2h2v2h2v14zM17 6h-2v2h2V6zM5 4v16h14V10h-6V4H5zm8 12H7v2h6v-2zm-6-4h10v2H7v-2zm4-4H7v2h4V8z","archive":"M22 4H2v6h2v10h16V10h2V4zM6 10h12v8H6v-8zm14-4v2H4V6h16zm-5 6H9v2h6v-2z","image":"M4 3H2v18h20V3H4zm16 2v14H4V5h16zm-6 4h-2v2h-2v2H8v2H6v2h2v-2h2v-2h2v-2h2v2h2v2h2v-2h-2v-2h-2V9zM8 7H6v2h2V7z","video":"M2 5h14v4h2V7h2V5h2v14h-2v-2h-2v-2h-2v4H2V5zm2 12h10V7H4v10z","music":"M8 4h12v16h-8v-8h6V8h-8v12H2v-8h6V4zm0 10H4v4h4v-4zm10 0h-4v4h4v-4z","table":"M2 3h20v18H2V3zm2 4v5h7V7H4zm9 0v5h7V7h-7zm7 7h-7v5h7v-5zm-9 5v-5H4v5h7z","file":"M3 22h18V8h-2V6h-2v2h-2V6h2V4h-2V2H3v20zm2-2V4h8v6h6v10H5z"};
function ci(name){ return '<svg class="ci" viewBox="0 0 24 24" aria-hidden="true"><path d="'+(CI[name]||CI['file-alt'])+'" fill="currentColor"/></svg>'; }
function attIcon(n){
  n=String(n||'').toLowerCase();
  if(/[.](zip|gz|tgz|tar|rar|7z)$/.test(n)) return 'archive';
  if(/[.]pdf$/.test(n)) return 'file';
  if(/[.](mp4|mov|webm|mkv)$/.test(n)) return 'video';
  if(/[.](mp3|m4a|wav|ogg|aac)$/.test(n)) return 'music';
  if(/[.](csv|xlsx?|numbers|tsv)$/.test(n)) return 'table';
  return 'file-alt';
}
// «Screenshot 2026-10-16 at 3.32.47 PM.png» is not something to read in a tile
function attShort(n){
  n=String(n||'file');
  const dot=n.lastIndexOf('.'), ext=dot>0?n.slice(dot):'', stem=dot>0?n.slice(0,dot):n;
  return stem.length>12 ? stem.slice(0,9)+'…'+ext : n;
}
const SORT=(new URLSearchParams(location.search)).get('sort')||'projects';
function setSort(v){
  const u=new URLSearchParams(location.search);
  if(v==='recent') u.set('sort','recent'); else u.delete('sort');
  location.search=u.toString();
}
function switchDev(id){
  const u=new URLSearchParams(location.search);
  if(id && id!=='local') u.set('dev', id); else u.delete('dev');
  location.search=u.toString();
}
function devAdd(){
  const url=prompt('Panel URL of the other device\\n(e.g. http://100.x.y.z:8788)');
  if(!url) return;
  const name=prompt('Name for it (blank = take its hostname)','')||'';
  const kind=prompt('What is it?','Mac mini')||'Mac mini';
  act('add_device', {url:url.trim(), name:name.trim(), kind:kind.trim()}, 'adding device');
}
// Переименование правит строку на месте и НЕ перезагружает страницу: список
// длинный, а reload сбрасывал бы и прокрутку, и открытую справа переписку —
// ровно там, где на них смотрят. Пустое поле возвращает автоматическое имя.
async function rowRename(ev, id){
  ev.stopPropagation();
  const li=document.querySelector('#side .row[data-id="'+id+'"]');
  const box=li && li.querySelector('.tabtitle');
  if(!box || box.querySelector('input')) return;
  const was=box.textContent;
  const inp=document.createElement('input');
  inp.className='tabedit'; inp.value=was; inp.maxLength=40;
  box.textContent=''; box.appendChild(inp);
  inp.focus(); inp.select();
  let done=false;
  async function finish(save){
    if(done) return;
    done=true;
    const val=inp.value.trim();
    box.textContent = (save && val) ? val : was;
    if(!save || val===was) return;
    await act('set_title', {ids:[id], value:val},
              val ? (id+' → '+val) : (id+' → automatic name'), true);
    // Стёрли имя — автоматическое считает сервер, и увидеть его можно только
    // из свежего снимка. Забираем точечно, без перезагрузки страницы.
    if(!val) refreshTitle(id, box);
  }
  // Щелчки внутри поля не должны ни открывать сессию, ни закрывать поле.
  inp.onclick=function(e){ e.stopPropagation(); };
  inp.ondblclick=function(e){ e.stopPropagation(); };
  inp.onkeydown=function(e){
    e.stopPropagation();
    if(e.key==='Enter'){ e.preventDefault(); finish(true); }
    else if(e.key==='Escape'){ e.preventDefault(); finish(false); }
  };
  inp.onblur=function(){ finish(true); };
}
async function refreshTitle(id, box){
  try{
    const r=await fetchT('/api/state?lite=1', {}, 20000);
    const j=await r.json();
    const row=(j.rows||[]).filter(function(x){ return x.short===id; })[0];
    if(row && row.title){ box.textContent=row.title; }
  }catch(e){}
}
// Что уже ушло на сервер и ещё не вернулось: повторное нажатие того же
// самого просто игнорируется. ⛔ Раньше на время ЛЮБОГО действия гасились
// все кнопки страницы — а ответ мог идти десятки секунд (отправка ждёт
// подтверждения доставки), и вся панель стояла деревянной.
const ACT_INFLIGHT={};
function actBusy(){
  document.body.classList.toggle('busy', Object.keys(ACT_INFLIGHT).length>0);
}
async function act(action, target, label, quiet){
  const akey=action+'|'+JSON.stringify(target===undefined?null:target);
  if(ACT_INFLIGHT[akey]) return false;
  ACT_INFLIGHT[akey]=1;
  actBusy();
  // Строку, над которой идёт работа, гасим и мигаем точкой: иначе не видно,
  // к какой именно сессии относится «…».
  let row=null;
  if(typeof target==='string'){ row=document.querySelector('#side .row[data-id="'+target+'"]'); }
  else if(target && target.ids && target.ids.length===1){
    row=document.querySelector('#side .row[data-id="'+target.ids[0]+'"]');
  }
  if(row) row.classList.add('busyact');
  flash('… '+label);
  let ok=false;
  try{
    const r=await fetch('/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:action,target:target,dev:DEV})});
    const j=await r.json();
    ok=j.ok;
    flash(j.ok?('✓ '+j.msg):('✗ '+j.msg));
  }catch(e){ flash('✗ '+e); }
  // quiet — для действий, после которых перерисовывать нечего: ответ на вопрос
  // дочитается опросом, а reload сбрасывал бы прокрутку ленты и раскрытые
  // группы шагов ровно в тот момент, когда на них смотрят.
  if(quiet){
    delete ACT_INFLIGHT[akey];
    actBusy();
    if(row) row.classList.remove('busyact');
    return ok;
  }
  setTimeout(()=>location.reload(), 1200);
  return ok;
}
// ⛔ Плашка обязана гаснуть сама. Без таймера она висела часами: сообщение от
// давней попытки продолжало утверждать что-то про текущее состояние, хотя к
// нему уже не относилось.
let FLASH_T=null;
async function transferTo(id, url, name){
  if(!url) return;
  name=name||url;
  // Сначала git: чисто ли и запушено ли всё, что сессия трогала. Считает
  // машина-владелец сессии. Окружение не едет — его везёт git.
  flash('… checking repos of '+id);
  let chk={ready:false,repos:[]};
  try{ chk=await (await fetch('/api/move_check?id='+encodeURIComponent(id)+dq('&'))).json(); }
  catch(e){ flash('✗ '+e); return; }
  const lines=(chk.repos||[]).map(function(r){
    return (r.ready?'✓ ':'✗ ')+r.dir+' '+r.branch+'@'+r.sha
      +(r.dirty?(' · '+r.dirty+' uncommitted'):'')+(r.ahead>0?(' · '+r.ahead+' unpushed'):'')
      +(r.ahead<0?' · no upstream':'')+(r.remote?'':' · no remote')
      +(r.envs&&r.envs.length?(' · env: '+r.envs.join(',')):''); }).join('\\n');
  const what='\\n\\nThe CONVERSATION moves (transcript, attachments, registry row). '
    +'On the other side these repos are cloned or pulled first, env files carried, '
    +'then the session is brought up past the cap and told to run pwd.';
  if(chk.ready){
    if(!confirm('Move '+id+' to '+name+'?\\n\\nRepos (clean, pushed):\\n'+(lines||'(none touched)')+what)) return;
    await act('transfer',{id:id,to:url,to_name:name,up:true,notify:true,force:false},'moving '+id+' → '+name);
    return;
  }
  if(chk.msg){ flash('✗ '+chk.msg); return; }
  if(confirm('Not ready to move '+id+':\\n'+lines
      +'\\n\\nOK — ask the session to commit, push and update HANDOFF.md; it moves to '+name+' by itself once it answers RC-MOVE-READY and git is clean.'
      +'\\nCancel — decide about moving as is.')){
    await act('move_prepare',{id:id,to:url,up:true,notify:true},'asking '+id+' to push', true);
    return;
  }
  if(confirm('Move '+id+' to '+name+' AS IS?\\n\\nUncommitted work stays on this machine only; the session will not see it there.'+what)){
    await act('transfer',{id:id,to:url,to_name:name,up:true,notify:true,force:true},'moving '+id+' → '+name+' as is');
  }
}
function flash(t){
  gameEmit('flash', {t:String(t)});
  if(IN_HQ && hqStatus(t)) return;
  const f=document.getElementById('flash');
  f.textContent=t;
  f.classList.add('on');
  f.title='click to dismiss';
  if(FLASH_T){ clearTimeout(FLASH_T); FLASH_T=null; }
  // «…» — это «идёт работа», его сменит результат, гасить рано.
  if(String(t).charAt(0)==='\u2026') return;
  const ms = String(t).charAt(0)==='\u2717' ? 15000 : 5000;
  FLASH_T=setTimeout(function(){ f.classList.remove('on'); }, ms);
}
document.addEventListener('click', function(ev){
  if(ev.target && ev.target.id==='flash'){ ev.target.classList.remove('on'); }
  if(ev.target && ev.target.closest && ev.target.closest('#hqst')){ hqStatusClear(); }
});
// HQ: statuses are one quiet line at the composer's edge, not a box across
// the chat (the owner 10-16: «статусы ужасно приходят при отправке сообщений, их
// надо как-то аккуратно где-то показывать»). A send says how it went on its
// own bubble, uploads on the tray, voice on the waveform: their «…» lines are
// not repeated here.
let HQST_T=null;
function hqStatusClear(){
  const el=document.getElementById('hqst');
  if(el) el.className='';
  if(HQST_T){ clearTimeout(HQST_T); HQST_T=null; }
}
function hqStatus(t){
  const el=document.getElementById('hqst');
  if(!el) return false;
  t=String(t);
  const c=t.charAt(0);
  const kind = c==='✗' ? 'bad' : (c==='✓' ? 'ok' : (c==='…' ? 'busy' : 'info'));
  const body = kind==='info' ? t : t.slice(1).trim();
  if(kind==='busy' && /^(sending|uploading|transcribing)/.test(body)) return true;
  el.innerHTML=(kind==='bad' ? ci('close') : (kind==='ok' ? ci('check')
    : '<span class="hqspin"><i></i><i></i><i></i></span>'))+'<span class="t"></span>';
  el.querySelector('.t').textContent=body;
  el.title=body+' · click to hide';
  el.className='on '+kind;
  if(HQST_T){ clearTimeout(HQST_T); HQST_T=null; }
  HQST_T=setTimeout(hqStatusClear, kind==='bad' ? 12000 : (kind==='busy' ? 90000 : 3200));
  return true;
}
// Список процессов под баром Load: кто ест память и кнопка убить. Пока открыт,
// обновляется сам, чтобы после kill сразу было видно, что освободилось.
// ⛔ В этом блоке ни одного бэкслеша: JS лежит в питоновской строке, и каждый
// уровень съедает по слою экранирования (регексп с бэкслешем уже ронял всю страницу).
let PROCS_T=null;
const NL=String.fromCharCode(10);
function pesc(t){ return String(t==null?'':t).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function pmb(v){ v=v||0; return v>=1024?(v/1024).toFixed(1)+'G':Math.round(v)+'M'; }
async function toggleProcs(){
  const box=document.getElementById('procs'); if(!box) return;
  if(!box.hidden){ box.hidden=true; if(PROCS_T){ clearInterval(PROCS_T); PROCS_T=null; } return; }
  box.hidden=false; box.innerHTML='<div class="phead">… reading processes</div>';
  await loadProcs();
  PROCS_T=setInterval(loadProcs, 5000);
}
async function loadProcs(){
  const box=document.getElementById('procs'); if(!box||box.hidden) return;
  let j;
  try{ j=await (await fetch('/api/procs'+dq('?'))).json(); }
  catch(e){ box.innerHTML='<div class="phead">✗ '+pesc(e)+'</div>'; return; }
  if(!j.ok){ box.innerHTML='<div class="phead">✗ '+pesc(j.msg||'')+'</div>'; return; }
  const t=j.totals||{};
  let h='<div class="phead"><span>sessions '+pmb(t.sessions_mb)+' · chrome '+pmb(t.chrome_mb)
    +' · other '+pmb(t.other_mb)+'</span><span>top '+j.procs.length+' of '+(t.count||0)+'</span></div>';
  for(const p of j.procs){
    const label=p.uuid ? ((p.main?'● ':'· ')+(p.title||p.uuid.slice(0,8))+' · '+p.cmd) : p.cmd;
    h+='<div class="prow" title="'+pesc(p.full)+' (pid '+p.pid+', up '+pesc(p.etime)+')">'
      +'<span class="pm">'+pmb(p.rss_mb)+'</span><span class="pc">'+p.cpu+'%</span>'
      +'<span class="pn'+(p.uuid?' sess':'')+'">'+pesc(label)+'</span>'
      +(p.killable?('<button class="danger pk" data-pid="'+p.pid+'" data-uuid="'+pesc(p.uuid||'')
        +'" data-main="'+(p.main?1:0)+'" data-label="'+pesc((p.title||p.cmd||'').slice(0,60))+'">✕</button>'):'')
      +'</div>';
  }
  box.innerHTML=h;
}
document.addEventListener('click', function(ev){
  const b=ev.target && ev.target.closest ? ev.target.closest('button.pk') : null;
  if(!b) return;
  ev.stopPropagation(); ev.preventDefault();
  killProc(parseInt(b.dataset.pid,10), b.dataset.uuid||'', b.dataset.main==='1', b.dataset.label||'');
});
async function killProc(pid, uuid, main, label){
  if(uuid && main){
    if(!confirm('Kill session «'+label+'» (pid '+pid+')?'+NL+NL+'It goes to Retired; Revive brings it back.')) return;
    await act('kill', uuid, 'kill '+label); return;
  }
  if(!confirm('Kill process '+pid+' «'+label+'»?'+(uuid?(NL+NL+'Only this process, not the whole session.'):''))) return;
  await act('kill_proc', {pid:pid}, 'kill pid '+pid, true);
  loadProcs();
}
// preflight prints a multi-line report and changes nothing — show it in place
// instead of reloading the page out from under it
function accOpen(){
  document.getElementById('accmodal').classList.add('on');
  document.getElementById('accscrim').classList.add('on');
}
function accClose(){
  document.getElementById('accmodal').classList.remove('on');
  document.getElementById('accscrim').classList.remove('on');
}
async function report(){
  flash('… building report');
  const pre=document.getElementById('report');
  try{
    const r=await fetch('/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'preflight',target:null})});
    const j=await r.json();
    pre.textContent=j.msg; pre.style.display='block'; flash(j.ok?'✓ report ready':'✗ '+j.msg);
  }catch(e){ flash('✗ '+e); }
}

// Вход — два шага с человеком посередине, поэтому здесь свой поток: act()
// перезагружает страницу через 1.2 с и убил бы шаг с кодом. Сервер ничего не
// ждёт, состояние забирает опрос /api/auth.
var authTimer=null;
function authStop(){ if(authTimer){ clearInterval(authTimer); authTimer=null; } }
function authBusy(on){
  ['authgo','authok'].forEach(function(id){
    var b=document.getElementById(id); if(b) b.disabled=on; });
}
// dev:DEV — вход идёт на ВЫБРАННУЮ машину. Без него флоу запускался на той,
// чью панель открыли, и «войти на a Linux VPS» дважды перелогинивало мини (04-09).
async function authPost(action, target){
  const r=await fetch('/act',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:action,target:target,dev:DEV})});
  return r.json();
}
async function authPoll(){
  try{
    const s=await (await fetch('/api/auth'+(DEV?('?dev='+encodeURIComponent(DEV)):''))).json();
    const a=document.getElementById('authurl');
    if(s.url && a.href!==s.url){ a.href=s.url; a.textContent=s.url; }
    if(s.switched){
      authStop();
      flash('✓ signed in: '+s.account+' · the guardian migrates sessions itself');
      setTimeout(function(){ location.reload(); }, 2500);
      return;
    }
    if(!s.running){
      authStop();
      document.getElementById('authbox').style.display='none';
      flash('✗ flow ended, account unchanged · '+(s.tail||''));
    }
  }catch(e){ /* network blinked — the next tick sorts it out */ }
}
async function authStart(){
  if(!confirm('Sign in to a different account?\\n\\nOnce it completes, live sessions '
    +'lose their relay and are relaunched under the new account. '
    +'This takes a few minutes. Continue?')) return;
  authStop(); authBusy(true);
  const a=document.getElementById('authurl');
  a.removeAttribute('href'); a.textContent='… waiting for the link';
  document.getElementById('authcode').value='';
  flash('… starting sign-in');
  try{
    const j=await authPost('auth_start',{email:document.getElementById('authmail').value});
    if(!j.ok){ document.getElementById('authbox').style.display='none';
               flash('✗ '+j.msg); return; }
    document.getElementById('authbox').style.display='block';
    flash('✓ '+j.msg);
    authTimer=setInterval(authPoll, 2000); authPoll();
  }catch(e){ flash('✗ '+e); }
  finally{ authBusy(false); }
}
async function authCode(){
  const code=document.getElementById('authcode').value.trim();
  if(!code){ flash('✗ empty code'); return; }
  authBusy(true);
  try{
    const j=await authPost('auth_code',{code:code});
    flash(j.ok?('✓ '+j.msg):('✗ '+j.msg));
    if(!j.ok){ authStop(); document.getElementById('authbox').style.display='none'; }
  }catch(e){ flash('✗ '+e); }
  finally{ authBusy(false); }
}
async function authCancel(){
  authStop(); authBusy(true);
  try{
    const j=await authPost('auth_cancel',null);
    document.getElementById('authbox').style.display='none';
    flash(j.ok?('✓ '+j.msg):('✗ '+j.msg));
  }catch(e){ flash('✗ '+e); }
  finally{ authBusy(false); }
}

async function migrate(){
  if(!confirm('Migration stops ALL restored sessions, restarts the daemon and '
    +'relaunches them under the current account. Cards get new links. Continue?')) return;
  act('migrate', null, 'migrating sessions — this takes minutes');
}

// bulk «Снять» — the alternative was an ssh one-liner, which is not a control surface
async function retireOld(){
  const h=prompt('Retire everything untouched for longer than how many hours?\\n'
    +'6 clears yesterday, 24 keeps all of yesterday.','6');
  if(h===null) return;
  const n=parseFloat(h);
  if(!(n>=1)){ flash('✗ need a number of hours, minimum 1'); return; }
  if(!confirm('Retired sessions leave the queue — the guardian stops restoring them. '
    +'Nothing is deleted, any of them can come back via Queue. Continue?')) return;
  // без перезагрузки: колонку и счётчики пересобираем на месте
  await act('retire_older', n, 'retiring everything older than '+n+'h', true);
  SIDE_SIG=null; reloadSide(); pulse();
}

async function killEmpty(){
  if(!confirm('Kill live sessions with no messages at all? These are claude -p \"/usage\" '
    +'artefacts — nothing inside to lose, and the slots go to real work.')) return;
  await act('kill_empty', null, 'killing empty sessions', true);
  SIDE_SIG=null; reloadSide(); pulse();
}
// Kill для чата из приложения — тоже на месте, строка просто гаснет.
async function appKill(ev, pid){
  ev.stopPropagation();
  const li=ev.currentTarget.closest('li');
  const ok=await act('killapp', pid, 'killing pid '+pid, true);
  if(ok && li) li.classList.add('gone');
  pulse();
}

// ---- множественный выбор ----
function picks(){return [...document.querySelectorAll('.pick:checked')].map(c=>c.value);}
function syncBar(){
  const n=picks().length, bar=document.getElementById('bulkbar');
  document.getElementById('bulkn').textContent=n;
  bar.classList.toggle('on', n>0);
  document.getElementById('side').classList.toggle('picking', n>0);
}
function pickSection(el){
  el.closest('section').querySelectorAll('.pick').forEach(c=>{c.checked=el.checked;});
  syncBar();
}
function clearPicks(){
  document.querySelectorAll('.pick:checked,.allbox input:checked').forEach(c=>{c.checked=false;});
  syncBar();
}
// модель и эффорт живут в реестре и подхватываются при следующем подъёме —
// живую сессию на лету не переключить, она уже запущена с этими флагами
async function bulkOpt(kind){
  const sel=document.getElementById(kind==='model'?'selmodel':'seleffort');
  const v=sel.value;
  const ids=picks();
  if(!ids.length){ flash('\\u2717 nothing selected'); return; }
  const label=v?(kind==='model'?'model '+v:'effort '+v)
               :(kind==='model'?'model as running':'effort as running');
  if(!confirm(ids.length+' session(s) selected.\\n'+label+'\\n\\nApplies on the next '
    +'launch — sessions running now will not switch. Continue?')) return;
  // Здесь перезагрузка ОСТАЁТСЯ: меняются десятки строк, и готовая разметка
  // их шапок устаревает вся сразу — чинить её по кускам дороже и хрупче, чем
  // перечитать страницу. Действие и так за подтверждением, оно редкое.
  act(kind==='model'?'set_model':'set_effort', {ids:ids, value:v}, label);
}

// Выбор живёт здесь, а не в разметке: лента перерисовывается опросом раз в
// полторы секунды и стирала бы любую пометку, поставленную прямо в DOM.
const ASK_LOCAL={};
function askState(k){ return ASK_LOCAL[k] || (ASK_LOCAL[k]={sel:[], sent:false}); }
function askRedraw(){ if(TX_LAST) renderTx(TX_LAST, TX_CUR); }

async function answerAsk(ev, n, multi){
  ev.stopPropagation();
  if(!TX_CUR) return;
  const grp=ev.currentTarget.closest('.qgroup');
  if(!grp || grp.classList.contains('locked') || grp.classList.contains('done')) return;
  const st=askState(grp.dataset.k);
  if(multi){
    // Копим выбор и ждём кнопку: в TUI такой вопрос закрывается Enter, и без
    // него он остался бы открытым, сколько по нему ни щёлкай.
    const at=st.sel.indexOf(n);
    if(at>=0) st.sel.splice(at,1); else st.sel.push(n);
    askRedraw();
    return;
  }
  st.sel=[n]; st.sent=true;
  askRedraw();                       // отметить ДО ответа сервера
  const ok=await act('answer', {id:TX_CUR, choice:n}, 'option '+n, true);
  if(!ok){ st.sel=[]; st.sent=false; askRedraw(); }
  // Claude отвечает не мгновенно — дочитываем ленту, а не дёргаем страницу.
  setTimeout(pollTx, 1200); setTimeout(pollTx, 4000); setTimeout(pollTx, 9000);
}

// ⛔ 10-25: a background agent's permission prompt sat on a session's screen
// for hours with nothing here saying so, and a send typed into it picked the
// highlighted «Yes». The server reads the dialog off the screen (j.perm) and
// refuses to type while it is open; it is shown over the composer with its own
// options, and only a click answers it. The sig goes back with the click: a
// different prompt on the screen by then is not answered.
let PERM_SHOWN=null;
function permShow(p, short){
  const box=document.getElementById('permbox');
  if(!box) return;
  PERM_SHOWN = p ? {sid:short, p:p} : null;
  const html = !p ? '' : '<div class="ask pend"><div class="qh">'
    +(p.kind==='permission' ? 'permission' : 'menu')+' · waiting for you</div>'
    +'<pre class="permtext">'+esc(p.text||'')+'</pre>'
    +'<div class="permbtns">'+(p.options||[]).map(function(o){
        return '<button class="o pick-opt" onclick="permitPick(event,'+Number(o.n)+')">'
          +'<span class="onum">'+Number(o.n)+'</span><b>'+esc(o.label)+'</b></button>';
      }).join('')+'</div>'
    +'<div class="qnote">messages to this session are held until this is answered: '
    +'typing would answer it</div></div>';
  if(box.innerHTML!==html) box.innerHTML=html;
}
async function permitPick(ev, n){
  ev.stopPropagation();
  const cur=PERM_SHOWN;
  if(!cur || cur.sid!==TX_CUR) return;
  const btns=document.querySelectorAll('#permbox button');
  btns.forEach(function(b){ b.disabled=true; });
  const ok=await act('permit', {id:cur.sid, choice:n, sig:cur.p.sig}, 'option '+n, true);
  // the box redraws only when the prompt changes: give the buttons back on a
  // failure at once, and after a while if the screen never moved
  if(!ok) btns.forEach(function(b){ b.disabled=false; });
  else setTimeout(function(){ btns.forEach(function(b){ b.disabled=false; }); }, 8000);
  setTimeout(pollTx, 600); setTimeout(pollTx, 2500); setTimeout(pulse, 3000);
}

async function confirmAsk(ev){
  ev.stopPropagation();
  if(!TX_CUR) return;
  const grp=ev.currentTarget.closest('.qgroup');
  if(!grp) return;
  const st=askState(grp.dataset.k);
  if(!st.sel.length) return;
  st.sent=true; askRedraw();
  const ok=await act('answer', {id:TX_CUR, choice:st.sel.slice(), enter:true},
                     'options '+st.sel.join(','), true);
  if(!ok){ st.sent=false; askRedraw(); }
  setTimeout(pollTx, 1200); setTimeout(pollTx, 4000); setTimeout(pollTx, 9000);
}
// Действия над строкой идут БЕЗ перезагрузки: карточка просто гаснет, а
// счётчики подтягивает ближайший pulse. Перезагрузка ради одного убийства
// сбрасывала прокрутку списка и открытый чат — при разборе десятка сессий это
// невыносимо.
const DEAD_ACTIONS={kill:1, retire:1, bulk_kill:1, bulk_retire:1};
const ST_AFTER={kill:'retired', retire:'retired', bulk_kill:'retired', bulk_retire:'retired',
  kill_requeue:'queued', unretire:'queued', bulk_unretire:'queued',
  up:'launching…', bulk_up:'launching…', bulk_restart:'restarting…'};
function markRow(id, action){
  const li=document.querySelector('#side .row[data-id="'+id+'"]');
  if(!li) return;
  if(DEAD_ACTIONS[action]){
    li.classList.remove('live','busyrow','waitrow','queue');
    li.classList.add('ret','gone');
  }
  const word=ST_AFTER[action], st=li.querySelector('.st');
  if(word && st){ st.textContent=word; st.className='st st-act'; }
  const a=li.querySelector('.rowacts'); if(a) a.innerHTML='';
}
// Удалить: строка уходит из колонки целиком, открытый чат закрывается.
function goneRow(id){
  const li=document.querySelector('#side .row[data-id="'+id+'"]');
  if(li) li.remove();
  if(typeof TX_CUR!=='undefined' && TX_CUR===id) closeTx();
}
async function delSession(ev, id){
  if(ev){ ev.stopPropagation(); ev.preventDefault(); }
  if(!confirm('Delete session '+id+' for good?\\nIf it is running it is stopped. '
    +'It leaves the panel and will not come back; the transcript sits in '
    +'~/.claude/rc-trash for 7 days, then it is purged.')) return;
  const ok=await act('delete', id, 'deleting '+id, true);
  if(ok){ goneRow(id); SIDE_SIG=null; }
  pulse();
}
async function rowAct(ev, action, id){
  ev.stopPropagation();          // иначе клик по иконке ещё и откроет чат
  ev.preventDefault();
  const ok=await act(action, id, action+' '+id, true);
  if(ok) markRow(id, action);
  pulse();
}
// Выбранное после загрузки страницы. Нужно, потому что шапка чата рисуется из
// ROWS[id].opts — готовой разметки, сложенной сервером на момент загрузки. Без
// этой памяти уход в другую сессию и возврат обратно показали бы прежнее
// значение, будто смены не было.
const OPT_LOCAL={};
function optRemember(id, kind, v){
  (OPT_LOCAL[id]=OPT_LOCAL[id]||{})[kind]=v;
  if(typeof ROWS!=='undefined' && ROWS[id]){
    // lastUsed() читает именно это — иначе черновик подставлял бы старую
    // модель до следующей перезагрузки страницы.
    if(kind==='effort') ROWS[id].effort=v; else ROWS[id].model=v;
  }
}
function optApply(id){
  const o=OPT_LOCAL[id]; if(!o) return;
  document.querySelectorAll('#drextraopts select[data-opt]').forEach(function(sel){
    const v=o[sel.dataset.opt];
    if(v!==undefined){ sel.value=v; sel.dataset.was=v; }
  });
}
// Выбор показывается и запоминается СРАЗУ, а сервер догоняет. ⛔ Раньше
// селект ждал ответа, а ответ стоял в общей очереди действий за отправкой
// сообщения (до 25 с) — и при любой ошибке по дороге откатывался на пустой
// пункт, то есть на безымянное «default model». Запросы по одному селекту
// идут строго по очереди: иначе быстрые «A, потом B» могли лечь в реестр
// как «B, потом A».
const OPT_CHAIN={}, OPT_SEQ={};
function rowOpt(el, id, kind){
  const v=el.value, k=id+':'+kind;
  const seq=(OPT_SEQ[k]||0)+1; OPT_SEQ[k]=seq;
  if(el.dataset.ok===undefined) el.dataset.ok=el.dataset.was||'';
  optRemember(id, kind, v);
  el.classList.add('saving');
  const label=(kind==='model'?'model ':'effort ')+(v?(el.options[el.selectedIndex]||{}).text||v
               :'as running')+' · '+id;
  OPT_CHAIN[k]=(OPT_CHAIN[k]||Promise.resolve()).then(function(){
    return act(kind==='model'?'set_model':'set_effort', {ids:[id], value:v}, label, true);
  }).then(function(ok){
    if(ok) el.dataset.ok=v;
    if(OPT_SEQ[k]!==seq) return;          // later choice owns the select now
    el.classList.remove('saving');
    if(ok){ el.dataset.was=v; return; }
    // не применилось — возвращаем последнее ПОДТВЕРЖДЁННОЕ значение
    const back=el.dataset.ok||'';
    el.value=back; el.dataset.was=back; optRemember(id, kind, back);
  });
}
async function bulk(action, verb){
  const ids=picks();
  if(!ids.length){ flash('✗ nothing selected'); return; }
  if(!confirm(ids.length+' session(s) selected.\\n'+verb+'\\nContinue?')) return;
  const ok=await act(action, ids, verb.toLowerCase()+' ('+ids.length+')', true);
  if(ok) ids.forEach(function(id){ action==='bulk_delete' ? goneRow(id) : markRow(id, action); });
  if(ok && action==='bulk_delete') SIDE_SIG=null;
  clearPicks();
  pulse();
}
document.addEventListener('change', e=>{
  if(e.target && e.target.classList && e.target.classList.contains('pick')) syncBar();
});

// ---- переписка в правой шторке ----
function esc(s){return String(s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}

// Транскрипт приходит сырым markdown — раньше шторка печатала его как есть,
// звёздочками и решётками. Рендерим то, что реально встречается в ответах:
// код, жирный, курсив, заголовки, списки, ссылки, таблицы. Остальное осознанно
// остаётся текстом.
//
// Порядок здесь не косметика. Код паркуется в плейсхолдеры первым, иначе
// разметка ниже перепишет его содержимое; плейсхолдеры доживают до конца, чтобы
// перевод строки внутри ``` не стал <br>; и только после распарковки чистятся
// <br> вокруг блочных тегов, которых до неё ещё не видно.
//
// Метка плейсхолдера содержит сырой "<". После esc() такого символа в тексте
// быть не может по построению — значит, столкнуться с содержимым она не может.
function md(s){
  var slots=[];
  function park(html){ slots.push(html); return '<x-slot '+(slots.length-1)+'>'; }
  // строчная разметка отдельной функцией: ячейки таблицы паркуются раньше, чем
  // основной проход, и без этого «**9**» внутри ячейки так и оставался звёздами
  function inline(x){
    x=x.replace(/\\*\\*([^*\\n]+)\\*\\*/g,'<strong>$1</strong>');
    x=x.replace(/(^|[\\s(\\[])\\*([^*\\n]+)\\*(?=[\\s).,:;!?\\]]|$)/g,'$1<em>$2</em>');
    x=x.replace(/(^|[\\s(\\[])_([^_\\n]+)_(?=[\\s).,:;!?\\]]|$)/g,'$1<em>$2</em>');
    return x.replace(/\\[([^\\]\\n]+)\\]\\((https?:[^)\\s]+)\\)/g,
                     '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  }
  var t=esc(String(s));

  t=t.replace(/```[a-zA-Z0-9_+.-]*\\n?([\\s\\S]*?)```/g,function(_,code){
    return park('<pre><code>'+code.replace(/\\n+$/,'')+'</code></pre>');});
  t=t.replace(/`([^`\\n]+)`/g,function(_,code){ return park('<code>'+code+'</code>'); });

  // таблица = шапка, строка-разделитель, тело. Без разделителя это просто текст
  // с вертикальными чертами, и трогать его нельзя.
  t=t.replace(/(?:^[ \\t]*\\|.*\\|[ \\t]*\\n?)+/gm,function(block){
    var rows=block.trim().split('\\n').map(function(r){return r.trim();});
    if(rows.length<2 || !/^\\|[\\s:|-]+\\|$/.test(rows[1])) return block;
    function cells(r){ return r.replace(/^\\||\\|$/g,'').split('|')
                        .map(function(c){return c.trim();}); }
    var html='<table><thead><tr>'
      +cells(rows[0]).map(function(c){return '<th>'+inline(c)+'</th>';}).join('')
      +'</tr></thead><tbody>'
      +rows.slice(2).map(function(r){
          return '<tr>'+cells(r).map(function(c){return '<td>'+inline(c)+'</td>';}).join('')+'</tr>';
        }).join('')
      +'</tbody></table>';
    return park(html)+'\\n';});

  t=t.replace(/^[ \\t]*#{1,6}[ \\t]+(.+?)[ \\t]*$/gm,'<div class="mdh">$1</div>');
  t=t.replace(/^[ \\t]*(?:---|\\*\\*\\*|___)[ \\t]*$/gm,'<hr>');

  // Цитата. Claude присылает ею готовый текст для отправки кому-то — письмо,
  // сообщение в чат, — и без разбора «&gt;» оставался в ленте символом в начале
  // каждой строки, чего в приложении Claude не видно (6ac1a8b1, 10.09).
  // ⛔ Ищем именно «&gt;»: esc() отработал выше, настоящего «>» здесь уже нет.
  // Разметку внутри применяем сами и паркуем результат, иначе следующий проход
  // разберёт его второй раз.
  t=t.replace(/(?:^[ \\t]*&gt;[ \\t]?.*(?:\\n|$))+/gm,function(block){
    var body=block.replace(/\\n+$/,'').split('\\n')
      .map(function(l){ return l.replace(/^[ \\t]*&gt;[ \\t]?/,''); })
      .join('\\n');
    return park('<blockquote>'+inline(body).replace(/\\n/g,'<br>')+'</blockquote>')+'\\n';
  });
  t=inline(t);

  t=t.replace(/^[ \\t]*[-*+][ \\t]+(.+?)[ \\t]*$/gm,'<li>$1</li>');
  t=t.replace(/^[ \\t]*\\d+[.)][ \\t]+(.+?)[ \\t]*$/gm,'<li>$1</li>');
  t=t.replace(/(?:<li>[\\s\\S]*?<\\/li>\\n?)+/g,function(run){
    return '<ul>'+run.replace(/\\n/g,'')+'</ul>';});

  t=t.replace(/\\n/g,'<br>');
  t=t.replace(/<x-slot (\\d+)>/g,function(_,i){return slots[+i];});

  t=t.replace(/(?:<br>\\s*)+(<(?:ul|pre|table|hr|div|blockquote)\\b)/g,'$1');
  t=t.replace(/(<\\/(?:ul|pre|table|div|blockquote)>|<hr>)(?:\\s*<br>)+/g,'$1');
  return t;
}
let TX_CUR=null, TX_TIMER=null;
// Начало текущего хода — отметка последней РЕПЛИКИ человека в ленте. Возраст
// последней записи транскрипта показывает возраст шага («12s»), а спросили
// про время работы над всей задачей — это две разные величины.
let TURN_TSE=null, TURN_SID=null, TURN_USE=null;
// Отправленное показываем сразу, не дожидаясь, пока оно доедет до транскрипта:
// подтверждение доставки занимает секунды, и до него чат стоял мёртвым.
const BUSY_JS=60;   // держать в согласии с BUSY_S в rc_control.py
// Статус приходит с сервера — он снят с экрана TUI, а не выведен из тишины
// в транскрипте. Поэтому «работает» видно и во время компакта, и когда
// сообщение стоит в очереди: раньше на этих местах не было ничего, и это
// читалось как «команда не дошла».
// Сколько уже идёт — это возраст последней записи транскрипта: пока сессия
// сидит в одном длинном шаге, туда не падает ничего, и ровно этот промежуток
// человек и хочет видеть («8 минут назад и серия шагов, а статуса нет»).
function tuiSpan(sec){
  if(sec==null || !isFinite(sec) || sec<0) return '';
  const s=Math.round(sec);
  if(s<60) return s+'s';
  if(s<3600) return Math.floor(s/60)+'m';
  return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
}
// Токены за ВЕСЬ прогон считает сервер (поле turn), с той же точки отсчёта,
// что и время. Число с экрана TUI — счётчик одного запроса, оно и выглядело
// «непонятным»: сбрасывалось на каждом шаге.
function tuiTok(use, t){
  if(use && use.out) return '↓'+tokShort(use.out);
  return t && t.detail ? t.detail : '';
}
function tuiTitle(use){
  if(!use || !use.steps) return '';
  return 'this run: the model wrote '+tokShort(use.out)
        +', sent '+tokShort(use.in)
        +', read from cache '+tokShort(use.cache_read)
        +' · model replies: '+use.steps;
}
function tuiText(t, age, use){
  if(!t || !t.kind) return ['',''];
  const span=tuiSpan(age);
  if(t.kind==='compacting') return ['compacting context'+(span?' · '+span:''),''];
  if(t.kind==='queued')     return ['queued','queued'];
  if(t.kind==='working'){
    const bits=['working'];
    if(span) bits.push(span);
    const tok=tuiTok(use, t);
    if(tok) bits.push(tok);
    return [bits.join(' · '),''];
  }
  if(t.kind==='idle')       return ['ready','ready'];
  return ['',''];
}
// В узкой колонке слева слова «working» намеренно нет — работу показывают
// кружок, рамка и подсветка (см. row_html). Но компакт и очередь ими не
// выражены никак, и без подписи выглядят как «ничего не происходит».
function tuiChip(t){
  if(!t) return '';
  if(t.kind==='compacting') return '<span class="tflag tui">compacting</span>';
  if(t.kind==='queued') return '<span class="tflag tui queued">queued</span>';
  return '';
}
let PEND=[], PEND_ID=0, TX_LAST=null;
const INVIS_RE=new RegExp('['+[[0x200B,0x200F],[0x202A,0x202E],[0x2060,0x2064],[0x2066,0x2069]].map(function(r){
  return String.fromCharCode(r[0])+'-'+String.fromCharCode(r[1]); }).join('')+String.fromCharCode(0xFEFF)+String.fromCharCode(0xAD)+']','g');
function pendNorm(t){ return String(t||'').replace(INVIS_RE,'').replace(/\\s+/g,' ').trim(); }
/* Ожидающих реплик может быть несколько: занятой сессии пишут подряд, не
   дожидаясь ответа. Раньше здесь была ОДНА ячейка, и второе сообщение
   затирало пузырь первого — на экране оставалось только последнее.
   Сверяем по тому, что РЕАЛЬНО ушло в сессию, а не по показанному тексту:
   у вложений они разные (в ленте «📎 имя», в сессию уходит путь). */
function pendAdd(shown, payload, sid, att){
  const e={id:++PEND_ID, sid:sid||TX_CUR||'', text:shown, raw:payload,
           att:att||[],
           probe:pendNorm(payload).slice(0,40), ok:false, at:Date.now()};
  PEND.push(e); return e;
}
// ⛔ Отрисовка НЕ должна зависеть от того, прочитана ли лента. Раньше каждый
// вызов стоял под «if(TX_LAST)», а TX_LAST равен null, пока не ответил
// /transcript — и если тот ответ не пришёл или пришёл с ошибкой, сессия
// оставалась без ленты НАВСЕГДА. В такой сессии отправленное не показывалось
// вообще: пузыря нет, опроса нет, сообщение уходило вслепую и всплывало
// только после перезагрузки страницы. Пузырь важнее ленты — рисуем и без неё.
function txDraw(sid){
  const s=(sid===undefined)?TX_CUR:sid;
  renderTx(TX_LAST || {shown:[], artifacts:[], age:null, stub:true}, s);
}
// ⛔ Ожидающая реплика привязана к сессии. Раньше очередь была общей и
// очищалась при переходе в другую сессию — уход и возврат стирали с экрана
// сообщение, которое ещё не доехало до транскрипта (а занятая сессия держит
// его в своей очереди минутами). Показываем и сверяем только своё.
function pendFor(sid){
  return PEND.filter(function(p){ return p.sid===sid; });
}
function pendDrop(e){ if(e) PEND=PEND.filter(function(x){ return x!==e; }); }
function pendClear(){ PEND=[]; }
// Вернуть неподтверждённый текст в поле ввода. Автоматически не возвращаем:
// сообщение ещё может дойти, и подставленный текст легко отправить дважды.
function pendBack(id){
  const p=PEND.filter(function(x){ return x.id===id; })[0];
  if(!p) return;
  const ta=document.getElementById('drtext');
  ta.value=(ta.value?ta.value.replace(/\\s+$/,'')+String.fromCharCode(10):'')
           +(p.raw||p.text);
  taGrow(ta); ta.focus();
  pendForget(id);
}
function pendForget(id){
  // a dismissed or taken-back message must not go out later from the outbox
  PEND.forEach(function(x){ if(x.id===id){ x.cancelled=true; if(x.nonce) outboxDrop(x.nonce); } });
  PEND=PEND.filter(function(x){ return x.id!==id; });
  txDraw(TX_CUR);
}
// Пузырь живёт, пока его реплика не найдена в ленте. Два прежних повода
// погасить его раньше времени оба оказались ложными — см. ниже.
const PEND_QUEUED_MS = 30000;      // после этого подпись «sending…» — уже враньё
const PEND_MAX_MS    = 30*60000;   // предохранитель от бессмертного пузыря
function pendReconcile(j, sid){
  if(!PEND.length) return;
  const users=(j.shown||[]).filter(function(m){ return m.role==='user'; })
                           .map(function(m){
                             return {t:pendNorm(m.text), tse:m.tse||0};
                           });
  const used={}, now=Date.now();
  PEND=PEND.filter(function(p){
    if(p.sid!==sid) return true;      // чужую очередь эта лента не закрывает
    // ⛔ Сверка шла по вхождению подстроки со ВСЕЙ лентой, без оглядки на
    // время. Короткое «да», «ок», «продолжай» находится внутри любого
    // старого сообщения — свежий пузырь гас на первой же отрисовке, и на
    // экране не оставалось ничего. Смотрим только на реплики, которые
    // МОГЛИ быть этой отправкой: две минуты запаса на расхождение часов
    // панели и мини, остальное отсекаем.
    const floor=(p.at-120000)/1000;
    for(let i=0;i<users.length;i++){
      // Одна реплика ленты закрывает одну ожидающую: без этого два
      // одинаковых текста подряд схлопнулись бы в один пузырь.
      if(used[i]) continue;
      if(users[i].tse && users[i].tse < floor) continue;
      if(p.probe && users[i].t.indexOf(p.probe)>=0){ used[i]=1; return false; }
    }
    // ⛔ Здесь пузырь снимался через 30 секунд, если сервер подтвердил
    // доставку. Но занятая сессия держит сообщение в очереди TUI МИНУТАМИ,
    // и записи в транскрипте всё это время нет — сообщение просто исчезало
    // с экрана, а появлялось лишь после перезагрузки страницы. Пузырь —
    // единственное место, где текст ещё виден человеку, поэтому оставляем
    // его до реального совпадения; через полминуты только меняем подпись на
    // честную «в очереди» и даём убрать вручную.
    if(!p.warn && p.ok && now-p.at > PEND_QUEUED_MS) p.queued=true;
    return now-p.at < PEND_MAX_MS;
  });
}
// Шаги показываем по умолчанию — без них не видно, что сессия вообще работает.
// Переключателя для размышлений нет намеренно: Claude Code хранит в транскрипте
// только подпись thinking-блока, сам текст пуст (проверено 08-24), так что
// показывать было бы нечего.
// какие группы шагов человек раскрыл — переживает перерисовку при опросе
const TX_OPEN=new Set();
function stepToggle(el){
  const k=el.getAttribute('data-k');
  if(el.open) TX_OPEN.add(k); else TX_OPEN.delete(k);
}
function stepWord(n){
  const d=n%10, s=n%100;
  return n===1 ? 'step' : 'steps';
}
function applyFilters(){
  const st=document.getElementById('tgSteps').checked;
  document.querySelectorAll('#drbody details.steps').forEach(function(e){ e.classList.toggle('hidden', !st); });
}
// Черновик новой сессии. Раньше промпт набирался в узкой колонке слева и
// сессия создавалась сразу по кнопке. Теперь это обычный пустой чат: пишешь,
// выбираешь модель с эффортом и только отправка заводит сессию.
let DRAFT=false;
function lastUsed(){
  // «Последнее, чем пользовались» берём у самой свежей сессии, У КОТОРОЙ это
  // значение вообще есть. Просто самая свежая не годится: модель и усилие
  // вычисляются из дайджеста, а у только что заведённой сессии его ещё нет —
  // и черновик сбрасывался на «default» ровно после того, как сессию создали.
  // Модель и усилие ищем независимо: пустыми они пустеют не всегда вместе.
  const rows=[];
  for(const k in ROWS) rows.push(ROWS[k]);
  rows.sort(function(a,b){ return (b.ts||0)-(a.ts||0); });
  const pick=function(f){
    for(let i=0;i<rows.length;i++){ if(rows[i][f]) return rows[i][f]; }
    return '';
  };
  return {model:pick('model'), effort:pick('effort')};
}
function newDraft(){
  draftSave();
  DRAFT=true; TX_CUR=null;
  stopVis(false);
  if(TX_TIMER){ clearTimeout(TX_TIMER); TX_TIMER=null; }
  try{ history.replaceState(null,'','#new'); }catch(e){}
  markSel(null);
  document.getElementById('drtitle').textContent='New session';
  document.getElementById('drmeta').textContent='draft — the session is created when you send';
  document.getElementById('dracts').innerHTML='';
  document.getElementById('drextra').innerHTML='';
  // У черновика раскрывать под «…» нечего — сессии ещё нет, а модель и усилие
  // и так на виду. Кнопка, которая ничего не делает, только сбивает.
  // ⛔ Меню зовётся drmenu, а не drmore: старое имя пережило переименование и
  // роняло newDraft() на пустом getElementById ровно тут. Дальше по функции —
  // модель, усилие, подсказка и фокус в поле, то есть весь черновик, поэтому
  // «+ New session» выглядела как незалипающая кнопка, если на экране была
  // открыта другая сессия: заголовок менялся, а тело оставалось чужим.
  const drmenu=document.getElementById('drmenu');
  if(drmenu) drmenu.style.display='none';
  document.getElementById('drdot').className='dot';
  const lu=lastUsed();
  document.getElementById('drextraopts').innerHTML=
     draftSelect('draftmodel', DRAFT_MODELS, lu.model||'')
    +draftSelect('drafteffort', DRAFT_EFFORTS, lu.effort||'');
  document.getElementById('drbody').innerHTML=
    '<div id="mainempty">Write the first prompt below.<br>'
    +'Model and effort are preset to what the latest session runs.</div>';
  const ta=document.getElementById('drtext');
  ta.placeholder='First prompt — Enter creates the session';
  draftLoad(''); ta.focus();
}
function draftSelect(id, list, cur){
  return '<select id="'+id+'" class="rowopt">'
    +list.map(function(o){
        return '<option value="'+esc(o[0])+'"'+(o[0]===cur?' selected':'')+'>'
               +esc(o[1])+'</option>';
      }).join('')+'</select>';
}
// ---- просмотр файла ----
let FILE_CUR=null;
function fileUrl(p){ return '/file?path='+encodeURIComponent(p)+dq('&'); }
function isImgPath(p){ return /\\.(png|jpe?g|gif|webp|svg)$/.test(String(p||'').toLowerCase()); }
function fileDownload(){ if(FILE_CUR) window.open(fileUrl(FILE_CUR), '_blank'); }
function closeFile(){
  FILE_CUR=null;
  document.body.classList.remove('filed');
  document.getElementById('fbody').innerHTML='';
  document.getElementById('fname').textContent='';
}
async function openFile(path){
  FILE_CUR=path;
  document.body.classList.add('filed');
  const name=document.getElementById('fname'), box=document.getElementById('fbody');
  name.textContent=path; name.title=path;
  box.innerHTML='<pre>… reading</pre>';
  const low=path.toLowerCase();
  if(isImgPath(low)){
    box.innerHTML='<img src="'+esc(fileUrl(path))+'" alt="">';
    return;
  }
  if(/\\.(pdf|html?)$/.test(low)){
    // В iframe, а не в саму страницу: отчёт рисуется как задумано, но живёт
    // в своём origin и до панели не дотягивается.
    box.innerHTML='<iframe src="'+esc(fileUrl(path))+'"></iframe>';
    return;
  }
  try{
    const r=await fetch(fileUrl(path));
    const t=await r.text();
    if(!r.ok){ box.innerHTML='<pre>✗ '+esc(t)+'</pre>'; return; }
    // markdown рисуем разметкой — его открывают, чтобы прочитать отчёт,
    // а не посмотреть на решётки заголовков.
    box.innerHTML = /\\.(md|markdown)$/.test(low)
      ? '<div class="msg"><div class="mtext">'+md(t)+'</div></div>'
      : '<pre>'+esc(t)+'</pre>';
  }catch(e){ box.innerHTML='<pre>✗ '+esc(e)+'</pre>'; }
}
// Одно меню в шапке вместо кнопки «…» и отдельного «move to…»: карточка,
// переезд на каждую из других машин, повтор заметки о переезде.
function menuFill(short, items){
  const m=document.getElementById('drmenu');
  m.innerHTML='<option value="">⋯</option>'+items.map(function(it){
    return '<option value="'+esc(it.v)+'" data-name="'+esc(it.n||'')+'">'+esc(it.t)+'</option>'; }).join('');
  m.setAttribute('data-id', short);
  m.style.display = items.length ? '' : 'none';
}
async function menuPick(sel){
  const v=sel.value, id=sel.getAttribute('data-id');
  const name=sel.options[sel.selectedIndex].getAttribute('data-name')||'';
  sel.value='';
  if(!v || !id) return;
  if(v.indexOf('card:')===0){ window.open(v.slice(5), '_blank'); return; }
  if(v.indexOf('move:')===0){ await transferTo(id, v.slice(5), name); return; }
  if(v==='note'){ await act('move_note',{id:id},'re-sending move note to '+id,true); return; }
  if(v==='delete'){ await delSession(null, id); return; }
}
function tokShort(n){
  if(n>=1000000) return (n/1000000).toFixed(1).replace(/\\.0$/,'')+'M';
  if(n>=1000) return (n/1000).toFixed(1)+'k';
  return String(n);
}
// Одна строка вместо четырёх: id, время, счётчики, pid и контекст жили
// раздельно и повторяли друг друга — «крутится» дублировало «работает»,
// а id печатался дважды.
function drMetaLine(short, j){
  const i=(typeof ROWS!=='undefined' && ROWS[short]) || {};
  const bits=[short];
  if(i.ts) bits.push(agoRu(Date.now()/1000-i.ts));
  if(j) bits.push(j.total+' msg');
  if(j) bits.push((j.steps||0)+' steps');
  if(i.pid) bits.push('pid '+i.pid);
  let h=esc(bits.join(' · '));
  if(i.ctx && i.ctxmax){
    const p=Math.min(100,Math.round(i.ctx*100/i.ctxmax));
    h+=' · <span class="ctxmini '+(p>=80?'hot':(p>=60?'warm':''))
      +'" title="'+esc(tokShort(i.ctx)+' of '+tokShort(i.ctxmax)+' context')+'">'
      +'<i style="width:'+p+'%"></i></span>'+p+'%'+(p>=80?' · run /compact':'');
  }
  if(j && j.clipped) h+=' · transcript truncated';
  return h;
}
function closeTx(){
  // Чат теперь не шторка, а вторая панель — закрывать нечего, только снять выбор.
  TX_CUR=null;
  stopVis(false);
  if(TX_TIMER){ clearTimeout(TX_TIMER); TX_TIMER=null; }
  markSel(null);
  document.getElementById('drtitle').textContent='Pick a session on the left';
  document.getElementById('drmeta').textContent='';
  document.getElementById('dracts').innerHTML='';
  document.getElementById('drextraopts').innerHTML='';
  document.getElementById('drextra').innerHTML='';
  document.getElementById('drextra').classList.remove('on');
  document.getElementById('drdot').className='dot';
  document.getElementById('drbody').innerHTML=
    '<div id="mainempty">Sessions are listed on the left.<br>'
    +'Pick one — its conversation, context and controls appear here.</div>';
  try{ history.replaceState(null,'',location.pathname); }catch(e){}
}
function renderTx(j, short){
  const body=document.getElementById('drbody');
  // Ленты может не быть вовсе — тогда сюда приходит заглушка из txDraw().
  // Шапку она не трогает намеренно: имя и мета там уже стоят, а из пустого
  // объекта получились бы «undefined» и «files 0».
  if(!j.stub){
    TX_LAST=j;
    document.getElementById('drtitle').textContent=j.name;
    document.getElementById('drmeta').innerHTML = drMetaLine(short, j);
    const fb=document.getElementById('drfiles'), n=sessionFiles(j).length;
    if(IN_HQ){ fb.innerHTML=ci('paperclip')+'<b>'+n+'</b>'; fb.title=n+' files and artifacts of this session'; }
    else fb.textContent='files '+n;
    fb.style.display = n ? 'inline-block' : 'none';
  }
  pendReconcile(j, short);
  // не дёргать читателя: прокручиваем вниз, только если он и так внизу
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
  // Тот же порог, что и у бейджа WORKING в списке слева (BUSY_S на
  // сервере). Разные пороги давали разный ответ на один вопрос: лента
  // молчала, пока карточка ещё горела «работает».
  // Возраст записи — плохой признак работы: на долгом шаге транскрипт молчит
  // минутами, и низ ленты пустел ровно тогда, когда человек ждёт ответа
  // (30-08, 42bde158). Экран TUI знает правду, возраст оставлен запасным.
  const tuiNow = j.tui || null;
  const tuiKind = tuiNow ? tuiNow.kind : '';
  // Роль 'user' в ленте — именно сообщение человека: результаты инструментов
  // сервер сворачивает в 'step', так что за реплику их принять нельзя.
  TURN_TSE=null; TURN_SID=short; TURN_USE=j.turn||null;
  // Точка отсчёта — с сервера: он видит ленту целиком. Скан по видимой части
  // остаётся запасным путём на случай старого ответа без поля.
  if(TURN_USE && TURN_USE.tse) TURN_TSE=Number(TURN_USE.tse);
  else for(let k=(j.shown||[]).length-1;k>=0;k--){
    const it=j.shown[k];
    if(it.role==='user' && it.tse){ TURN_TSE=Number(it.tse); break; }
  }
  const turnAge = TURN_TSE ? (Date.now()/1000 - TURN_TSE) : j.age;
  const working = tuiKind === 'working' || tuiKind === 'compacting'
                  || (tuiKind !== 'idle' && j.age !== null && j.age !== undefined
                      && j.age < BUSY_JS);
  document.getElementById('drdot').classList.toggle('busy', working);
  stopVis(!j.stub && working);
  // Подряд идущие шаги сворачиваем в одну группу. В сводке — счётчик и
  // последний шаг, поэтому прогресс виден и не раскрывая: при опросе раз в
  // полторы секунды строка сама обновляется по мере работы сессии.
  // ⛔ Сборка разметки — и есть та самая тормозящая часть: markdown по каждому
  // сообщению, 172 КБ строки, ~140 мс основного потока, и всё это на КАЖДОМ
  // опросе, даже когда в ленте ничего не изменилось. Подпись по данным дешевле
  // самой сборки на три порядка, поэтому считаем её ДО, а не после.
  // ⛔ Строка «пишет ответ…» меняется каждые полторы секунды, и пока она жила
  // ВНУТРИ разметки ленты, работающая сессия пересобирала весь чат на каждом
  // опросе — из-за этого прокрутка и дёргалась ровно тогда, когда человек
  // читает свежий ответ. Она живёт отдельным узлом и обновляется на месте, а
  // сообщения остаются нетронутыми.
  const footHtml = pendFor(short).length
    ? pendingHtml('waiting for a reply', false, short)
    : (working ? pendingHtml(tuiText(tuiNow, turnAge, TURN_USE)[0] || 'writing the answer…', true) : '');
  permShow(j.stub ? null : (j.perm || null), short);

  const dataSig=txDataSig(j, short, working, tuiKind);
  if(body.dataset.dsig===dataSig && !body.dataset.stub){
    txFoot(body, footHtml, atBottom || txFollowing());
    return;
  }
  // Лента — список кусков, по одному верхнему узлу на кусок: так на опросе
  // заменяются только изменившиеся (обычно последний и новые), а не весь чат.
  const items=j.shown; const segs=[]; let i=0;
  if(j.more>0 || j.clipped){
    // на потолке (700) кнопке нечего подгружать — говорим словами
    const canMore = j.more>0 && !(j.limit && j.limit>=TX_FULL_LIMIT);
    segs.push('<div class="txmore">'
      +(canMore?'<button onclick="txMore()">Show earlier · '+j.more+' more</button>'
        :(j.more>0?'<span>'+j.more+' older items not shown</span>':''))
      +(j.clipped?'<span>the start of this transcript is past 40 MB and not shown</span>':'')
      +'</div>');
  }
  while(i<items.length){
    const m=items[i];
    if(m.role==='step'){
      const g=[], key=m.ts+'|'+(m.tool||'');
      while(i<items.length && items[i].role==='step'){ g.push(items[i]); i++; }
      const last=g[g.length-1];
      segs.push('<details class="steps" data-k="'+esc(key)+'"'+(TX_OPEN.has(key)?' open':'')
        +' ontoggle="stepToggle(this)"><summary>'+g.length+' '+stepWord(g.length)
        +' · last: '+esc(last.tool)+' '+esc(last.text)+'</summary>'
        +g.map(function(s){
            if(!s.path) return '<p class="step"><b>'+esc(s.tool)+'</b> '+esc(s.text)+'</p>';
            // ⛔ JSON.stringify даёт строку в ДВОЙНЫХ кавычках, а атрибут тоже
            // в двойных — без esc() он обрывался на первой же, и клик просто
            // ничего не делал. Подписываем именем файла: у Artifact в _LABEL_KEYS
            // первым идёт description, и ссылка выглядела как кусок описания.
            const nm=String(s.path).split('/').pop();
            return '<p class="step"><b>'+esc(s.tool)+'</b> '
              +'<span class="steplink" title="'+esc(s.path)+'"'
              +' onclick="openFile('+esc(JSON.stringify(s.path))+')">'
              +esc(nm)+'</span>'
              +(s.text && s.text!==nm ? ' <span class="stepnote">'+esc(s.text)+'</span>' : '')
              +'</p>';
          }).join('')+'</details>');
      continue;
    }
    if(m.role==='ask'){
      // Сколько вопросов уже закрыто: TUI спрашивает их по одному, и открытым
      // должен быть ровно один — следующий по порядку.
      let askActive=0;
      while(askActive<m.questions.length){
        const stx=ASK_LOCAL[short+':'+(m.tse||0)+':'+askActive];
        if(stx && stx.sent) askActive++; else break;
      }
      // Вопрос всегда развёрнут и никогда не прячется тумблером «шаги»:
      // это единственное, что останавливает сессию до ответа человека.
      segs.push('<div class="ask'+(m.pending?' pend':'')+'"><div class="qh">'
        +(m.pending?'CLAUDE ASKED · WAITING FOR YOU':'CLAUDE ASKED')+' · '
        +esc(whenTxt(m))+'</div>'
        +(m.pending && m.questions.length>1
            ? '<div class="qnote">'+m.questions.length
              +' questions — the TUI asks them one at a time, answer top-down</div>' : '')
        +m.questions.map(function(q, qi){
            // Ключ по отметке времени, а не по номеру в ленте: лента обрезается
            // сверху, и номер уехал бы вместе с ней.
            const key = short+':'+(m.tse||0)+':'+qi;
            const st = ASK_LOCAL[key] || {sel:[], sent:false};
            const state = !m.pending ? ''
              : (qi < askActive ? ' done' : (qi > askActive ? ' locked' : ''));
            let g = '<div class="qgroup'+state+'" data-k="'+esc(key)+'">'
              +'<div class="q">'+(q.header?'<span class="hd">'+esc(q.header)+'</span> ':'')
              +esc(q.q)+(q.multi?' <span class="hd">multi</span>':'')+'</div>';
            g += q.options.map(function(o,oi){
                  const n=oi+1, on=st.sel.indexOf(n)>=0;
                  const body='<b>'+esc(o.label)+'</b>'
                    +(o.desc?'<i>'+esc(o.desc)+'</i>':'');
                  // Кликать можно только пока вопрос открыт: у отвеченного
                  // меню в TUI уже нет, и цифра ушла бы в поле ввода текстом.
                  return m.pending
                    ? '<button class="o pick-opt'+(on?' chosen':'')+'"'
                        +' onclick="answerAsk(event,'+n+','+(q.multi?1:0)+')">'
                        +'<span class="onum">'+n+'</span>'+body+'</button>'
                    : '<div class="o">'+body+'</div>';
                }).join('');
            // Кнопка нужна ТОЛЬКО здесь: обычное меню выбирает по цифре сразу,
            // а вопрос с несколькими ответами ждёт Enter.
            if(m.pending && q.multi && qi===askActive){
              g += '<button class="go qconfirm" onclick="confirmAsk(event)"'
                +(st.sel.length?'':' disabled')+'>Send '
                +(st.sel.length?st.sel.length+' selected':'— pick at least one')
                +'</button>';
            }
            if(m.pending && qi<askActive) g += '<div class="qdone">answered</div>';
            return g+'</div>';
          }).join('')
        +'</div>');
      i++; continue;
    }
    if(m.role==='answer'){
      segs.push('<div class="msg answer"><div class="mtext">'
        +(m.pairs && m.pairs.length
            ? m.pairs.map(function(p){
                return '<div class="p"><b>'+esc(p.q)+'</b>'+esc(p.a)+'</div>';
              }).join('')
            : esc(m.text||''))
        +'</div>'+mfoot(m, 'you answered')+'</div>');
      i++; continue;
    }
    if(m.role==='artifact'){
      // Артефакт — результат работы, а не шаг: шаги свёрнуты в группу, и там
      // его никто не найдёт.
      segs.push('<div class="filechips">'
        +'<span class="fchip art" title="'+esc(m.path)+'"'
        +' onclick="openFile('+esc(JSON.stringify(m.path))+')">&#128196;'
        +'<b>'+esc(m.name||m.path)+'</b></span>'
        +(m.title?' <span class="stepnote">'+esc(m.title)+'</span>':'')+'</div>');
      i++; continue;
    }
    if(m.role==='stop'){
      segs.push('<div class="stopnote">&#9632; stopped by you · '+esc(whenTxt(m))+'</div>');
      i++; continue;
    }
    if(m.role==='think'){ segs.push('<p class="think">'+esc(m.text)+'</p>'); i++; continue; }
    segs.push('<div class="msg '+m.role+'"><div class="mtext" data-src="'+srcKey(m.text)+'">'+mdc(m.text)+'</div>'
      +chips(m.paths)+mfoot(m)+'</div>');
    i++;
  }
  // ⛔ Лента перерисовывалась ЦЕЛИКОМ на каждом опросе — раз в полторы секунды
  // у работающей сессии, — даже когда в ней не менялось ни байта. Пересборка
  // всех узлов на длинном чате и есть тот самый лаг, а `innerHTML=` попутно
  // сбрасывает прокрутку: человека, читавшего середину, отбрасывало наверх, и
  // выделенный текст пропадал из-под курсора. Перерисовываем, только когда
  // разметка действительно другая.
  const html=segs.join('');
  const sig=html.length+':'+txHash(html);
  if(body.dataset.sig!==sig && !selectionInside(body)){
    // ⛔ Мерить прокрутку надо ПРЯМО ПЕРЕД вставкой, а не в начале функции:
    // между ними успевает пройти и запрос, и сборка разметки, и человек за это
    // время докручивает вниз. Со старым замером его отбрасывало назад ровно
    // тогда, когда он мотал к концу ленты.
    const prevTop=body.scrollTop, prevH=body.scrollHeight;
    const nowBottom = prevH - prevTop - body.clientHeight < 60;
    // ⛔ Было body.innerHTML=html на каждое изменение: у работающей сессии это
    // пересборка всех узлов раз в несколько секунд, с разбором markdown по
    // каждому сообщению, — отсюда рывки и «деревянность». Теперь меняются
    // только куски, которых раньше не было; якорь чтения держит txPatch.
    const anchored = txPatch(body, segs, !nowBottom && !txFollowing());
    body.dataset.sig=sig;
    body.dataset.dsig=dataSig;
    body.dataset.stub = j.stub ? '1' : '';
    txFoot(body, footHtml, nowBottom || txFollowing());
    if(nowBottom) body.scrollTop=body.scrollHeight;
    else if(txFollowing()) txToBottom(body, true);
    // ⛔ Якорь — ВЕРХ, а не низ. Лента дописывается снизу, и «постоянное
    // расстояние до низа» на каждом дописанном куске уводило текст вверх
    // из-под глаз читателя: ровно то, на что жалуются, когда сессия работает
    // и ответ печатается. Читаемый кусок остаётся на месте (txPatch держит
    // его по экранной позиции), а новое просто копится ниже.
    else if(!anchored) body.scrollTop=prevTop;
  }
  // Открытый вопрос виден по ленте: у него role==='ask' и pending. Считаем
  // здесь же, чтобы не заводить второй источник правды.
  const asking=(j.shown||[]).some(function(m){ return m.role==='ask' && m.pending; });
  const warn=document.getElementById('askwarn');
  if(warn){
    warn.classList.toggle('on', asking);
    if(asking) warn.textContent='The session is waiting on a question. '
      +'Sending text will dismiss it and go through as a normal message.';
  }
  applyFilters();
  // Вниз доводим того, кто и так стоял внизу, и того, кто только что отправил:
  // фильтры меняют высоту уже после вставки.
  if(atBottom) body.scrollTop=body.scrollHeight;
  else if(txFollowing()) txToBottom(body, true);
}

// Дешёвая подпись разметки: сравнивать строки целиком на каждом опросе дорого,
// а 32-битного хеша с длиной хватает, чтобы отличить изменившуюся ленту.
// Подпись ЛЕНТЫ по данным, а не по готовой разметке: сколько записей, чем
// кончается последняя, что показывает TUI и что ждёт отправки. Всё, от чего
// разметка зависит, и ничего, что стоило бы дорого посчитать.
/**
 * Обновить подвал ленты, не трогая сообщения.
 *
 * Он либо есть, либо его нет; когда есть — меняется только его содержимое.
 * Прокрутку доводим до низа лишь тому, кто и так стоял внизу: подвал меняет
 * высоту ленты, и без этого читающего конец отцепляло бы от него.
 */
function txFoot(body, html, keepBottom){
  let foot=body.querySelector(':scope > .txfoot');
  if(!html){ if(foot) foot.remove(); return; }
  if(!foot){
    foot=document.createElement('div');
    foot.className='txfoot';
    body.appendChild(foot);
  }
  if(foot.innerHTML!==html) foot.innerHTML=html;
  if(keepBottom) txToBottom(body, txFollowing());
}

function txDataSig(j, short, working, tuiKind){
  const it=j.shown||[], last=it[it.length-1]||{}, prev=it[it.length-2]||{};
  // working и tuiKind сюда НЕ входят: они рисуют только подвал, который
  // обновляется отдельно и ленту не трогает.
  return [it.length, last.role||'', last.tse||0, (last.text||'').length,
          prev.tse||0, (prev.text||'').length,
          TX_OPEN.size, j.stub?1:0,
          (j.artifacts||[]).length, FILTER_STATE()].join('|');
}
// Состояние фильтров входит в подпись: иначе переключение «шаги» не
// перерисовывало бы ленту, пока в ней ничего не менялось.
function FILTER_STATE(){
  const b=document.getElementById('tgSteps');
  return b ? (b.checked?'1':'0') : '';
}

// Когда человек последний раз двигал прокрутку ленты.
let TX_SCROLL_AT=0;

/**
 * До какого момента лента обязана держаться низа.
 *
 * Своё отправленное сообщение человек хочет увидеть — это единственный случай,
 * когда прокрутку двигаем за него намеренно. Одного рывка вниз в момент
 * отправки мало: следом идут опросы (1.2 / 4 / 9 с), приходит настоящая
 * реплика вместо пузыря, появляется строка «пишет ответ…», и каждая из этих
 * перерисовок меняет высоту. Поэтому держим намерение окном, а не разовым
 * действием, и снимаем его, как только человек сам тронул колесо.
 */
let TX_FOLLOW_UNTIL=0;
function txFollow(ms){ TX_FOLLOW_UNTIL=Date.now()+(ms||9000); }
function txFollowing(){ return Date.now()<TX_FOLLOW_UNTIL; }
/** Довести до низа: плавно, когда ехать далеко, и мгновенно на мелких правках. */
function txToBottom(body, smooth){
  const dist=body.scrollHeight-body.scrollTop-body.clientHeight;
  if(dist<=1) return;
  if(smooth && dist>200 && !window.matchMedia('(prefers-reduced-motion: reduce)').matches){
    try{ body.scrollTo({top:body.scrollHeight, behavior:'smooth'}); return; }catch(e){}
  }
  body.scrollTop=body.scrollHeight;
}
document.addEventListener('scroll', function(ev){
  if(!ev.target || ev.target.id!=='drbody') return;
  TX_SCROLL_AT=Date.now();
  // Прокрутка колесом или тачпадом отменяет «веди к новому»: дальше человек
  // сам решает, куда смотреть. Наши собственные доводки до низа сюда не
  // попадают — они идут при уже истёкшем окне или ставят его заново.
  if(txFollowing() && ev.target.scrollHeight-ev.target.scrollTop-ev.target.clientHeight>140)
    TX_FOLLOW_UNTIL=0;
}, true);

function txHash(str){
  let h=5381;
  for(let i=0;i<str.length;i++) h=((h*33) ^ str.charCodeAt(i)) & 0xffffffff;
  return h.toString(36);
}

// Разбор markdown — самая дорогая часть сборки ленты, а тексты сообщений не
// меняются. Один раз на текст, а не на каждый опрос.
const MD_CACHE=new Map();
function mdc(s){
  const k=String(s==null?'':s);
  let h=MD_CACHE.get(k);
  if(h===undefined){
    h=md(k); MD_CACHE.set(k, h);
    if(MD_CACHE.size>3000) MD_CACHE.delete(MD_CACHE.keys().next().value);
  }
  return h;
}

/**
 * Привести ленту к списку кусков, трогая только разницу.
 *
 * Каждый кусок — один верхний узел с ключом data-h (длина и хеш его
 * разметки). Узлы с тем же ключом остаются как есть — с раскрытыми группами,
 * загруженными картинками и местом на экране; лишние уходят, новые встают
 * на свои места. Подвал (.txfoot) не трогается: он живёт своей жизнью.
 *
 * keepAnchor — читатель не внизу: запоминаем первый видимый узел, который
 * переживёт правку, и возвращаем его на ту же экранную позицию. Когда сверху
 * уходят старые куски (окно ленты сдвинулось) или приходят более ранние
 * (Show earlier), текст перед глазами не прыгает. Возвращает true, если
 * якорь сработал.
 */
function txPatch(body, segs, keepAnchor){
  const foot=body.querySelector(':scope > .txfoot');
  const keys=segs.map(function(h){ return h.length+':'+txHash(h); });
  const pool=new Map(), olds=[];
  Array.prototype.forEach.call(body.children, function(el){
    if(el===foot) return;
    olds.push(el);
    const k=el.dataset ? el.dataset.h : '';
    if(!k) return;
    const l=pool.get(k); if(l) l.push(el); else pool.set(k, [el]);
  });
  const reuse=keys.map(function(k){
    const l=pool.get(k); return (l && l.length) ? l.shift() : null; });
  const kept=new Set(reuse.filter(Boolean));
  let anchor=null, anchorTop=0;
  if(keepAnchor){
    const top=body.getBoundingClientRect().top;
    for(let n=0;n<olds.length;n++){
      if(!kept.has(olds[n])) continue;
      const r=olds[n].getBoundingClientRect();
      if(r.bottom>top+1){ anchor=olds[n]; anchorTop=r.top; break; }
    }
  }
  olds.forEach(function(el){ if(!kept.has(el)) el.remove(); });
  let cur=body.firstElementChild, pend=[];
  function flush(){
    if(!pend.length) return;
    // ⛔ Не <template>: его узлы живут в отдельном, инертном документе и при
    // вставке переезжают в этот. WebKit после такого переезда оставлял ленту
    // неотрисованной до первой прокрутки или щелчка — «белый экран» при
    // заходе в таб (26-09). Разбираем в обычном div этого же документа.
    const box=document.createElement('div');
    box.innerHTML=pend.map(function(x){ return x[0]; }).join('');
    const kids=box.children;
    // кусок, давший не один узел, останется без ключа и пересоберётся
    // следующим проходом — лучше лишняя сборка, чем чужой ключ
    if(kids.length===pend.length){
      for(let n=0;n<kids.length;n++) kids[n].dataset.h=pend[n][1];
    }
    const frag=document.createDocumentFragment();
    while(box.firstChild) frag.appendChild(box.firstChild);
    body.insertBefore(frag, cur);
    pend=[];
  }
  for(let n=0;n<segs.length;n++){
    const el=reuse[n];
    if(!el){ pend.push([segs[n], keys[n]]); continue; }
    flush();
    if(el===cur) cur=el.nextElementSibling;
    else body.insertBefore(el, cur);
  }
  flush();
  if(anchor && anchor.isConnected){
    const d=anchor.getBoundingClientRect().top-anchorTop;
    if(d) body.scrollTop+=d;
    return true;
  }
  return false;
}

// ---- ленты по сессиям: кэш, опрос «что изменилось», подгрузка заранее ----
// ⛔ Открытие чата стирало ленту и ждало /transcript целиком — до 480 КБ через
// 70-250 мс до основная машинаа, и каждое открытие начиналось с пустого экрана. Теперь
// последняя виденная лента каждой сессии лежит здесь: открытие рисует её
// сразу, а сеть только догоняет разницу. Опрос отдаёт подпись ленты, и если
// она не поменялась, сервер отвечает тремястами байтами вместо всей ленты.
const TX_CACHE={}, TX_INFLIGHT={}, TX_LIMIT_OF={};
const TX_OPEN_LIMIT=200, TX_FULL_LIMIT=700;
function txCachePut(id, j){
  delete TX_CACHE[id];
  TX_CACHE[id]={j:j, at:Date.now()};
  const ks=Object.keys(TX_CACHE);
  if(ks.length>24) delete TX_CACHE[ks[0]];
}
function txFetch(id){
  if(TX_INFLIGHT[id]) return TX_INFLIGHT[id];
  const base=TX_CACHE[id];
  const lim=TX_LIMIT_OF[id]||TX_OPEN_LIMIT;
  const useSig=!!(base && base.j.sig && base.j.limit===lim);
  const url='/transcript?uuid='+encodeURIComponent(id)+'&limit='+lim
    +(useSig?'&sig='+encodeURIComponent(base.j.sig):'')+dq('&');
  const p=fetchT(url, {}, 20000).then(function(r){ return r.json(); }).then(function(j){
    if(j.ok && j.same){
      // живые поля — из ответа, сама лента — из кэша
      const full=Object.assign({}, base.j, j);
      delete full.same;
      txCachePut(id, full);
      return full;
    }
    if(j.ok && j.shown) txCachePut(id, j);
    return j;
  });
  TX_INFLIGHT[id]=p;
  const done=function(){ if(TX_INFLIGHT[id]===p) delete TX_INFLIGHT[id]; };
  p.then(done, done);
  return p;
}
// ⛔ 26-09: при заходе в таб лента бывала в DOM, но на экране белое поле,
// пока не прокрутишь или не щёлкнешь. WebKit не перерисовывает прокручиваемый
// блок, у которого сменили содержимое и scrollTop в одном кадре. Один
// принудительный кадр (слой пересоздаётся) и доводка вниз уже после раскладки.
function txRepaint(body, toBottom, short){
  // ⛔ 27-09: переключения слоя (transform) не хватило — белое поле
  // оставалось до ручной прокрутки. Прокрутка же его и лечит, а присвоить
  // scrollTop, равный текущему, — это не прокрутка: браузер ничего не делает.
  // Поэтому настоящая прокрутка на пиксель и обратно, после раскладки.
  requestAnimationFrame(function(){
    body.style.transform='translateZ(0)';
    const max = Math.max(0, body.scrollHeight - body.clientHeight);
    const want = toBottom ? max : Math.min(body.scrollTop, max);
    if(max > 4) body.scrollTop = want > 2 ? want - 2 : want + 2;   // a real 2 px scroll
    requestAnimationFrame(function(){
      body.style.transform='';
      body.scrollTop = toBottom ? body.scrollHeight : want;
      setTimeout(function(){
        // картинки догрузились и толкнули низ — доводим, но только того, кто
        // так и стоит у низа: ушедшего вверх читателя не дёргаем
        const gap=body.scrollHeight-body.scrollTop-body.clientHeight;
        if(toBottom && TX_CUR===short && gap<150) body.scrollTop=body.scrollHeight;
        txDiag(body, short);
      }, 900);
    });
  });
}
// Разовая сводка для диагностики белого поля: браузер и что лежит в видимой
// части ленты через секунду после открытия. Пишет сервер в rc-panel-diag.log.
function txDiag(body, short){
  if(!short || TX_CUR!==short) return;
  try{
    const r=body.getBoundingClientRect();
    let vis=0;
    for(const el of body.children){
      const b=el.getBoundingClientRect();
      if(b.bottom>r.top+1 && b.top<r.bottom-1 && b.height>0) vis++;
    }
    const d={id:short, ua:navigator.userAgent, dpr:window.devicePixelRatio,
      st:Math.round(body.scrollTop), sh:body.scrollHeight, ch:body.clientHeight,
      kids:body.children.length, visible:vis, hidden:document.hidden,
      w:Math.round(r.width), h:Math.round(r.height)};
    fetch('/diag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  }catch(e){}
}
// Заранее, тихо: ленты недавно живших сессий — чтобы первый заход в таб
// рисовался из кэша, а не с белого поля. По одной, с паузами, и только пока
// вкладка на экране.
function txWarm(){
  if(typeof ROWS==='undefined') return;
  const ids=Object.keys(ROWS).filter(function(k){ return ROWS[k].live; })
    .sort(function(a,b){ return (ROWS[b].busy?1:0)-(ROWS[a].busy?1:0) || (ROWS[b].ts||0)-(ROWS[a].ts||0); })
    .slice(0, 8);
  let i=0;
  (function next(){
    if(i>=ids.length || document.hidden) return;
    const id=ids[i++];
    if(id!==TX_CUR) txPrefetch(id);
    setTimeout(next, 500);
  })();
}
setTimeout(txWarm, 2500);
function txMore(){
  if(!TX_CUR) return;
  TX_LIMIT_OF[TX_CUR]=TX_FULL_LIMIT;
  if(TX_TIMER) clearTimeout(TX_TIMER);
  pollTx();
}
// Наведение на строку — почти всегда прелюдия к щелчку: пока рука доезжает,
// лента уже в кэше, и чат открывается без ожидания.
let TX_PF_T=null;
function txPrefetch(id){
  if(!id || id===TX_CUR || TX_INFLIGHT[id]) return;
  const c=TX_CACHE[id];
  if(c && Date.now()-c.at<20000) return;
  txFetch(id).catch(function(){});
}
document.addEventListener('mouseover', function(ev){
  const li=ev.target && ev.target.closest ? ev.target.closest('#side .row[data-id]') : null;
  if(!li) return;
  if(TX_PF_T) clearTimeout(TX_PF_T);
  TX_PF_T=setTimeout(function(){ txPrefetch(li.dataset.id); }, 120);
});
document.addEventListener('touchstart', function(ev){
  const li=ev.target && ev.target.closest ? ev.target.closest('#side .row[data-id]') : null;
  if(li) txPrefetch(li.dataset.id);
}, {passive:true});

// Пересборка узлов убивает выделение: пока человек что-то выделил в ленте,
// не трогаем её вовсе — он либо копирует, либо читает.
function selectionInside(el){
  const s=window.getSelection && window.getSelection();
  if(!s || s.isCollapsed || !s.anchorNode) return false;
  return el.contains(s.anchorNode) || el.contains(s.focusNode);
}
function headFill(short){
  const info=(typeof ROWS!=='undefined') ? ROWS[short] : null;
  document.getElementById('drtitle').textContent = info ? info.name : short;
  document.getElementById('drtitle').title = info ? info.name : short;
  document.getElementById('dracts').innerHTML = info ? info.acts : '';
  document.getElementById('drextraopts').innerHTML = info ? info.opts : '';
  optApply(short);
  document.getElementById('drextra').innerHTML = info ? info.extra : '';
  document.getElementById('drextra').classList.toggle('on', !!(info && info.extra));
  menuFill(short, info ? (info.menu||[]) : []);
  const dot=document.getElementById('drdot');
  dot.classList.toggle('live', !!(info&&info.live));
  dot.classList.toggle('busy', !!(info&&info.busy));
}
// HQ: a session launched after this page loaded is not in ROWS yet. Its chat
// opens at once and its header comes from /api/row, instead of a page reload
// (10-16: «после старта сессии жду ещё секунд 20 с пустым экраном»).
async function rcAdopt(short){
  if(rcHas(short)) return true;
  for(let n=0;n<4;n++){
    try{
      const r=await fetchT('/api/row?id='+encodeURIComponent(short)+dq('&'), {cache:'no-store'}, 8000);
      const j=await r.json();
      if(j.ok && j.row){ ROWS[short]=j.row; if(TX_CUR===short) headFill(short); return true; }
    }catch(e){}
    await sleepMs(1500);
  }
  return false;
}
// HQ: the first order of a session just launched is on screen at once, with its
// status, until the transcript has it
function hqFirst(short, text){
  if(!text || PEND.some(function(p){ return p.sid===short && p.first; })) return;
  const e=pendAdd(text, text, short, []);
  e.ok=true; e.first=true; e.status='';
  if(TX_CUR===short){ txDraw(short); txToBottom(document.getElementById('drbody'), true); }
}
async function openTx(short){
  const body=document.getElementById('drbody');
  draftSave();          // ⛔ строго ДО смены DRAFT/TX_CUR — ключ считается по ним
  // pendClear() здесь БЫЛО и стирало ещё не доехавшие реплики: они
  // теперь помечены сессией и просто не показываются в чужой ленте.
  DRAFT=false; TX_LAST=null;
  document.getElementById('drtext').placeholder=TX_PH;
  TX_CUR=short;
  gameEmit('open', {sid:short});
  draftLoad(short);     // вложения тоже свои: их пути ведут в папку этой сессии
  // Выбор живёт в адресе: действия перезагружают страницу целиком, и без этого
  // чат схлопывался бы в пустой на каждое нажатие кнопки.
  try{ history.replaceState(null,'','#'+short); }catch(e){}
  const li=markSel(short);
  if(li) li.scrollIntoView({block:'nearest'});
  headFill(short);
  if(TX_TIMER){ clearTimeout(TX_TIMER); TX_TIMER=null; }
  body.innerHTML='';
  body.dataset.sig=''; body.dataset.dsig=''; body.dataset.stub='';
  stopVis(false);
  const cached=TX_CACHE[short];
  if(cached){
    renderTx(cached.j, short);
    body.scrollTop=body.scrollHeight;
    txRepaint(body, true, short);
  } else {
    // не белое поле, а слово: ждать видно, что ждём
    document.getElementById('drmeta').textContent='… reading transcript';
    body.innerHTML='<div id="mainempty">loading the conversation…</div>';
  }
  try{
    const j=await txFetch(short);
    // ⛔ Пока ответ ехал, человек мог открыть другую сессию: поздний ответ
    // рисовал чужую ленту под чужим заголовком.
    if(TX_CUR!==short) return;
    // ⛔ Оба выхода по ошибке уходили БЕЗ scheduleTx — и сессия оставалась
    // без опроса навсегда: одна неудачная выдача /transcript (мини под
    // нагрузкой отвечает не всегда) навсегда превращала чат в картинку.
    // Отправить туда было можно, увидеть отправленное — нет.
    if(!j.ok){
      document.getElementById('drmeta').textContent='✗ '+j.msg;
      if(TX_CUR===short) scheduleTx(null);
      return;
    }
    renderTx(j, short);
    // the latest exchange is the bottom, not the top; a cached open already
    // put the reader there, and renderTx keeps whoever is at the bottom there
    if(!cached) body.scrollTop=body.scrollHeight;
    txRepaint(body, !cached, short);
    scheduleTx(j);
  }catch(e){
    if(TX_CUR!==short) return;
    document.getElementById('drmeta').textContent='✗ '+e;
    scheduleTx(null);
  }
}
// Пока ответ пишется — опрашиваем часто, в покое реже. Стриминга тут нет:
// транскрипт пополняется целыми сообщениями (замерено), так что чаще, чем
// раз в полторы секунды, спрашивать нечего.
function scheduleTx(j){
  if(TX_TIMER) clearTimeout(TX_TIMER);
  const hot = j && j.age !== null && j.age !== undefined && j.age < 60;
  TX_TIMER=setTimeout(pollTx, hot ? 1500 : 5000);
}
async function pollTx(){
  const want=TX_CUR;
  if(!want) return;
  try{
    const j=await txFetch(want);
    // ⛔ Пока ответ ехал, могли уйти в черновик или в другую сессию. Без
    // этой сверки запоздавший опрос рисовал СТАРУЮ ленту поверх новой, и
    // «New session» с первого нажатия выглядела как несработавшая: экран
    // возвращался к прежней сессии, и помогало только второе нажатие.
    if(TX_CUR!==want) return;
    if(j.ok){ renderTx(j, want); scheduleTx(j); } else scheduleTx(null);
  }catch(e){ if(TX_CUR===want) scheduleTx(null); }
}
// Вложения живут отдельным списком, а не текстом в поле: сырой путь в строке
// ввода — это не вложение, его нельзя ни убрать, ни разглядеть, и он мешает
// писать сам вопрос. Путь подставляется к сообщению уже при отправке.
let ATTACH=[];
// Черновик у КАЖДОЙ сессии свой: набранное принадлежит той переписке, в
// которой это набирали. Раньше поле ввода было общим, и текст, начатый для
// одной сессии, уезжал в другую при переключении.
const DRAFTS={};
function draftKey(){ return DRAFT ? '' : (TX_CUR || ''); }
function draftSave(){
  const ta=document.getElementById('drtext');
  if(!ta) return;
  const k=draftKey(), txt=ta.value;
  if(txt.trim() || ATTACH.length) DRAFTS[k]={text:txt, attach:ATTACH.slice()};
  else delete DRAFTS[k];
  // Текст переживает и перезагрузку страницы: групповые действия перезагружают
  // её целиком, и набранное иначе пропадало бы вместе с ней. Вложения храним
  // только в памяти — у превью картинок ссылки на объекты, перезагрузку они
  // всё равно не переживают.
  try{
    if(txt.trim()) localStorage.setItem('draft:'+k, txt);
    else localStorage.removeItem('draft:'+k);
  }catch(e){}
}
function draftLoad(k){
  let d=DRAFTS[k];
  if(!d){
    let saved='';
    try{ saved=localStorage.getItem('draft:'+k)||''; }catch(e){}
    d={text:saved, attach:[]};
  }
  const ta=document.getElementById('drtext');
  ta.value=d.text; taGrow(ta);
  ATTACH=d.attach.slice(); renderAttach();
}
function renderAttach(){
  const box=document.getElementById('attachbar');
  box.innerHTML=ATTACH.map(function(a,i){
    const kb=a.size ? (a.size>=1048576 ? (a.size/1048576).toFixed(1)+'M'
                                       : Math.round(a.size/1024)+'K') : '';
    const tip=esc(a.name+(kb?' · '+kb:'')+(a.pending?' · uploading':''));
    const prog = a.pending
      ? '<em class="pct">'+(a.retry?'retry '+(a.retry+1)+' · ':'')+(a.pct||0)+'%</em>'
        +'<i class="bar"><u style="width:'+(a.pct||0)+'%"></u></i>' : '';
    const del = a.pending ? '' : '<button onclick="attachDrop('+i+')" title="Remove">'+(IN_HQ?ci('close'):'&times;')+'</button>';
    // A picture IS the attachment: a tile with the image and nothing else.
    if(a.url) return '<span class="att img'+(a.pending?' up':'')+'" title="'+tip+'">'
      +'<img src="'+esc(a.url)+'" alt="">'+prog+del+'</span>';
    return '<span class="att file'+(a.pending?' up':'')+'" title="'+tip+'">'
      +'<i class="ic">'+(IN_HQ?ci(attIcon(a.name)):'&#128196;')+'</i>'
      +'<b>'+esc(attShort(a.name))+'</b>'
      +(a.pending ? prog : (kb?'<em class="pct">'+kb+'</em>':''))+del+'</span>';
  }).join('');
  box.classList.toggle('on', ATTACH.length>0);
}
function attachDrop(i){
  const a=ATTACH[i];
  if(a && a.url) try{ URL.revokeObjectURL(a.url); }catch(e){}
  ATTACH.splice(i,1);
  renderAttach();
}
function attachClear(){
  ATTACH.forEach(function(a){ if(a.url) try{ URL.revokeObjectURL(a.url); }catch(e){} });
  ATTACH=[]; renderAttach();
}
// ⛔ 10-16: on the lossy link a 2 MB zip hung with no end and no retry, the owner
// dropped the same files again and again, and one copy landed cut at 1 MiB.
// Now a try that makes no progress for 25 s is dropped and retried (4 tries,
// same key, so a try that did land is not saved twice), the server rejects a
// cut body, and HQ hears every step of it to draw the transfer.
const UP_STALL_MS=25000, UP_REPLY_MS=45000, UP_TRIES=4;
function upEmit(item, state, msg){
  gameEmit('upload', {sid:item.sid||'', key:item.key, name:item.name, size:item.size,
                      pct:item.pct||0, state:state, msg:msg||'', retry:item.retry||0,
                      img:item.url||''});
}
function upXhr(f, item){
  return new Promise(function(resolve, reject){
    const x=new XMLHttpRequest();
    let last=Date.now(), sent=false, why='';
    const watch=setInterval(function(){
      const quiet=Date.now()-last;
      if((!sent && quiet>UP_STALL_MS) || (sent && quiet>UP_REPLY_MS)){
        why = sent ? 'no answer after the upload' : 'stalled: no progress for 25 s';
        try{ x.abort(); }catch(e){}
      }
    }, 2000);
    x.open('POST', '/upload?uuid='+encodeURIComponent(item.sid||'')
      +'&name='+encodeURIComponent(f.name||'file')
      +'&size='+(f.size||0)+'&key='+encodeURIComponent(item.key));
    x.upload.onprogress=function(e){
      last=Date.now();
      if(!e.lengthComputable) return;
      if(e.loaded>=e.total) sent=true;
      const pct=Math.min(99, Math.round(e.loaded*100/e.total));
      if(pct!==item.pct){ item.pct=pct; renderAttach(); upEmit(item, 'progress'); }
    };
    x.upload.onload=function(){ sent=true; last=Date.now(); };
    x.onload=function(){
      clearInterval(watch);
      try{ resolve(JSON.parse(x.responseText)); }
      catch(err){ reject(new Error('bad answer from the panel')); }
    };
    x.onerror=function(){ clearInterval(watch); reject(new Error('connection lost')); };
    x.onabort=function(){ clearInterval(watch); reject(new Error(why || 'cancelled')); };
    x.send(f);
  });
}
async function uploadOne(f){
  // the same file dropped twice on the same chat is one attachment
  const twin=ATTACH.filter(function(a){
    return a.name===(f.name||'file') && a.size===(f.size||0); })[0];
  if(twin){ flash('… '+twin.name+' is already attached'); return twin.path||''; }
  // 11-17: the server refuses an oversized file before it reads the body, and
  // the browser reports that as «connection lost». Say the real reason here.
  // UP_MAX_MB mirrors UPLOAD_MAX in rc_control.py.
  const UP_MAX_MB=200;
  if((f.size||0) > UP_MAX_MB*1048576)
    throw new Error('file is '+Math.round((f.size||0)/1048576)+' MB, the limit is '+UP_MAX_MB+' MB');
  // ⛔ Раньше карточка файла появлялась ТОЛЬКО после ответа сервера: на
  // скриншоте в пару мегабайт это секунды пустоты, а потом картинка возникала
  // рывком. Кладём её сразу — превью берём из самого File, поэтому показать
  // есть что ещё до первого байта на проводе.
  const url=(f.type && f.type.indexOf('image/')===0) ? URL.createObjectURL(f) : '';
  const item={name:f.name||'file', path:'', url:url, pending:true, pct:0,
              size:f.size||0, sid:TX_CUR||'', retry:0,
              key:Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,9)};
  ATTACH.push(item);
  renderAttach();
  upEmit(item, 'start');
  let last=null;
  for(let t=1; t<=UP_TRIES; t++){
    try{
      const j=await upXhr(f, item);
      if(!j.ok) throw new Error(j.msg||'upload failed');
      item.path=j.path; item.pending=false; item.pct=100;
      renderAttach();
      upEmit(item, 'done');
      return j.path;
    }catch(e){
      last=e;
      if(/too large|empty file/.test(String(e.message)) || ATTACH.indexOf(item)<0) break;
      if(t<UP_TRIES){
        item.retry=t; item.pct=0; renderAttach();
        upEmit(item, 'retry', e.message);
        await new Promise(function(r){ setTimeout(r, Math.min(8000, 1500*t)); });
      }
    }
  }
  // Неудачную карточку убираем: висящий файл без пути нельзя ни отправить,
  // ни понять.
  const i=ATTACH.indexOf(item);
  if(i>=0){ ATTACH.splice(i,1); if(url) try{ URL.revokeObjectURL(url); }catch(_){} }
  renderAttach();
  upEmit(item, 'fail', last ? last.message : 'failed');
  throw (last || new Error('upload failed'));
}
async function uploadMany(files){
  if(!files || !files.length) return;
  // В черновике сессии ещё нет, и TX_CUR пуст — это нормально: файл
  // ляжет в общую папку, а путь уйдёт вместе с первым промптом.
  if(!TX_CUR && !DRAFT){ flash('✗ pick a session first'); return; }
  const list=Array.prototype.slice.call(files);
  flash('… uploading '+list.length+' file'+(list.length>1?'s':''));
  let done=0;
  for(const f of list){
    try{ await uploadOne(f); done++; }
    catch(e){ flash('✗ '+f.name+': '+e.message); }
  }
  if(done) flash('✓ attached '+done+' file'+(done>1?'s':''));
  document.getElementById('drtext').focus();
}
async function uploadFile(inp){
  await uploadMany(inp.files);
  inp.value='';
}
// Перетаскивание файла в чат. Отличаем от перетаскивания СЕССИИ между группами:
// у файлового drag в dataTransfer.types есть 'Files', у нашего — нет.
function isFileDrag(ev){
  const t=ev.dataTransfer && ev.dataTransfer.types;
  return !!(t && Array.prototype.indexOf.call(t, 'Files') >= 0);
}
// ⛔ 10-16: the overlay was kept by counting dragenter/dragleave, and Safari
// does not pair them (iframes, text nodes, a drag that leaves the window), so
// «Drop it here» could stay over the chat for good. dragover fires every
// ~50 ms while a file hovers: the overlay lives while those keep coming.
let DROP_T=null, DROP_EMIT=0;
function dropShow(){
  document.body.classList.add('dropping');
  if(DROP_T) clearTimeout(DROP_T);
  DROP_T=setTimeout(dropHide, 400);
}
function dropHide(){ document.body.classList.remove('dropping'); DROP_T=null; }
function dropInit(){
  const main=document.getElementById('main');
  if(!main) return;
  function over(ev){
    if(!isFileDrag(ev)) return;
    ev.preventDefault();
    try{ ev.dataTransfer.dropEffect='copy'; }catch(e){}
    // In HQ the game page draws the drop targets (the agents) over everything;
    // the chat only tells it a file is being dragged.
    if(IN_HQ){
      if(Date.now()-DROP_EMIT>120){ DROP_EMIT=Date.now(); gameEmit('drag', {x:ev.clientX, y:ev.clientY}); }
      return;
    }
    dropShow();
  }
  document.addEventListener('dragenter', over);
  document.addEventListener('dragover', over);
  document.addEventListener('drop', function(ev){
    if(!isFileDrag(ev)) return;
    ev.preventDefault();
    dropHide();
    uploadMany(ev.dataTransfer.files);
  });
  // Картинку чаще всего просто вставляют из буфера — тот же путь.
  document.addEventListener('paste', function(ev){
    const items=(ev.clipboardData && ev.clipboardData.files) || null;
    if(!items || !items.length) return;
    const t=ev.target;
    if(t && (t.id==='drtext' || t.id==='newtext' || document.getElementById('main').contains(t))){
      ev.preventDefault();
      uploadMany(items);
    }
  });
}
dropInit();
/* Поле растёт под текст, как в самом Claude. Высоту обязательно
   сбрасываем в auto перед замером: без сброса scrollHeight помнит прежнюю
   высоту, и поле умеет только расти — при удалении строк не сжимается. */
function taGrow(ta){
  if(!ta) return;
  ta.style.height='auto';
  ta.style.height=Math.min(ta.scrollHeight,
    Math.round(window.innerHeight*0.45))+'px';
}
document.addEventListener('input', function(ev){
  if(ev.target && ev.target.id==='drtext') taGrow(ev.target);
});
window.addEventListener('resize', function(){
  taGrow(document.getElementById('drtext'));
});

// ■ Стоп — Esc в TUI, как в терминале: снимает текущий шаг, сессия остаётся
// живой и ждёт следующей реплики. Кнопка видна, только пока сессия работает.
function stopVis(on){
  const b=document.getElementById('drstop');
  if(b) b.style.display = (on && TX_CUR && !DRAFT) ? '' : 'none';
}
async function stopTurn(){
  const id=TX_CUR; if(!id) return;
  const b=document.getElementById('drstop');
  b.disabled=true; b.classList.add('saving');
  const ok=await act('interrupt', {id:id}, 'stopping the current step of '+id, true);
  b.disabled=false; b.classList.remove('saving');
  if(ok && TX_CUR===id) stopVis(false);
  if(TX_CUR===id){ setTimeout(pollTx, 500); setTimeout(pollTx, 2500); }
  setTimeout(pulse, 1200);
}
// ⛔ 10-14: Enter while a screenshot was still uploading sent «текст ""»: the
// card had no path yet, the message went out without the picture and the
// upload landed nowhere. Sending now waits for every pending upload; a failed
// one is already dropped from ATTACH by uploadOne, so what is left has a path.
let ATTACH_WAIT=false;
function attachPending(){ return ATTACH.some(function(a){ return a.pending; }); }
async function attachReady(){
  if(!attachPending()) return true;
  flash('… waiting for the upload to finish, then sending');
  const t0=Date.now();
  while(attachPending()){
    if(Date.now()-t0>180000){ flash('✗ the upload is taking too long — try again'); return false; }
    await new Promise(function(r){ setTimeout(r, 250); });
  }
  return true;
}
async function sendTx(){
  if(ATTACH_WAIT) return;
  if(attachPending()){
    ATTACH_WAIT=true;
    let ok=false;
    try{ ok=await attachReady(); } finally { ATTACH_WAIT=false; }
    if(!ok) return;
  }
  const ta=document.getElementById('drtext'), t=ta.value.trim();
  if(DRAFT){ return createFromDraft(t); }
  if(!TX_CUR) return;
  const files=ATTACH.filter(function(a){ return a.path; })
    .map(function(a){ return '"'+a.path+'"'; }).join(' ');
  if(!t && !files) return;
  const full=(t && files) ? (t+' '+files) : (t || files);
  // Показываем пузырю сами файлы, а не строку «📎 имя»: они уже лежат на
  // мини, и chips() покажет снимок ровно так же, как в настоящей реплике,
  // — переход от пузыря к ленте перестаёт быть заметен.
  const apaths=ATTACH.filter(function(a){ return a.path; }).map(function(a){ return a.path; });
  const shown=t;
  const sent=TX_CUR;
  // Рисуем сразу и чистим поле: ждать подтверждения доставки, глядя на
  // застывший экран, — ровно то, что раздражало.
  const ent=pendAdd(shown, full, sent, apaths);
  ta.value=''; taGrow(ta);
  draftSave();          // ушло — снимаем черновик, иначе он вернётся при входе
  attachClear();
  // Своё сообщение человек должен увидеть: ведём к нему и держимся низа,
  // пока приходят подтверждение, настоящая реплика и строка «пишет ответ…».
  txFollow();
  txDraw(sent);
  txToBottom(document.getElementById('drbody'), true);
  await sendText(full, sent, t, ent);
  ta.focus();
}
function sendDiag(id, err){
  try{ fetch('/diag',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id, kind:'send', err:String(err).slice(0,300)})}); }catch(_){}
}
// Отправка как таковая — общая для кнопки и для голосового.
// One send = one nonce. A lost answer (Safari «Load failed» over the Tailscale
// relay) is asked again with the same nonce: the server replays the first
// result and never types the message twice. Only when every try is lost does
// the text go back into the box.
// ⛔ 10-15: the direct Tailscale path to the MacBook lost 60% of its packets.
// One stalled connection held a try for 170 s, a try that never reached the
// server could only be retried blind, and «connection dropped (2/5)» made the owner
// resend by hand. Now a try is short; when its answer is lost the page asks
// the server by nonce (/act_result) whether the send arrived, and posts again
// only if it never did. The text sits in a local outbox until the server has
// answered for it: a reload or a long outage loses nothing, types nothing twice.
// 10-16: the answer to a send got lost on the weak link and the bubble sat on
// «sending…» for the whole 15 s before the nonce check confirmed it — looked
// unsent, the owner sent it again. The server answers a send in ~2.5 s.
const SEND_POST_MS=6000, SEND_POLL_MS=6000, SEND_PATIENCE_MS=10*60000;
const OUTBOX_KEY='rc.outbox', OUTBOX_MAX_AGE_MS=5*3600*1000;
const OUTBOX_LIVE={};
function sleepMs(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
// Link health from every request the page makes: the strip over the composer
// says «weak connection» while requests time out, so a slow send reads as the
// network, not as a broken panel.
const NET={fails:[], retrying:0};
let NET_SENT='';
function netNote(ok){
  const now=Date.now();
  if(!ok) NET.fails.push(now);
  NET.fails=NET.fails.filter(function(t){ return now-t<60000; });
  netShow();
}
function netShow(){
  const el=document.getElementById('netwarn');
  if(!el) return;
  const now=Date.now();
  const recent=NET.fails.filter(function(t){ return now-t<30000; }).length;
  const bad=NET.retrying>0 || recent>=2;
  const state = (NET.retrying>0 && recent>=4) ? 'dead' : bad ? 'weak' : 'ok';
  if(state!==NET_SENT){ NET_SENT=state; gameEmit('net', {state:state}); }
  el.classList.toggle('on', bad);
  if(bad) el.textContent = NET.retrying
    ? 'Weak connection to the computer. Your message is safe: it is confirmed and retried automatically, do not send it again.'
    : 'Weak connection to the computer: requests are timing out. Sends still get through, they are confirmed and retried automatically.';
}
setInterval(netShow, 5000);
async function fetchT(url, opts, ms){
  const ac=(typeof AbortController==='function') ? new AbortController() : null;
  const tm=ac ? setTimeout(function(){ ac.abort(); }, ms) : null;
  try{
    const r=await fetch(url, Object.assign({}, opts||{}, ac ? {signal:ac.signal} : {}));
    netNote(true);
    return r;
  }catch(e){ netNote(false); throw e; }
  finally{ if(tm) clearTimeout(tm); }
}
function outboxAll(){ try{ return JSON.parse(localStorage.getItem(OUTBOX_KEY)||'[]')||[]; }catch(e){ return []; } }
function outboxSave(list){ try{ localStorage.setItem(OUTBOX_KEY, JSON.stringify(list)); }catch(e){} }
function outboxPut(e){
  const l=outboxAll().filter(function(x){ return x.nonce!==e.nonce; });
  l.push(e); outboxSave(l);
}
function outboxDrop(nonce){ outboxSave(outboxAll().filter(function(x){ return x.nonce!==nonce; })); }
function newNonce(){ return Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10); }
function sendStatus(ent, text){
  if(!ent || ent.status===text) return;
  ent.status=text;
  if(TX_CUR===ent.sid) txDraw(ent.sid);
}
async function sayReliably(sent, payload, ent, nonce, posted){
  nonce=nonce||newNonce();
  if(ent) ent.nonce=nonce;
  OUTBOX_LIVE[nonce]=1;
  const body=JSON.stringify({action:'say',target:{id:sent,text:payload},dev:DEV,nonce:nonce});
  const prev=outboxAll().filter(function(x){ return x.nonce===nonce; })[0];
  outboxPut({nonce:nonce, sid:sent, payload:payload, shown:ent?ent.text:payload,
             att:ent?(ent.att||[]):[], at:prev?prev.at:Date.now()});
  const t0=Date.now();
  let n=0, last=null, rough=false;
  try{
    while(Date.now()-t0 < SEND_PATIENCE_MS){
      if(ent && ent.cancelled){ outboxDrop(nonce); throw new Error('cancelled'); }
      n++;
      try{
        if(!posted){
          posted=true;       // from here on it may have arrived: ask, never blind-post
          const r=await fetchT('/act',{method:'POST',headers:{'Content-Type':'application/json'},
                                body:body}, SEND_POST_MS);
          const j=await r.json();
          if(n>1) sendDiag(sent, 'recovered on try '+n+(j.replayed?' (replayed)':''));
          outboxDrop(nonce);
          return j;
        }
        const r=await fetchT('/act_result?nonce='+encodeURIComponent(nonce)+dq('&'),
                             {cache:'no-store'}, SEND_POLL_MS);
        const st=(r.status===404) ? {state:'unknown'} : await r.json();
        if(st.state==='done'){
          sendDiag(sent, 'confirmed by nonce on try '+n);
          outboxDrop(nonce);
          return Object.assign({}, st.out||{}, {replayed:true});
        }
        if(st.state==='running'){
          // the first try only outlived its timeout: a session being launched
          // takes half a minute. That is the server, not the link.
          if(rough && n===2){ rough=false; NET.retrying=Math.max(0,NET.retrying-1); NET.fails.pop(); netShow(); }
          sendStatus(ent, 'delivering…');
          await sleepMs(1500);
          continue;
        }
        posted=false;        // the server never got it: post again, same nonce
      }catch(e){
        if(String(e && e.message)==='cancelled') throw e;
        last=e;
        if(!rough){ rough=true; NET.retrying++; netShow(); }
        sendDiag(sent, 'network try '+n+': '+e);
        sendStatus(ent, 'weak connection — confirming the send ('+n+'), the text is safe');
        await sleepMs(Math.min(6000, 700*n));
      }
    }
  } finally {
    delete OUTBOX_LIVE[nonce];
    if(rough){ NET.retrying=Math.max(0, NET.retrying-1); netShow(); }
  }
  throw (last || new Error('no answer from the computer'));
}
async function sendText(payload, sent, restore, ent, nonce, posted){
  const ta=document.getElementById('drtext');
  if(!posted){ flash('… sending'); gameEmit('sending', {sid:sent}); }
  try{
    const j=await sayReliably(sent, payload, ent, nonce, posted);
    if(ent){ ent.status=''; ent.stuck=false; }
    gameEmit(j.ok?'sent':'fail', {sid:sent, msg:String(j.msg||'')});
    if(IN_HQ && j.ok) hqStatusClear();
    else flash(j.ok?('✓ '+j.msg):('✗ '+j.msg));
    if(!j.ok || j.unconfirmed) sendDiag(sent, (j.ok?'unconfirmed: ':'fail: ')+j.msg);
    if(!j.ok){
      // не ушло — снимаем пузырь и возвращаем текст, чтобы он не потерялся
      pendDrop(ent);
      if(TX_CUR===sent){
        if(restore!==undefined){ ta.value=(ta.value?ta.value+' ':'')+restore; taGrow(ta); }
        txDraw(sent);
      }
    } else {
      if(ent){ ent.ok=true; ent.at=Date.now(); }
      // Доставка не подтвердилась — текст мог не дойти. Держим пузырь на
      // экране с кнопкой возврата вместо того, чтобы гасить его по таймеру:
      // потерять набранное хуже, чем показать лишнюю строку.
      if(j.unconfirmed && ent){ ent.warn=true; }
      if(TX_CUR===sent) txDraw(sent);
      setTimeout(pollTx,1200); setTimeout(pollTx,4000); setTimeout(pollTx,9000);
      // Порядок в колонке — тоже часть ответа на отправку: строка должна
      // подняться сразу, а не на ближайшем тике через шесть секунд.
      setTimeout(pulse, 2000);
    }
  }catch(e){
    if(String(e && e.message)==='cancelled') return;
    // ⛔ No more «put the text back and hope». After ten minutes without the
    // mini the message stays a bubble and stays in the outbox, and goes out
    // by itself when the link is back (same nonce, so never twice).
    sendDiag(sent, 'network: '+e);
    if(ent){ ent.stuck=true; ent.status=''; }
    if(TX_CUR===sent) txDraw(sent);
    flash('✗ no connection to the computer for 10 minutes — the message waits and goes out by itself when the link is back');
    setTimeout(outboxFlush, 20000);
  }
}
// Messages a previous page (or this one, after a long outage) left unconfirmed.
// Asked by nonce first; posted again only if the server never saw them.
function outboxFlush(){
  const now=Date.now();
  outboxAll().forEach(function(e){
    if(!e || !e.nonce || OUTBOX_LIVE[e.nonce]) return;
    let ent=PEND.filter(function(p){ return p.nonce===e.nonce; })[0];
    if(!ent){
      ent=pendAdd(e.shown||e.payload, e.payload, e.sid, e.att||[]);
      ent.nonce=e.nonce;
      ent.at=e.at||now;
    }
    if(now-(e.at||now) > OUTBOX_MAX_AGE_MS){
      // too old for the server to remember its nonce: only a person may resend
      outboxDrop(e.nonce);
      ent.stuck=true;
      if(TX_CUR===e.sid) txDraw(e.sid);
      return;
    }
    sendText(e.payload, e.sid, undefined, ent, e.nonce, true);
  });
}
window.addEventListener('online', function(){ setTimeout(outboxFlush, 1000); });
setInterval(function(){ if(!document.hidden) outboxFlush(); }, 30000);
setTimeout(outboxFlush, 2500);
// Телефон (11-17): на экранной клавиатуре нет Shift, и Return-отправка не
// давала перенести строку вовсе. Там Return = новая строка, шлёт кнопка.
const TOUCH=window.matchMedia && matchMedia('(hover:none) and (pointer:coarse)').matches;
// no pinch zoom (the owner 11-17): iOS Safari ignores user-scalable=no, gesture events it honours
['gesturestart','gesturechange'].forEach(function(n){ document.addEventListener(n, function(e){ e.preventDefault(); }, {passive:false}); });
const TX_PH=TOUCH ? 'Message this session' : 'Message this session — Enter to send, Shift+Enter for a new line';
(function(){ const t=document.getElementById('drtext'); if(t && TOUCH) t.placeholder=TX_PH; })();
document.addEventListener('keydown', e=>{
  if(!TOUCH && e.target && e.target.id==='drtext' && e.key==='Enter' && !e.shiftKey){
    e.preventDefault(); sendTx();
  }
});

async function createFromDraft(t){
  // Вложения в черновике складываются в общую папку ещё до создания сессии,
  // а сюда попадают их пути — ровно так же, как в обычной отправке.
  const files=ATTACH.filter(function(a){ return a.path; })
    .map(function(a){ return '"'+a.path+'"'; }).join(' ');
  if(!t && !files){ flash('✗ a first prompt is required'); return; }
  const full=(t && files) ? (t+' '+files) : (t || files);
  const apaths=ATTACH.filter(function(a){ return a.path; }).map(function(a){ return a.path; });
  const shown=t;
  const keep=ATTACH.slice();
  const mv=(document.getElementById('draftmodel')||{}).value||'';
  const ev=(document.getElementById('drafteffort')||{}).value||'';
  const ta=document.getElementById('drtext');
  ta.disabled=true;
  // Создание сессии идёт до минуты: ждать его, глядя на пустой экран, — то же
  // самое, на что жаловались в обычной отправке.
  PEND=PEND.filter(function(p){ return p.sid!==''; });
  pendAdd(shown, full, '', apaths);
  ta.value=''; taGrow(ta);
  attachClear();
  const body=document.getElementById('drbody');
  // ⛔ Явно передаём ключ черновика. Ожидающая реплика кладётся под '',
  // а pendingHtml без третьего довода берёт TX_CUR — в черновике это
  // null, ключи не совпадали, и вместо сообщения был один загрузчик.
  body.innerHTML=pendingHtml('creating the session…', false, '');
  body.scrollTop=body.scrollHeight;
  document.getElementById('drmeta').textContent='creating the session…';
  flash('… creating the session, up to a minute');
  try{
    const r=await fetch('/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'new',target:{text:full, model:mv, effort:ev},dev:DEV})});
    const j=await r.json();
    flash(j.ok?('✓ '+j.msg):('✗ '+j.msg));
    if(j.ok){
      // id новой сессии приходит в тексте ответа — по нему и открываем её
      // после перезагрузки, чтобы черновик сразу стал живым чатом.
      const m=/new session ([0-9a-f]{8})/.exec(j.msg||'');
      gameEmit('created', {sid:m?m[1]:''});
      DRAFT=false;
      location.hash = m ? m[1] : '';
      setTimeout(function(){ location.reload(); }, 1200);
      return;
    }
  }catch(e){ flash('✗ '+e); }
  // не создалось — возвращаем и текст, и вложения: файлы уже лежат на
  // мини, терять их вместе с неудачной попыткой незачем.
  PEND=PEND.filter(function(p){ return p.sid!==''; });
  ta.value=t; taGrow(ta);
  ATTACH=keep; renderAttach();
  document.getElementById('drbody').innerHTML=
    '<div id="mainempty">Could not create the session. The text is back in the box.</div>';
  ta.disabled=false; ta.focus();
}
async function newSession(){
  const ta=document.getElementById('newtext'), t=ta.value.trim();
  if(!t){ flash('✗ a first prompt is required'); return; }
  if(!confirm('Create a new session? It takes one of the 13 slots.')) return;
  ta.disabled=true;
  flash('… launching the session, up to a minute');
  try{
    const r=await fetch('/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'new',target:{text:t}})});
    const j=await r.json();
    flash(j.ok?('✓ '+j.msg):('✗ '+j.msg));
    if(j.ok){ ta.value=''; setTimeout(()=>location.reload(), 2500); }
  }catch(e){ flash('✗ '+e); }
  ta.disabled=false;
}
document.addEventListener('keydown', e=>{
  if(e.target && e.target.id==='newtext' && e.key==='Enter' && (e.ctrlKey||e.metaKey)){
    e.preventDefault(); newSession();
  }
});

// ---- голосовой ввод ----
// Микрофон браузер отдаёт только в защищённом контексте. На http:// кнопки
// просто нет — появится сама, когда панель будет открыта по https (tailscale
// serve). Распознаёт браузер: Chrome шлёт звук в Google, Safari — в Apple.
// ---- голос ----
// Пишем звук и отдаём его СВОЕЙ машине, а не браузеру. Браузерный
// SpeechRecognition шлёт аудио наружу (Chrome в Google, Safari в Apple) и на
// смеси русского с английскими терминами ошибается так, что текст проще
// надиктовать заново. Здесь распознаёт whisper на устройстве, а термины и
// пунктуацию потом правит дешёвая модель.
let REC=null, RECCHUNKS=[], RECSTREAM=null;
function micReady(){
  // getUserMedia браузер даёт только в защищённом контексте: по http://<ip>
  // кнопки не будет, по http://localhost и по https — будет.
  return window.isSecureContext && navigator.mediaDevices
      && typeof navigator.mediaDevices.getUserMedia === 'function'
      && typeof window.MediaRecorder === 'function';
}
// HQ: the composer turns into a live waveform while the mic is open, then a
// «transcribing» scan until the text is back (the owner 10-16: the recording and
// send animations should make you want to send more tasks).
let RV=null;
function recViz(stream){
  try{
    const AC=window.AudioContext||window.webkitAudioContext;
    if(!AC) return;
    const ac=new AC(), src=ac.createMediaStreamSource(stream), an=ac.createAnalyser();
    an.fftSize=256; src.connect(an);
    const old=document.getElementById('hqrec'); if(old) old.remove();
    const box=document.createElement('div'); box.id='hqrec';
    box.innerHTML='<span class="rd"></span><b>REC</b><canvas width="640" height="80"></canvas><em>0:00</em>';
    document.querySelector('#drsend .composer').appendChild(box);
    const cv=box.querySelector('canvas'), g=cv.getContext('2d'), data=new Uint8Array(an.frequencyBinCount), t0=Date.now();
    const mic=document.getElementById('drmic');
    RV={ac:ac, box:box, raf:0};
    const frame=function(){
      if(!RV || RV.box!==box) return;
      an.getByteFrequencyData(data);
      const W=cv.width, H=cv.height, n=40, bw=W/n;
      g.clearRect(0,0,W,H);
      let sum=0;
      for(let i=0;i<n;i++){
        const v=data[Math.floor(i*data.length/n/1.7)]/255; sum+=v;
        const h=Math.max(4, v*H);
        g.fillStyle = i%3===0 ? 'rgba(255,95,162,.9)' : (i%2 ? 'rgba(77,225,255,.9)' : 'rgba(157,255,87,.85)');
        g.fillRect(Math.round(i*bw)+2, Math.round((H-h)/2), Math.max(3, Math.round(bw)-5), Math.round(h));
      }
      mic.style.boxShadow='0 0 0 '+Math.round(2+sum/n*14)+'px rgba(255,77,109,.35)';
      const sec=Math.floor((Date.now()-t0)/1000);
      box.querySelector('em').textContent=Math.floor(sec/60)+':'+String(sec%60).padStart(2,'0');
      RV.raf=requestAnimationFrame(frame);
    };
    frame();
  }catch(e){}
}
function recVizStop(){
  if(!RV) return;
  try{ cancelAnimationFrame(RV.raf); RV.ac.close(); }catch(e){}
  document.getElementById('drmic').style.boxShadow='';
  RV.box.classList.add('done');
  RV.box.querySelector('b').textContent='TRANSCRIBING';
  const box=RV.box; RV={box:box, raf:0, done:true};
  setTimeout(function(){ if(box.isConnected) box.remove(); }, 90000);
}
function recVizDone(){
  const box=document.getElementById('hqrec');
  if(box){ box.classList.add('gone'); setTimeout(function(){ box.remove(); }, 300); }
  RV=null;
}
function micStop(){
  if(REC && REC.state !== 'inactive'){ try{ REC.stop(); }catch(e){} }
}
async function toggleMic(){
  const b=document.getElementById('drmic');
  if(REC){ micStop(); return; }
  if(!TX_CUR && !DRAFT){ flash('✗ pick a session first'); return; }
  let stream;
  try{
    stream = await navigator.mediaDevices.getUserMedia({audio:{
      channelCount:1, echoCancellation:true, noiseSuppression:true,
      autoGainControl:true}});
  }catch(e){ flash('✗ microphone: '+(e.name||e)); return; }
  RECSTREAM=stream; RECCHUNKS=[];
  try{ REC=new MediaRecorder(stream); }
  catch(e){ flash('✗ recorder: '+e); stream.getTracks().forEach(t=>t.stop()); return; }
  REC.ondataavailable=function(ev){ if(ev.data && ev.data.size) RECCHUNKS.push(ev.data); };
  REC.onstop=async function(){
    b.classList.remove('rec');
    if(IN_HQ){ recVizStop(); gameEmit('rec', {on:false}); }
    RECSTREAM.getTracks().forEach(function(t){ t.stop(); });
    const blob=new Blob(RECCHUNKS, {type:(REC && REC.mimeType) || 'audio/webm'});
    REC=null; RECSTREAM=null; RECCHUNKS=[];
    if(!blob.size){ flash('✗ nothing recorded'); return; }
    b.disabled=true; b.classList.add('busy');
    const draft=DRAFT, sent=TX_CUR;
    // Расшифровка идёт секунд тридцать. Показываем это прямо в ленте тем же
    // «ожидающим» пузырём, что и обычная отправка: держать человека у
    // молчащего экрана, пока в углу мигает флеш, — то же самое, на что
    // жаловались раньше.
    let vent=null;
    if(!draft){
      vent=pendAdd('🎤 \u2026', '', sent);
      txDraw(sent);
      const body=document.getElementById('drbody');
      body.scrollTop=body.scrollHeight;
    }
    flash('\u2026 transcribing '+Math.round(blob.size/1024)+' KB');
    let text='';
    try{
      const r=await fetch('/voice?name=voice.webm'+dq('&'), {method:'POST', body:blob});
      const j=await r.json();
      if(!j.ok){
        flash('\u2717 '+j.msg);
        recVizDone(); gameEmit('voice', {ok:false});
        if(!draft){ pendDrop(vent); txDraw(sent); }
        b.disabled=false; b.classList.remove('busy');
        return;
      }
      text=j.text||'';
      recVizDone(); gameEmit('voice', {ok:true});
      flash('\u2713 transcribed ('+(j.model||'')+')');
    }catch(e){
      recVizDone(); gameEmit('voice', {ok:false});
      flash('\u2717 '+e);
      if(!draft){ pendDrop(vent); txDraw(sent); }
      b.disabled=false; b.classList.remove('busy');
      return;
    }
    b.disabled=false; b.classList.remove('busy');
    const ta=document.getElementById('drtext');
    if(draft || !sent){
      // В черновике отправлять некуда — сессии ещё нет.
      ta.value=(ta.value?ta.value.replace(/\\s+$/,'')+' ':'')+text;
      taGrow(ta); ta.focus();
      return;
    }
    if(!text.trim()){
      pendDrop(vent); txDraw(sent);
      flash('\u2717 nothing recognised');
      return;
    }
    // Голосовое уходит сразу: диктуют, чтобы не печатать, а не чтобы потом
    // ещё нажимать. Ошибку распознавания правит следующее сообщение.
    pendDrop(vent);
    const vsent=pendAdd(text, text, sent);
    txDraw(sent);
    await sendText(text, sent, undefined, vsent);
  };
  REC.start();
  b.classList.add('rec');
  if(IN_HQ){ recViz(stream); gameEmit('rec', {on:true}); }
  flash('● recording — click the mic again to stop');
}
if(micReady()) document.getElementById('drmic').style.display='inline-flex';
document.addEventListener('keydown', e=>{
  if(e.key!=='Escape') return;
  if(e.target && (e.target.id==='drtext'||e.target.id==='newtext')){ e.target.blur(); return; }
  if(document.getElementById('accmodal').classList.contains('on')){ accClose(); return; }
  if(document.body.classList.contains('filed')){ closeFile(); return; }
  if(IN_HQ && document.body.classList.contains('hqmore')){ hqMore(false); return; }
  if(IN_HQ){ gameEmit('esc'); return; }
  closeTx();
});
// Список слева серверный и сам не обновляется, поэтому «кто сейчас работает»
// застывало до перезагрузки. Лёгкий опрос обновляет только флаги и пульс —
// перерисовывать разметку целиком нельзя, это сбросило бы выбор и прокрутку.
// Перетаскивание между группами. Автораскладка смотрит только на слова
// первого промпта, поэтому мимо неё сессия попадает «в никуда» — и вернуть её
// на место иначе нечем.
let DRAG_ID=null;
function dragRow(ev){
  const li=ev.currentTarget;
  DRAG_ID=li.dataset.id;
  li.classList.add('dragging');
  try{ ev.dataTransfer.setData('text/plain', DRAG_ID);
       ev.dataTransfer.effectAllowed='move'; }catch(e){}
}
function dragEnd(ev){
  ev.currentTarget.classList.remove('dragging');
  document.querySelectorAll('.fold.dropok').forEach(function(e){e.classList.remove('dropok');});
}
function dragOver(ev){
  if(!DRAG_ID) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect='move';
  ev.currentTarget.classList.add('dropok');
}
function dragOut(ev){ ev.currentTarget.classList.remove('dropok'); }
async function dropRow(ev){
  ev.preventDefault();
  const fold=ev.currentTarget;
  fold.classList.remove('dropok');
  const id=DRAG_ID || (ev.dataTransfer && ev.dataTransfer.getData('text/plain'));
  DRAG_ID=null;
  if(!id) return;
  const proj=fold.dataset.proj || '';
  await act('set_project', {ids:[id], value:proj}, id+' → '+proj);
}
// DRAFT_MODELS приезжает из питона вместе с ROWS — список моделей на панели
// один, и разъезжаться его копиям больше негде.
const DRAFT_EFFORTS=[['','default effort'],['low','low'],['medium','medium'],
                     ['high','high'],['xhigh','xhigh'],['max','max']];
// Одна и та же подпись времени и в табах, и в ленте: абсолютные «17:39:18»
// на странице, открытой с утра, не отвечают на вопрос «давно ли это было».
// Один и тот же «ожидающий» блок для ленты и для черновика: в черновике
// транскрипта ещё нет, а показать отправленное надо так же сразу.
function pendingHtml(note, onlyDots, sid){
  let h='';
  if(!onlyDots){
    h+=pendFor(sid===undefined?TX_CUR:sid).map(function(p){
      const back='<button onclick="pendBack('+p.id+')">put it back in the box</button>'
                +'<button onclick="pendForget('+p.id+')">dismiss</button>';
      const foot = p.warn
        ? '<b class="warnnote">not confirmed</b>'+back
        : (p.stuck
            ? '<b class="warnnote">not sent yet — it goes out by itself when the connection is back</b>'+back
            : (p.queued
                ? '<b class="warnnote">queued — the session is busy</b>'+back
                : (p.ok ? '<span class="dlv">'+(IN_HQ?ci('check'):'✓')+' delivered</span>'
                        : '<span class="sndg">'+esc(p.status || 'sending…')+'</span>')));
      return '<div class="msg user pending'+((p.warn||p.queued)?' unsure':'')+'">'
        +(p.text?'<div class="mtext">'+md(p.text)+'</div>':'')
        +chips(p.att)
        +'<div class="mfoot" style="opacity:1">'+foot+'</div></div>';
    }).join('');
  }
  h+='<div class="msg assistant pending"><div class="mtext">'
    +'<span class="typing"><i></i><i></i><i></i></span>'+esc(note)+'</div></div>';
  return h;
}
function chips(paths){
  if(!paths || !paths.length) return '';
  return '<div class="filechips">'+paths.map(function(p){
    const nm=String(p).split('/').pop();
    // Картинку рисуем сразу: /file уже отдаёт её с нужным Content-Type, и
    // отдельный эндпоинт для превью не нужен. Щелчок по-прежнему открывает
    // файл целиком в правой колонке.
    if(isImgPath(p)){
      return '<span class="fshot" title="'+esc(p)+'"'
        +' onclick="openFile('+esc(JSON.stringify(p))+')">'
        +'<img src="'+esc(fileUrl(p))+'" alt="'+esc(nm)+'">'
        +'<b>'+esc(nm)+'</b></span>';
    }
    return '<span class="fchip" title="'+esc(p)+'"'
      +' onclick="openFile('+esc(JSON.stringify(p))+')">&#128206;'
      +'<b>'+esc(nm)+'</b></span>';
  }).join('')+'</div>';
}
// Все файлы сессии в одном месте: по ленте их искать глазами бесполезно,
// особенно когда шагов три сотни.
function sessionFiles(j){
  const out=[], seen={};
  (j.artifacts||[]).forEach(function(a){
    if(seen[a.path]) return; seen[a.path]=1;
    out.push({path:a.path, name:a.name, note:a.title||'', art:true});
  });
  (j.shown||[]).forEach(function(m){
    (m.paths||[]).forEach(function(p){
      if(seen[p]) return; seen[p]=1;
      out.push({path:p, name:String(p).split('/').pop(), note:'', art:false});
    });
    if(m.role==='step' && m.path && !seen[m.path]){
      seen[m.path]=1;
      out.push({path:m.path, name:String(m.path).split('/').pop(), note:m.tool||'', art:false});
    }
  });
  return out;
}
function openArtifacts(){
  const list=sessionFiles(TX_LAST||{});
  document.body.classList.add('filed');
  document.getElementById('fname').textContent='Files in this session — '+list.length;
  document.getElementById('fbody').innerHTML= list.length
    ? '<div id="flist">'+list.map(function(f){
        return '<span class="fchip'+(f.art?' art':'')+'" title="'+esc(f.path)+'"'
          +' onclick="openFile('+esc(JSON.stringify(f.path))+')">'
          +(f.art?'&#128196;':'&#128206;')+'<b>'+esc(f.name)+'</b>'
          +(f.note?' <span class="stepnote">'+esc(f.note)+'</span>':'')+'</span>';
      }).join('')+'</div>'
    : '<pre>no files mentioned in the visible history</pre>';
}
function mfoot(m, note){
  return '<div class="mfoot"><span>'+esc(note?note+' · ':'')+esc(whenTxt(m))+'</span>'
    +'<button class="cp" title="Copy message" onclick="copyMsg(event,this)">copy</button>'
    +'</div>';
}
// ⛔ 12.11 the owner: «копирование аутпута криво — текст копируется без форматинга».
// The button copied innerText of the RENDERED message: bold, headings, list
// numbers (md() draws numbered lists as <ul>), code fences and tables were
// gone. Now the clipboard gets the message's own markdown as text/plain (Telegram
// and any chat turn **bold** / `code` / ``` back into formatting) and clean HTML
// as text/html (Docs, Notes, Gmail keep the look). A plain Cmd+C over a selection
// in the feed goes the same way, via htmlToMd() of the selected fragment.
// No RegExp and no backslashes: this JS lives in a Python string.
const SRC=new Map();
function srcKey(t){
  t=String(t==null?'':t);
  const k=t.length.toString(36)+txHash(t);
  if(!SRC.has(k)){
    SRC.set(k, t);
    if(SRC.size>3000) SRC.delete(SRC.keys().next().value);
  }
  return k;
}
function squeezeNl(x){
  while(x.indexOf(NL+NL+NL)>=0) x=x.split(NL+NL+NL).join(NL+NL);
  return x;
}
function trimNl(x){
  while(x.length && x.charAt(x.length-1)===NL) x=x.slice(0,-1);
  while(x.length && x.charAt(0)===NL) x=x.slice(1);
  return x;
}
function h2m(n, inPre){
  if(n.nodeType===3) return inPre ? n.nodeValue : n.nodeValue.split(NL).join(' ');
  if(n.nodeType!==1 && n.nodeType!==11) return '';
  function kids(pre){ let s=''; n.childNodes.forEach(function(c){ s+=h2m(c, pre); }); return s; }
  if(n.nodeType===11) return kids(inPre);
  const tg=n.tagName, cl=n.classList;
  if(tg==='BUTTON' || tg==='INPUT' || tg==='SELECT' || tg==='TEXTAREA' || tg==='SCRIPT'
     || tg==='STYLE' || tg==='IMG' || tg==='DETAILS') return '';
  if(cl && (cl.contains('mfoot') || cl.contains('filechips'))) return '';
  if(tg==='BR') return NL;
  if(tg==='STRONG' || tg==='B'){ const k=kids(inPre); return k.trim() ? '**'+k+'**' : k; }
  if(tg==='EM' || tg==='I'){ const k=kids(inPre); return k.trim() ? '_'+k+'_' : k; }
  if(tg==='CODE') return inPre ? n.textContent : '`'+n.textContent+'`';
  if(tg==='PRE') return NL+'```'+NL+trimNl(n.textContent)+NL+'```'+NL;
  if(tg==='A'){
    const k=kids(inPre), h=n.getAttribute('href')||'';
    return h && h!==k ? '['+k+']('+h+')' : k;
  }
  if(tg.length===2 && tg.charAt(0)==='H' && '123456'.indexOf(tg.charAt(1))>=0)
    return NL+'#'.repeat(+tg.charAt(1))+' '+kids(false).trim()+NL;
  if(cl && cl.contains('mdh')) return NL+'### '+kids(false).trim()+NL;
  if(tg==='UL' || tg==='OL'){
    let s=NL, i=0;
    n.childNodes.forEach(function(c){
      if(c.nodeType!==1 || c.tagName!=='LI') return;
      let t=''; c.childNodes.forEach(function(x){ t+=h2m(x, false); });
      s+=(tg==='OL' ? (++i)+'. ' : '- ')+t.trim()+NL;
    });
    return s;
  }
  if(tg==='LI') return '- '+kids(false).trim()+NL;
  if(tg==='BLOCKQUOTE')
    return NL+kids(false).trim().split(NL).map(function(l){ return '> '+l; }).join(NL)+NL;
  if(tg==='HR') return NL+'---'+NL;
  if(tg==='TABLE'){
    const out=[];
    n.querySelectorAll('tr').forEach(function(tr, ri){
      const cells=[];
      tr.querySelectorAll('th,td').forEach(function(td){
        let t=''; td.childNodes.forEach(function(x){ t+=h2m(x, false); });
        cells.push(t.trim().split('|').join('/'));
      });
      out.push('| '+cells.join(' | ')+' |');
      if(ri===0) out.push('|'+cells.map(function(){ return '---'; }).join('|')+'|');
    });
    return NL+out.join(NL)+NL;
  }
  if(tg==='P' || (cl && (cl.contains('msg') || cl.contains('mtext')))) return kids(inPre)+NL+NL;
  return kids(inPre);
}
function htmlToMd(root){ return squeezeNl(h2m(root, false)).trim(); }
// HTML for rich editors: same markup, minus our UI, headings as real headings
function clipHtml(root){
  const c=root.cloneNode(true);
  c.querySelectorAll('.mfoot,.filechips,button,details,input,select').forEach(function(e){ e.remove(); });
  c.querySelectorAll('.mdh').forEach(function(d){
    const h=document.createElement('h3'); h.innerHTML=d.innerHTML; d.replaceWith(h);
  });
  c.querySelectorAll('[class]').forEach(function(e){ e.removeAttribute('class'); });
  return c.innerHTML;
}
let CLIP_PAYLOAD=null;
document.addEventListener('copy', function(e){
  if(!e.clipboardData) return;
  if(CLIP_PAYLOAD){
    e.clipboardData.setData('text/plain', CLIP_PAYLOAD.text);
    if(CLIP_PAYLOAD.html) e.clipboardData.setData('text/html', CLIP_PAYLOAD.html);
    e.preventDefault();
    return;
  }
  const t=e.target;
  if(t && t.nodeType===1 && (t.tagName==='TEXTAREA' || t.tagName==='INPUT')) return;
  const sel=window.getSelection(), body=document.getElementById('drbody');
  if(!sel || sel.isCollapsed || !sel.rangeCount || !body) return;
  const r0=sel.getRangeAt(0);
  if(!body.contains(r0.commonAncestorContainer)) return;
  // inside one code block: the browser's own plain text is already right
  const anc=r0.commonAncestorContainer.nodeType===1 ? r0.commonAncestorContainer
                                                     : r0.commonAncestorContainer.parentNode;
  if(anc && anc.closest && anc.closest('pre')) return;
  const div=document.createElement('div');
  for(let i=0;i<sel.rangeCount;i++) div.appendChild(sel.getRangeAt(i).cloneContents());
  const text=htmlToMd(div);
  if(!text) return;
  e.clipboardData.setData('text/plain', text);
  e.clipboardData.setData('text/html', clipHtml(div));
  e.preventDefault();
});
function clipWrite(text, html){
  // execCommand fires the copy event above, which fills both formats; works on
  // plain http too, where navigator.clipboard does not exist
  CLIP_PAYLOAD={text:text, html:html};
  const ta=document.createElement('textarea');
  ta.value=text; ta.style.position='fixed'; ta.style.top='-1000px';
  document.body.appendChild(ta); ta.focus(); ta.select();
  let ok=false;
  try{ ok=document.execCommand('copy'); }catch(e){ ok=false; }
  ta.remove(); CLIP_PAYLOAD=null;
  return ok;
}
async function copyMsg(ev, btn){
  ev.stopPropagation();
  const box=btn.closest('.msg');
  const mt=box ? (box.querySelector('.mtext')||box) : null;
  if(!mt) return;
  const raw=mt.dataset && mt.dataset.src ? SRC.get(mt.dataset.src) : null;
  const text=raw!=null ? raw : htmlToMd(mt);
  const html=clipHtml(mt);
  let done=clipWrite(text, html);
  if(!done && navigator.clipboard && window.isSecureContext){
    try{
      if(window.ClipboardItem){
        await navigator.clipboard.write([new ClipboardItem({
          'text/plain': new Blob([text], {type:'text/plain'}),
          'text/html': new Blob([html], {type:'text/html'})})]);
      } else await navigator.clipboard.writeText(text);
      done=true;
    }catch(e){ done=false; }
  }
  if(!done){ flash('✗ could not copy — select the text by hand'); return; }
  const was=btn.textContent;
  btn.textContent='copied'; btn.classList.add('done');
  setTimeout(function(){ btn.textContent=was; btn.classList.remove('done'); }, 1200);
}
function whenTxt(m){
  return m && m.tse ? agoRu(Date.now()/1000 - m.tse)
                    : String((m&&m.ts)||'').replace('T',' ');
}
function agoShort(sec){
  sec=Math.max(0,Math.floor(sec));
  if(sec<60) return 'now';
  const m=Math.floor(sec/60);
  if(m<60) return m+'m';
  const h=Math.round(sec/3600);
  if(h<24) return h+'h';
  return Math.round(sec/86400)+'d';
}
function agoRu(sec){
  sec=Math.max(0,Math.floor(sec));
  if(sec<60) return 'just now';
  const pl=function(n,a,b,c){const d=n%10,h=n%100;
    return (d===1&&h!==11)?a:((d>=2&&d<=4&&(h<10||h>=20))?b:c);};
  const m=Math.floor(sec/60);
  if(m<60) return m+' min ago';
  const h=Math.floor(m/60), mm=m%60;
  if(h<24) return h+'h'+(mm?' '+mm+'m':'')+' ago';
  const d=Math.floor(h/24), hh=h%24;
  return d+'d'+(hh?' '+hh+'h':'')+' ago';
}
// Считаем в браузере, а не в питоне: страница живёт часами, и отрендеренное
// сервером «5 минут назад» к вечеру врало бы на весь вечер.
function retime(){
  const now=Date.now()/1000;
  document.querySelectorAll('#side time[data-ts]').forEach(function(t){
    const ts=parseFloat(t.dataset.ts);
    const v = ts ? agoRu(now-ts) : '';
    // в табе — коротко («2h»): «1h 53m ago» съедало место у проекта и
    // пересказа; полное время — в подсказке
    if(t.closest('.row')){
      const sv = ts ? agoShort(now-ts) : '';
      if(t.textContent!==sv) t.textContent=sv;
      t.title=v;
    } else t.textContent=v;
  });
}
let PULSE_T=null;
// ⛔ 09-24: после выкладки открытая вкладка жила со старыми CSS/JS, а
// колонку пересобирала свежая разметка с сервера — таб рисовался помесью двух
// версий. Сервер называет свою версию заголовком X-Panel-UI; разошлась —
// перечитываем страницу сами, если ничего не потеряем (черновики и так в
// localStorage, открытый чат вернётся по #hash), иначе просим один раз.
let UI_STALE=false;
function uiStale(){
  if(UI_STALE) return;
  const ta=document.getElementById('drtext');
  const typing = document.activeElement===ta || (ta && ta.value.trim());
  const held = typing || sideBusy() || picks().length || ATTACH.length || PEND.length
    || Object.keys(ACT_INFLIGHT).length
    || (document.getElementById('accmodal')||{classList:{contains:function(){return false;}}}).classList.contains('on');
  if(!held){ location.reload(); return; }
  UI_STALE=true;
  flash('… the panel was updated — reload the page when convenient');
}
// Одно слово статуса на строку; row_status() в питоне — его близнец.
function rowStatus(x){
  const dg=x.digest||{}, t=x.tui||{};
  if(x.live && (dg.waiting || x.perm)) return ['waiting for you','st-wait'];
  if(x.live && t.kind==='compacting') return ['compacting','st-work'];
  if(x.busy) return ['working','st-work'];
  if(x.live) return ['idle','st-idle'];
  if(x.retired) return ['retired','st-ret'];
  if(x.state==='dead') return ['queued','st-queue'];
  return ['stopped','st-stop'];
}
// Полоса «сейчас работают» — близнец nowbar_html() в питоне.
let NOWBAR_H='';
function nowbarHtml(rows){
  const wait=rows.filter(function(r){ return r.live && ((r.digest||{}).waiting || r.perm); });
  const work=rows.filter(function(r){ return r.busy && wait.indexOf(r)<0; })
                 .sort(function(a,b){ return (b.la_ts||0)-(a.la_ts||0); });
  if(!wait.length && !work.length)
    return '<div id="nowbar" class="nowbar none">nothing is working right now</div>';
  const pill=function(r, kind){
    const t=String(r.title||r.name||r.short);
    return '<button class="np '+kind+(r.short===TX_CUR?' sel':'')+'" data-id="'+esc(r.short)+'"'
      +' title="'+esc((kind==='wait'?'waiting for you: ':'working: ')+t)+'"'
      +' onclick="openTx(this.dataset.id)"><i></i><span>'+esc(t.slice(0,28))+'</span></button>';
  };
  let head='<b>'+work.length+'</b> working';
  if(wait.length) head+=' · <b class="w">'+wait.length+'</b> waiting for you';
  return '<div id="nowbar" class="nowbar"><div class="nh">'+head+'</div>'
    +wait.map(function(r){ return pill(r,'wait'); }).join('')
    +work.map(function(r){ return pill(r,'work'); }).join('')+'</div>';
}
// Какая сессия открыта — и в списке, и в полосе.
function markSel(short){
  document.querySelectorAll('#side .row.sel').forEach(function(e){e.classList.remove('sel');});
  document.querySelectorAll('#nowbar .np').forEach(function(b){
    b.classList.toggle('sel', !!short && b.dataset.id===short); });
  if(!short) return null;
  const li=document.querySelector('#side .row[data-id="'+short+'"]');
  if(li) li.classList.add('sel');
  return li;
}
// ⛔ Порядок строк считает сервер, и до этого он застывал до перезагрузки:
// свежая отправка обновляла время в табе, а сама строка оставалась внизу —
// хотя в старую сессию пишут ровно затем, чтобы поднять её к свежим. Колонку
// пересобираем с сервера, но только когда порядок или состав РЕАЛЬНО
// изменились: перерисовывать сто тридцать строк каждые шесть секунд незачем.
let SIDE_SIG=null;
function sideSig(rows){
  // Состав и раскладка по секциям — ровно то, из чего сервер строит колонку.
  // Время сюда не входит намеренно: оно меняется постоянно, и колонка
  // пересобиралась бы на каждом тике.
  return rows.map(function(x){
    return x.short+':'+(x.live?1:0)+(x.retired?1:0)+':'+(x.state||'');
  }).sort().join('|');
}
function orderStale(by){
  // Строка свежее той, что стоит над ней, — порядок разъехался.
  let stale=false;
  document.querySelectorAll('#sidelist ul.rows').forEach(function(ul){
    // ⛔ Работающие пропускаем ТОЛЬКО в папках: там сервер держит их наверху
    // независимо от времени. В плоском списке он сортирует по одной свежести,
    // и пропуск занятых убивал бы всю затею — сессия занята ровно после того,
    // как ей написали, то есть именно тогда, когда её и надо поднять.
    const folded=!!ul.closest('details.fold');
    let prev=null;
    Array.prototype.forEach.call(ul.children, function(li){
      const x=by[li.dataset.id];
      if(!x) return;
      if(folded && x.busy) return;
      const v=x.la_ts||0;
      if(prev!==null && v>prev+1) stale=true;
      prev=v;
    });
  });
  return stale;
}
// ⛔ Пока человек правит имя, колонку трогать НЕЛЬЗЯ. Пересборка меняет
// innerHTML целиком, и поле ввода с недописанным именем исчезает вместе с
// разметкой. Ловилось это постоянно: апдейт в активной сессии двигает её
// время, orderStale() видит разъехавшийся порядок и пересобирает колонку —
// то есть чем активнее сессия, тем вернее человека прерывало на полуслове.
function sideBusy(){
  return !!document.querySelector('#sidelist .tabedit');
}
async function reloadSide(){
  const box=document.getElementById('sidelist');
  if(IN_HQ || !box || sideBusy()) return;
  // Пересборку переживают раскрытые папки, отметки, выбранная строка и
  // прокрутка — иначе обновление порядка выглядело бы как перезагрузка.
  const open={};
  box.querySelectorAll('details.fold').forEach(function(d){
    if(d.open) open[d.dataset.proj||'']=1;
  });
  const picked={};
  box.querySelectorAll('.pick:checked').forEach(function(c){ picked[c.value]=1; });
  const selRow=box.querySelector('.row.sel');
  const sel=selRow ? selRow.dataset.id : '';
  const top=box.scrollTop, atop=(box.parentNode||{}).scrollTop||0;
  try{
    const r=await fetch('/side'+dq('?')+(dq('?')?'&':'?')+'sort='+encodeURIComponent(SORT));
    if(!r.ok) return;
    box.innerHTML=await r.text();
    NOWBAR_H='';
  }catch(e){ return; }
  box.querySelectorAll('details.fold').forEach(function(d){
    d.open=!!open[d.dataset.proj||''];
  });
  box.querySelectorAll('.pick').forEach(function(c){
    if(picked[c.value]) c.checked=true;
  });
  markSel(sel || TX_CUR);
  box.scrollTop=top;
  if(box.parentNode) box.parentNode.scrollTop=atop;
  retime();
}
// Inside HQ the chat does not poll /api/state on its own (and never rebuilds
// the hidden side list): HQ polls anyway and hands the state in here. Two
// pollers plus a 272 KB side list choked a link that loses a third of its
// packets (10-16).
function hqPulse(j, ui){ try{ pulseApply(j, ui); }catch(e){} }
async function pulse(){
  retime();
  if(!document.hidden && !IN_HQ){
    try{
      const resp=await fetchT('/api/state?lite=1'+dq('&'), {}, 20000);
      pulseApply(await resp.json(), resp.headers.get('X-Panel-UI'));
    }catch(e){}
  }
  // ⛔ Один тикер, а не «сколько раз позвали». Раньше здесь стоял голый
  // setTimeout, а visibilitychange звал pulse() напрямую — каждое
  // возвращение на вкладку заводило ЕЩЁ одну цепочку, и /api/state (почти
  // секунда на мини) дёргался пачками.
  if(PULSE_T) clearTimeout(PULSE_T);
  PULSE_T=setTimeout(pulse, 6000);
}
function pulseApply(j, ui){
  {
    {
      if(ui && typeof UI_VER!=='undefined' && ui!==UI_VER) uiStale();
      const by={}; j.rows.forEach(function(x){ by[x.short]=x; });
      let busy=0;
      document.querySelectorAll('#side .row[data-id]').forEach(function(li){
        const x=by[li.dataset.id]; if(!x) return;
        const dg=x.digest||{};
        li.classList.toggle('busyrow', !!x.busy);
        li.classList.toggle('waitrow', !!(x.live && (dg.waiting || x.perm)));
        if(x.busy) busy++;
        // Без этого retime() пересчитывает старую отметку и время в табе
        // стоит на месте, сколько сессии ни пиши.
        const t=li.querySelector('time[data-ts]');
        if(t && x.la_ts) t.dataset.ts=x.la_ts;
        // Слово статуса — то же, что рисует сервер (row_status в питоне).
        // Погашенную действием строку не трогаем: её слово поставил markRow.
        const st=li.querySelector('.st');
        if(st && !li.classList.contains('gone')){
          const sv=rowStatus(x), sc='st '+sv[1];
          if(st.textContent!==sv[0]) st.textContent=sv[0];
          if(st.className!==sc) st.className=sc;
        }
        // контекст в табе — только от 60%, как рисует сервер
        const used=dg.ctx||0, cap=dg.ctx_max||0, meta=li.querySelector('.rmeta');
        if(meta){
          const pc=(used && cap) ? Math.min(100,Math.round(used*100/cap)) : 0;
          let cx=meta.querySelector('.tctx');
          if(pc>=60){
            if(!cx){ cx=document.createElement('span'); meta.insertBefore(cx, meta.firstChild); }
            const cc='tctx '+(pc>=80?'hot':'warm');
            if(cx.className!==cc) cx.className=cc;
            if(cx.textContent!==pc+'%') cx.textContent=pc+'%';
          } else if(cx) cx.remove();
        }
      });
      const nbar=document.getElementById('nowbar');
      if(nbar){
        const nh=nowbarHtml(j.rows);
        if(nh!==NOWBAR_H){ nbar.outerHTML=nh; NOWBAR_H=nh; }
      }
      // Шапка открытой карточки берёт статус из того же ответа: отдельный
      // опрос ради одной строки был бы лишним кругом по сети.
      const cur=TX_CUR?by[TX_CUR]:null, el=document.getElementById('drtui');
      if(el){
        // Отметка принадлежит конкретной сессии: без этой сверки после
        // переключения в шапке минуту светилось бы время чужого хода.
        const same = cur && TURN_TSE && TURN_SID===TX_CUR;
        const v=tuiText(cur?cur.tui:null,
                        same ? (Date.now()/1000 - TURN_TSE)
                             : (cur?cur.busy_age:null),
                        same ? TURN_USE : null);
        el.textContent=v[0];
        el.className=v[1]||'';
        const ct=cur && cur.tui ? cur.tui.kind : '';
        stopVis(!!cur && (ct==='working' || ct==='compacting' || (!!cur.busy && ct!=='idle')));
        el.title=same ? tuiTitle(TURN_USE) : '';
      }
      // Лимиты обновляются на месте: цифры теперь снимаются раз в минуту, и
      // ради них перезагружать страницу было бы издевательством.
      if(j.meta){
        const lim=j.meta.limits||{};
        document.querySelectorAll('.lim[data-k]').forEach(function(el){
          const v=lim[el.dataset.k];
          const fill=el.querySelector('.tr i'), num=el.querySelector('b'),
                res=el.querySelector('.lres');
          if(fill) fill.style.width=(v==null?0:Math.max(0,Math.min(100,Math.round(v))))+'%';
          if(num) num.textContent=(v==null)?'—':(v+'%');
          el.classList.toggle('hot', v!=null && v>=80);
          el.classList.toggle('warm', v!=null && v>=60 && v<80);
          const r=lim[el.dataset.k+'_resets']||'';
          if(res) res.textContent=r.replace(/\\s*\\([^)]*\\)\\s*$/,'').replace(' at ',' ').slice(0,22);
          el.title='resets '+(r||'?').slice(0,34);
        });
        const age=document.querySelector('.limage time');
        if(age && j.meta.usage_ts) age.dataset.ts=j.meta.usage_ts;
        const agebox=document.querySelector('.limage');
        if(agebox) agebox.classList.toggle('stale', !!j.meta.usage_stale);
      }
      const sig=sideSig(j.rows);
      if(SIDE_SIG===null) SIDE_SIG=sig;
      // ⛔ SIDE_SIG двигаем ТОЛЬКО вместе с настоящей пересборкой. Если
      // отметить подпись как учтённую и не пересобрать (человек правит имя),
      // порядок застынет до следующей перемены состава — а её можно ждать
      // часами.
      else if(!IN_HQ && !sideBusy() && (sig!==SIDE_SIG || orderStale(by))){
        SIDE_SIG=sig; reloadSide();
      }
      // ⛔ Здесь же стояло обновление «живых» — по селектору первого чипа,
      // а первый чип и есть «working». Каждые 6 с число работающих затиралось
      // числом живых: «17 working», когда работали две (09-24). Живые и так
      // видны полосой Live n/cap.
      const chip=document.querySelector('.chip.busy');
      const nbusy=(j.meta && typeof j.meta.busy==='number') ? j.meta.busy : busy;
      if(chip){ chip.querySelector('b').textContent=nbusy; chip.classList.toggle('on', nbusy>0); }
    }
  }
}
retime();
PULSE_T=setTimeout(pulse, 2000);
// Скрытую вкладку pulse намеренно не опрашивает, поэтому по возвращении
// время и флаги в табах были бы устаревшими до следующего тика.
// ⛔ Ленту чата тоже надо перечитать. В скрытой вкладке браузер душит
// setTimeout до одного раза в минуту, а то и замораживает совсем — pollTx
// вместе с ним. Без этого вызова человек возвращался к переписке, застывшей
// на том, что было при уходе, и «сообщение не появилось» выглядело именно
// так: помогала только перезагрузка страницы.
document.addEventListener('visibilitychange', function(){
  if(document.hidden) return;
  retime(); pulse();
  if(TX_CUR){ if(TX_TIMER) clearTimeout(TX_TIMER); pollTx(); }
});

// Восстанавливаем выбранную сессию после перезагрузки — её кладёт openTx в hash.
(function(){
  const h=(location.hash||'').replace(/^#/,'');
  if(h==='new'){ newDraft(); return; }
  if(h && typeof ROWS!=='undefined' && ROWS[h]) openTx(h);
})();

/* --- ширина колонок мышью ---------------------------------------------
   Тянем не элемент, а переменные --wside/--wfile на корне: колонки живут
   в grid, и двигать пришлось бы три соседних трека сразу. Ширину берём
   замером реального элемента, а не разбором grid-template-columns —
   вычисленное значение возвращает уже разложенные пиксели и ломается,
   как только в шаблоне меняется число треков. */
function gutSize(which){
  var el=document.getElementById(which==='side'?'side':'filepane');
  return el?el.getBoundingClientRect().width:(which==='side'?300:420);
}
function gutVar(which){ return which==='side'?'--wside':'--wfile'; }

function gutDrag(ev,which){
  if(ev.button!==0) return;
  ev.preventDefault();
  var root=document.documentElement, x0=ev.clientX, w0=gutSize(which);
  document.body.classList.add('resizing');
  function move(e){
    var d=e.clientX-x0;
    var w=(which==='side')?w0+d:w0-d;
    var lo=(which==='side')?190:260;
    var hi=(which==='side')?620:Math.round(window.innerWidth*0.72);
    root.style.setProperty(gutVar(which),Math.max(lo,Math.min(hi,w))+'px');
  }
  function up(){
    document.removeEventListener('mousemove',move);
    document.removeEventListener('mouseup',up);
    document.body.classList.remove('resizing');
    try{ localStorage.setItem(which==='side'?'wside':'wfile',
         Math.round(gutSize(which))); }catch(err){}
  }
  document.addEventListener('mousemove',move);
  document.addEventListener('mouseup',up);
}

function gutReset(which){
  document.documentElement.style.removeProperty(gutVar(which));
  try{ localStorage.removeItem(which==='side'?'wside':'wfile'); }catch(err){}
}

(function(){
  try{
    var a=localStorage.getItem('wside'), b=localStorage.getItem('wfile');
    if(a) document.documentElement.style.setProperty('--wside',a+'px');
    if(b) document.documentElement.style.setProperty('--wfile',b+'px');
  }catch(err){}
})();

// HQ embed: the flash line lives in the hidden side column, so it moves out to
// stay visible; Alt+arrows and Ctrl+J/K step through agents in the parent.
// HQ's ⋯ opens the chat header (files, menu, model, effort, steps) as a card.
function hqMore(on){
  const b=document.body;
  const v=(on===undefined) ? !b.classList.contains('hqmore') : !!on;
  b.classList.toggle('hqmore', v);
  gameEmit('more', {on:v});
  return v;
}
if(IN_HQ){
  (function(){ const f=document.getElementById('flash'); if(f) document.body.appendChild(f); })();
  document.addEventListener('mousedown', function(ev){
    if(!document.body.classList.contains('hqmore')) return;
    const h=document.getElementById('drhead');
    if(h && !h.contains(ev.target)) hqMore(false);
  });
  document.addEventListener('keydown', function(e){
    const up=(e.altKey && e.key==='ArrowUp') || (e.ctrlKey && e.key==='k');
    const dn=(e.altKey && e.key==='ArrowDown') || (e.ctrlKey && e.key==='j');
    if(up || dn){ e.preventDefault(); gameEmit('nav', {dir: up ? -1 : 1}); }
  });
  (function(){
    const put=function(el, name){ if(el) el.innerHTML=ci(name); };
    put(document.querySelector('.composer button[onclick*="drfile"]'), 'paperclip');
    put(document.getElementById('drmic'), 'mic');
    put(document.getElementById('drstop'), 'debug-stop');
    put(document.querySelector('.composer .go.iconbtn'), 'arrow-up');
  })();
  gameEmit('ready', {sid: TX_CUR || ''});
}

// 12.11 «поиск по всем сессиям, чтобы я легко находил любые сессии»: one box
// over the column. The server (rc_search.py) matches names, summaries and the
// text of every conversation, retired included; results replace the column
// until the box is cleared, so one can click through several of them.
// ⛔ No RegExp here: this JS lives in a Python string where a backslash is a trap.
let SQ_T=null, SQ_N=0;
function sqInput(){
  const q=document.getElementById('sq').value.trim();
  if(SQ_T) clearTimeout(SQ_T);
  if(!q){ sqShow(false); return; }
  SQ_T=setTimeout(function(){ sqRun(q); }, 220);
}
function sqShow(on){
  const res=document.getElementById('sres'), list=document.getElementById('sidelist');
  if(!res || !list) return;
  res.hidden=!on; list.style.display=on ? 'none' : '';
  if(!on){ SQ_N++; res.innerHTML=''; document.getElementById('sqn').textContent=''; }
}
async function sqRun(q){
  if(!q) return;
  const n=++SQ_N;
  document.getElementById('sqn').textContent='…';
  let j=null;
  try{
    const r=await fetchT('/api/search?q='+encodeURIComponent(q)+dq('&'), {cache:'no-store'}, 25000);
    j=await r.json();
  }catch(e){ j={ok:false, msg:'search failed: '+e}; }
  if(n!==SQ_N) return;
  sqRender(j);
}
function sqMark(text, terms){
  text=String(text||'');
  const low=text.toLowerCase();
  let out='', i=0;
  while(i<text.length){
    let best=-1, bl=0;
    terms.forEach(function(t){
      const p=low.indexOf(t, i);
      if(p>=0 && (best<0 || p<best || (p===best && t.length>bl))){ best=p; bl=t.length; }
    });
    if(best<0 || !bl){ out+=esc(text.slice(i)); break; }
    out+=esc(text.slice(i, best))+'<mark>'+esc(text.slice(best, best+bl))+'</mark>';
    i=best+bl;
  }
  return out;
}
function sqWhen(ts){
  if(!ts) return '';
  const sec=Date.now()/1000-ts;
  if(sec<86400*2) return agoShort(sec);
  const d=new Date(ts*1000);
  return ('0'+d.getDate()).slice(-2)+'.'+('0'+(d.getMonth()+1)).slice(-2);
}
function sqRender(j){
  const res=document.getElementById('sres'), cnt=document.getElementById('sqn');
  sqShow(true);
  if(!j || !j.ok){
    cnt.textContent='';
    res.innerHTML='<div class="snone">'+esc((j && j.msg) || 'search failed')+'</div>';
    return;
  }
  const terms=j.terms || [];
  cnt.textContent=j.n+(j.n===1 ? ' session' : ' sessions');
  if(!j.n){
    res.innerHTML='<div class="snone">Nothing found. The search covers names, summaries'
      +' and the text of every conversation, retired included.</div>';
    return;
  }
  const rows=j.results.map(function(x){
    const hits=(x.hits || []).map(function(h){
      return '<div class="shit"><span class="srole">'+(h.role==='user' ? 'you' : 'claude')
        +(h.ts ? ' · '+sqWhen(h.ts) : '')+'</span> '+sqMark(h.snip, terms)+'</div>';
    }).join('');
    const more=x.n>(x.hits || []).length ? '<div class="smore">'+x.n+' messages match</div>' : '';
    const meta=(x.project && x.project!=='No project' ? x.project+' · ' : '')
      +(x.cls==='ret' ? 'retired · ' : x.cls==='live' ? 'live · ' : '')+sqWhen(x.la_ts);
    return '<li class="srow '+x.cls+(x.busy ? ' busyrow' : '')+'" data-id="'+esc(x.short)+'"'
      +' onclick="sqOpen(this.dataset.id)">'
      +'<div class="sline"><span class="sdot"></span><span class="stitle">'+sqMark(x.title, terms)+'</span>'
      +'<span class="smeta" title="'+esc(meta)+'">'+esc(meta)+'</span></div>'
      +(x.sub ? '<div class="ssub">'+sqMark(x.sub, terms)+'</div>' : '')+hits+more+'</li>';
  }).join('');
  res.innerHTML=(j.n>j.results.length
      ? '<div class="snone">the '+j.results.length+' best of '+j.n+', add a word to narrow it</div>' : '')
    +'<ul class="srows">'+rows+'</ul>';
  sqSel();
}
function sqSel(){
  document.querySelectorAll('#sres .srow').forEach(function(li){
    li.classList.toggle('sel', !!TX_CUR && li.dataset.id===TX_CUR);
  });
}
function sqOpen(id){
  openTx(id); setTimeout(sqSel, 60);
  // phone layout: the chat sits under the whole column, out of sight
  if(window.matchMedia && window.matchMedia('(max-width:860px)').matches){
    const m=document.getElementById('main');
    if(m) setTimeout(function(){ m.scrollIntoView({block:'start'}); }, 120);
  }
}
function sqKey(e){
  // stopPropagation: the page's own Esc closes the open chat (closeTx)
  if(e.key==='Escape'){ e.stopPropagation(); e.target.value=''; sqShow(false); e.target.blur(); }
  else if(e.key==='Enter'){
    e.preventDefault();
    if(SQ_T){ clearTimeout(SQ_T); SQ_T=null; }
    const li=document.querySelector('#sres:not([hidden]) .srow');
    if(li) sqOpen(li.dataset.id); else sqRun(e.target.value.trim());
  }
}
document.addEventListener('keydown', function(e){
  if(e.key!=='/' || e.metaKey || e.ctrlKey || e.altKey || IN_HQ) return;
  const t=e.target, tag=(t && t.tagName) || '';
  if(tag==='INPUT' || tag==='TEXTAREA' || tag==='SELECT' || (t && t.isContentEditable)) return;
  const q=document.getElementById('sq');
  if(!q) return;
  e.preventDefault(); q.focus(); q.select();
});
"""


def esc(s):
    return html.escape(str(s) if s is not None else "")


def digest_html(r):
    """«На чём закончили»: последняя просьба и последний ответ.

    Derived from the transcript tail, never from a model call — this renders for
    every row on every page load, and 90 summarisation calls a refresh would be
    absurd. Clicking it opens the full conversation.
    """
    d = r.get("digest") or {}
    bits = []
    # Сессия с незакрытым вопросом не работает и не простаивает — она ждёт
    # человека, и понять это по строке иначе нельзя, шторку на каждую не откроешь.
    if d.get("waiting"):
        bits.append('<b class="wait">⏳ waiting for you:</b> ' + esc(d["waiting"][:160]))
    elif r.get("live") and r.get("perm"):
        bits.append('<b class="wait">waiting for your permission:</b> ' + esc(r["perm"][:160]))
    if d.get("last_user"):
        bits.append("<b>asked:</b> " + esc(d["last_user"][:160]))
    if d.get("last_assistant"):
        bits.append("<b>replied:</b> " + esc(d["last_assistant"][:200]))
    if not bits:
        return ""
    return ('<div class="dig" onclick="openTx(\'%s\')" title="open the conversation">%s</div>'
            % (esc(r["short"]), "<br>".join(bits)))


def _tok(n):
    if n >= 1000000:
        return ("%.1fM" % (n / 1000000.0)).replace(".0M", "M")
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def ctxbar_html(r):
    """Заполненность контекста — как в приложении.

    Сессия с полным контекстом выглядит совершенно живой, но новых ходов уже не
    принимает: сообщения из панели просто пропадают. По строке это иначе не
    видно, а лечится оно одним /compact.
    """
    d = r.get("digest") or {}
    used, cap = d.get("ctx") or 0, d.get("ctx_max") or 0
    if not used or not cap:
        return ""
    pct = min(100, int(round(used * 100.0 / cap)))
    cls = "ctxbar" + (" hot" if pct >= 80 else (" warm" if pct >= 60 else ""))
    tail = " · time to /compact" if pct >= 80 else ""
    return ('<div class="%s" title="session context, model %s"><span class="tr">'
            '<i style="width:%d%%"></i></span><b>%s / %s · %d%%</b>%s</div>'
            % (cls, esc(d.get("model_id") or "?"), pct,
               _tok(used), _tok(cap), pct, esc(tail)))


# Список моделей один на всю панель: он же в шапке строки, он же в баре
# массовых действий, он же в черновике. Три копии уже разъезжались — Haiku
# лежал в разметке, но не в белом списке rc_control, и выбор отваливался с
# «invalid value». Подписи человеческие: «claude-fable-5-1» в строке читается
# хуже, чем «Fable 5.1».
ROW_MODELS = (("", None),) + C.MODEL_CHOICES
# Бар массовых действий и черновик берут тот же список: разметку для бара
# собираем здесь, черновику отдаём JSON рядом с ROWS.
BULK_MODEL_OPTS = "".join('<option value="%s">%s</option>'
                          % (esc(v), esc(t or "as running (clear)"))
                          for v, t in ROW_MODELS)
DRAFT_MODELS_JSON = json.dumps([["", "CLI default"]]
                               + [list(m) for m in C.MODEL_CHOICES],
                               ensure_ascii=False)
# Ровно то, что принимает `claude --effort`: проверено 24-08, CLI на чужое
# значение отвечает «Valid values: low, medium, high, xhigh, max».
# «Ультракода» среди них нет и флага такого у CLI тоже нет.
ROW_EFFORTS = (("", None), ("low", "low"), ("medium", "medium"),
               ("high", "high"), ("xhigh", "xhigh"), ("max", "max"))


def model_name(mid):
    """claude-opus-5 → Opus 5, claude-haiku-4-5 → Haiku 4.5."""
    if not mid:
        return ""
    parts = mid.replace("claude-", "").split("-")
    if not parts:
        return mid
    return (parts[0].capitalize() + " " + ".".join(parts[1:])).strip()


def opt_select(short, kind, cur, choices, blank, now=""):
    """Переключатель модели/эффорта.

    В пустом пункте показываем, ЧТО СЕЙЧАС: в реестре у сессии обычно ничего не
    выставлено, и «по умолчанию» само по себе не отвечало на вопрос «а на чём
    она работает». Реальное значение берём из транскрипта.

    ⛔ 09-24: «default model · now X» читалось как «сессия на безымянной
    дефолтной модели», а после рестарта так и было: подъём шёл без --model.
    Теперь подъём без выбора в реестре берёт то, на чём сессия работала
    (G.launch_opts), и пункт называется по модели. «CLI default» — только
    когда не известно ничего.
    """
    label = ("%s · as running" % now) if now else blank
    opts = []
    for value, text in choices:
        opts.append('<option value="%s"%s>%s</option>'
                    % (esc(value), " selected" if value == (cur or "") else "",
                       esc(text if text else label)))
    # data-opt — чтобы после смены найти нужный селект, не разбирая разметку;
    # data-was — куда откатиться, если действие не применилось.
    return ('<select class="rowopt" title="applies on the next restart"'
            ' data-opt="%s" data-was="%s"'
            ' onchange="rowOpt(this,\'%s\',\'%s\')">%s</select>'
            % (kind, esc(cur or ""), esc(short), kind, "".join(opts)))


def head_menu(r, devs, cur):
    """Пункты единого меню в шапке: открыть карточку, переехать на …, повторить
    заметку о переезде. Одна выпадашка вместо кнопки «…» и отдельного «move
    to…» (просьба владельца 04-09). Значение — URL панели, а не id устройства: у
    каждой машины свой список устройств, и «local» хаба для источника — чужой
    хост. Свой URL хаб подставляет из BIND/PORT."""
    items = []
    if r["link"] and r["live"]:
        items.append({"v": "card:https://claude.ai/code/%s" % r["link"], "t": "open card"})
    for d in devs or []:
        if d.get("id") == (cur or {}).get("id"):
            continue
        url = d.get("url") or ("http://%s:%d" % (BIND, PORT))
        name = d.get("name") or d.get("id")
        items.append({"v": "move:" + url, "t": "move to " + name, "n": name})
    if r.get("moved_from") and r["live"]:
        items.append({"v": "note", "t": "move note (re-send)"})
    # Удалить совсем: в отличие от Retire/Kill сессия уходит из панели (11-16)
    items.append({"v": "delete", "t": "delete session"})
    return items


def row_head_html(r, devs=None, cur=None):
    """Шапка чата: всё про выбранную сессию.

    Раньше это жило в самой строке, но в узкой колонке слева места нет — да и
    незачем: подробности нужны только про ту сессию, которую сейчас читаешь.
    """
    acts = []
    if r["live"]:
        acts.append('<button class="danger" onclick="act(\'kill\',\'%s\',\'killing %s\')">'
                    'Kill</button>' % (r["short"], r["short"]))
        # Убить = убрать совсем; слот освободить, но сессию сохранить — вот это
        acts.append('<button onclick="act(\'kill_requeue\',\'%s\',\'%s → queue\')">'
                    'Queue</button>' % (r["short"], r["short"]))
    else:
        acts.append('<button class="go" onclick="act(\'up\',\'%s\',\'launching %s\')">'
                    'Launch</button>' % (r["short"], r["short"]))
        if r["retired"]:
            acts.append('<button onclick="act(\'unretire\',\'%s\',\'requeuing %s\')">'
                        'Queue</button>' % (r["short"], r["short"]))
        else:
            acts.append('<button onclick="act(\'retire\',\'%s\',\'retiring %s\')">'
                        'Retire</button>' % (r["short"], r["short"]))
    # Модель, эффорт и ссылку прячем под «ещё»: они нужны раз в день, а место
    # в шапке занимали постоянно. Наверху остаётся только то, чем пользуются.
    dg = r.get("digest") or {}
    if r.get("provider") in ("codex", "vibe"):
        # their own CLI: its models (and Codex's efforts), applied on Restart
        # through `resume`, which keeps the thread
        if r["provider"] == "codex":
            import rc_codex
            mch = [("", "Codex default")] + list(rc_codex.models())
            ech = [("", "default effort")] + [(e, e) for e in rc_codex.EFFORT_CHOICES]
        else:
            # Vibe keeps a session's model on resume: show it, chosen at launch
            mch = ech = None
        opts = ([opt_select(r["short"], "model", r.get("model"), mch,
                            "default", r.get("prov_model") or "")] if mch else
                ['<select class="rowopt" disabled title="Mistral Vibe keeps the model it was launched with">'
                 '<option>%s</option></select>' % esc("Mistral Vibe · " + (r.get("prov_model") or ""))])
        if ech:
            opts.append(opt_select(r["short"], "effort", r.get("effort"), ech,
                                   "default effort", ""))
    elif r.get("provider"):
        # another model (a plan key such as MiniMax): Claude models and
        # efforts do not apply to it, so the selects would only mislead
        pl = {"codex": "Codex", "vibe": "Mistral Vibe", "minimax": "MiniMax"}.get(r["provider"], r["provider"])
        opts = ['<select class="rowopt" disabled title="runs on %s"><option>%s</option></select>'
                % (esc(pl), esc(pl + (" · " + dg["model_id"] if dg.get("model_id")
                                         and dg.get("model_id") != "<synthetic>" else "")))]
    else:
        opts = [opt_select(r["short"], "model", r.get("model"), ROW_MODELS,
                           "CLI default", model_name(dg.get("model_id"))),
                opt_select(r["short"], "effort", r.get("effort"), ROW_EFFORTS,
                           "CLI default effort", dg.get("effort_now") or "")]
    extra = []
    if r.get("empty"):
        extra.append('<span class="warnnote">empty — a claude -p /usage artefact, '
                     'holding a slot for nothing</span>')
    if r.get("moved_host"):
        extra.append('<span class="warnnote">moved to %s</span>' % esc(r["moved_host"]))
    if r.get("moved_from"):
        extra.append('<span class="warnnote">moved here from %s</span>' % esc(r["moved_from"]))
    mp = r.get("move_pending") or {}
    if mp:
        extra.append('<span class="warnnote">move to %s: %s%s</span>'
                     % (esc(mp.get("to", "")), esc(mp.get("state", "")),
                        (" — " + esc(mp.get("last", ""))) if mp.get("last") else ""))
    # acts больше не рисуем в шапке: те же действия теперь на самой карточке
    # слева, и две копии одних кнопок только путали бы.
    return {"acts": "", "opts": "".join(opts), "extra": "".join(extra),
            "menu": head_menu(r, devs, cur)}


def row_status(r):
    """(word, css class) for the status line of a row. pulse() recomputes it
    in JS every 6 s (rowStatus) — the two must say the same thing.

    One word per state, in the order a person needs them: a session waiting on
    a question blocks until answered, working ones are what you watch, idle
    live ones hold a slot, the rest are history.
    """
    dg = r.get("digest") or {}
    tui = r.get("tui") or {}
    if r.get("live") and (dg.get("waiting") or r.get("perm")):
        return "waiting for you", "st-wait"
    if r.get("live") and tui.get("kind") == "compacting":
        return "compacting", "st-work"
    # Без числа токенов с экрана TUI: это счётчик одного запроса, он
    # сбрасывается на каждом шаге и читался как «непонятное число» (см. tuiTok).
    if r.get("busy"):
        return "working", "st-work"
    if r.get("live"):
        return "idle", "st-idle"
    if r.get("retired"):
        return "retired", "st-ret"
    if r.get("state") == "dead":
        return "queued", "st-queue"
    return "stopped", "st-stop"


def row_html(r):
    """Строка левой колонки — таб, а не карточка: только опознать и выбрать.

    ⛔ 09-24, «не могу быстро понять, в какой сессии я и какие работают, всё
    сливается»: каждая строка была карточкой в рамке внутри папки в рамке, с
    чекбоксом, двумя кнопками и процентом на виду, а работающая отличалась от
    простаивающей только бледной заливкой и мигающей точкой. Теперь строка
    плоская; состояние сказано словом и цветом в строке статуса (зелёный —
    работает, оранжевый — ждёт тебя, янтарный — в очереди); открытая сессия —
    своим, синим цветом, который не путается ни с одним из них. Чекбокс
    встаёт на место точки при наведении, кнопки всплывают поверх, не сдвигая
    строку.
    """
    cls = "live" if r["live"] else ("ret" if r["retired"] else
                                    ("queue" if r["state"] == "dead" else "idle"))
    if r.get("busy"):
        cls += " busyrow"
    dg = r.get("digest") or {}
    if r["live"] and (dg.get("waiting") or r.get("perm")):
        cls += " waitrow"
    word, stcls = row_status(r)
    # Контекст в табе — только когда пора что-то делать (от 60%): «13%» на
    # каждой строке было шумом, точная цифра есть в шапке чата.
    ctx = ""
    used, cap = dg.get("ctx") or 0, dg.get("ctx_max") or 0
    if used and cap:
        pct = min(100, int(round(used * 100.0 / cap)))
        if pct >= 60:
            ctx = ('<span class="tctx %s" title="context used">%d%%</span>'
                   % ("hot" if pct >= 80 else "warm", pct))
    # Убить и «в очередь» — прямо в строке: их жмут, пробегая список, и ради
    # этого открывать сессию незачем. stopPropagation обязателен — иначе клик
    # по иконке ещё и откроет чат.
    if r["live"]:
        kill_act, kill_t = "kill", "Kill — stop and retire"
        q_act, q_t = "kill_requeue", "Queue — stop but keep in the queue"
    elif r["retired"]:
        kill_act, kill_t = "kill", "Kill — remove from rotation"
        q_act, q_t = "unretire", "Queue — bring back"
    else:
        kill_act, kill_t = "retire", "Retire — stop restoring it"
        q_act, q_t = "up", "Launch now"
    rowacts = (
        '<span class="rowacts">'
        '<button class="ri danger" title="%s"'
        ' onclick="rowAct(event,\'%s\',\'%s\')">&times;</button>'
        '<button class="ri" title="%s"'
        ' onclick="rowAct(event,\'%s\',\'%s\')">&#8615;</button>'
        '<button class="ri danger" title="Delete for good — stops it and removes it'
        ' from the panel (transcript kept 7 days in ~/.claude/rc-trash)"'
        ' onclick="delSession(event,\'%s\')">del</button></span>'
        % (esc(kill_t), kill_act, esc(r["short"]),
           esc(q_t), q_act, esc(r["short"]), esc(r["short"])))
    pname = r.get("project") or ""
    # в папке проект — её заголовок, CSS прячет его там вместе с точкой
    proj = ('<span class="proj" title="%s">%s</span><span class="sep">·</span>'
            % (esc(pname), esc(pname))) if pname and pname != "No project" else ""
    name = ("empty — a claude -p /usage artefact" if r.get("empty")
            else (r.get("summary") or r["name"] or r["short"]))
    title = r.get("title") or r["short"]
    # Пересказ почти всегда устроен как «Тема: подробности», и тема уже стоит
    # заголовком — не повторяем её во второй строке.
    if title and name.startswith(title):
        rest = name[len(title):]
        if rest[:1] in (":", "—", "-", "·") and len(rest) > 12:
            name = rest[1:].strip()
    return ('<li class="row %s" data-id="%s" draggable="true"'
            ' ondragstart="dragRow(event)" ondragend="dragEnd(event)"'
            ' onclick="openTx(\'%s\')">'
            '<span class="lead"><span class="dot"></span>'
            '<input type="checkbox" class="pick" value="%s" aria-label="select %s"'
            ' onclick="event.stopPropagation()"></span>'
            '<div class="rmain"><div class="rline">'
            '<div class="tabtitle" title="%s" ondblclick="rowRename(event,\'%s\')">%s</div>'
            '<span class="rmeta">%s%s<time data-ts="%s"></time></span></div>'
            '<div class="rsum"><span class="st %s">%s</span><span class="sep">·</span>'
            '<span class="tabname">%s</span></div></div>%s</li>'
            % (cls, esc(r["short"]), esc(r["short"]),
               esc(r["short"]), esc(r["short"]),
               esc(title + " — double-click to rename"), esc(r["short"]), esc(title),
               ctx, proj, int(r.get("la_ts") or 0),
               stcls, esc(word), esc(name), rowacts))


def _ago(sec):
    if sec < 90:
        return "%ds" % sec
    if sec < 5400:
        return "%dm" % round(sec / 60.0)
    return "%dh" % round(sec / 3600.0)


# «Снятых» бывает под сотню, и раскрывать их по умолчанию — значит утопить в них
# всё остальное. Живое и очередь открыты, архив свёрнут.
FOLD_OPEN = {"Live", "Queued", "Idle"}


def flat_html(items):
    """Плоский список по свежести — когда важно «что трогали последним», а не
    в каком проекте оно лежит."""
    rows = sorted(items, key=lambda x: x.get("la_ts") or 0, reverse=True)
    return ['<ul class="rows">%s</ul>' % "".join(row_html(r) for r in rows)]


def folders_html(items, title):
    groups = {}
    for r in items:
        groups.setdefault(r.get("project") or "No project", []).append(r)
    out = []
    for name, rows in sorted(groups.items(),
                             key=lambda kv: (-sum(1 for x in kv[1] if x.get("busy")),
                                             -len(kv[1]), kv[0])):
        # items уже отсортированы по свежести, сортировка стабильна — так что
        # это поднимает работающие наверх, не ломая порядок внутри групп
        rows = sorted(rows, key=lambda x: 0 if x.get("busy") else 1)
        busy = sum(1 for x in rows if x.get("busy"))
        out.append('<details class="fold"%s data-proj="%s" ondragover="dragOver(event)"'
                   ' ondragleave="dragOut(event)" ondrop="dropRow(event)">'
                   '<summary><span class="fname">%s</span><span class="fcount">%d</span>%s'
                   '</summary><ul class="rows">%s</ul></details>'
                   % (" open" if title in FOLD_OPEN else "", esc(name),
                      esc(name), len(rows),
                      '<span class="fbusy">%d working</span>' % busy if busy else "",
                      "".join(row_html(r) for r in rows)))
    return out


def nowbar_html(rows):
    """«Сейчас работают» — над всеми секциями, по одной кнопке на сессию.

    Ответ на «какие сессии работают» за один взгляд, не листая папки: живые
    раскиданы по проектам, и работающая могла стоять третьей в пятой папке.
    Ждущие ответа идут первыми — они стоят, пока их не тронешь. pulse()
    пересобирает эту полосу из того же снимка (nowbarHtml в JS).
    """
    wait = [r for r in rows if r.get("live")
            and ((r.get("digest") or {}).get("waiting") or r.get("perm"))]
    work = [r for r in rows if r.get("busy") and r not in wait]
    work.sort(key=lambda x: x.get("la_ts") or 0, reverse=True)
    if not wait and not work:
        return '<div id="nowbar" class="nowbar none">nothing is working right now</div>'
    pills = []
    for r, kind in [(x, "wait") for x in wait] + [(x, "work") for x in work]:
        t = r.get("title") or r.get("name") or r["short"]
        pills.append('<button class="np %s" data-id="%s" title="%s"'
                     ' onclick="openTx(\'%s\')"><i></i><span>%s</span></button>'
                     % (kind, esc(r["short"]),
                        esc(("waiting for you: " if kind == "wait" else "working: ") + t),
                        esc(r["short"]), esc(t[:28])))
    head = '<b>%d</b> working' % len(work)
    if wait:
        head += ' · <b class="w">%d</b> waiting for you' % len(wait)
    return ('<div id="nowbar" class="nowbar"><div class="nh">%s</div>%s</div>'
            % (head, "".join(pills)))


def side_html(snap, sort_by="projects"):
    """Разметка левой колонки: секции, папки, строки сессий.

    Отдельно от page(), потому что колонку надо уметь пересобрать БЕЗ всей
    страницы. Порядок строк считает сервер, и до этого он застывал до
    перезагрузки: отправишь в старую сессию — время в табе освежается, а
    сама строка остаётся внизу, хотя писали в неё как раз чтобы поднять.
    """
    rows, app = snap["rows"], snap["app"]
    live = [r for r in rows if r["live"]]
    queue = [r for r in rows if not r["live"] and not r["retired"] and r["state"] == "dead"]
    idle = [r for r in rows if not r["live"] and not r["retired"] and r["state"] != "dead"]
    ret = [r for r in rows if r["retired"] and not r["live"]]

    parts = [nowbar_html(rows)]
    if app:
        parts.append('<section class="grp"><h2 title="chats started in the app: '
                     'children of the daemon, no transcript here">From the app'
                     '<span class="gcount">%d</span></h2><ul class="rows">' % len(app))
        for a in app:
            lnk = ('<a class="lnk" href="https://claude.ai/code/%s" target="_blank"'
                   ' rel="noreferrer" onclick="event.stopPropagation()">open card</a>'
                   % esc(a.get("link", ""))) if a.get("link") else ""
            parts.append('<li class="row live approw"><span class="lead"><span class="dot"></span></span>'
                         '<div class="rmain"><div class="rline"><div class="tabtitle">'
                         'app chat · pid %s</div><span class="rmeta">%s</span></div>'
                         '<div class="rsum"><span class="tabname">%s</span></div></div>'
                         '<span class="rowacts"><button class="ri danger" title="Kill this app chat"'
                         ' onclick="appKill(event,\'%s\')">&times;</button></span></li>'
                         % (a["pid"], esc(a["etime"]), lnk, a["pid"]))
        parts.append('</ul></section>')

    for items, title, sub in ((live, "Live", "holding memory and slots"),
                              (queue, "Queued", "restored automatically when a slot frees"),
                              (idle, "Idle", "neither live nor queued"),
                              (ret, "Retired", "the guardian leaves these alone")):
        if not items:
            continue
        # bulk retire belongs where the queue is, not in an ssh session
        if title == "Queued":
            tools = ('<div class="acts">'
                     '<button onclick="retireOld()">Retire old</button></div>')
        elif title == "Live":
            # Порядок переключают, глядя на живые, — значит и кнопки должны
            # быть там же, а не в шапке колонки над лимитами.
            tools = ('<div class="sortbar"><span>sort</span>'
                     '<button class="%s" onclick="setSort(\'projects\')">projects</button>'
                     '<button class="%s" onclick="setSort(\'recent\')">recent</button></div>'
                     % ("on" if sort_by != "recent" else "",
                        "on" if sort_by == "recent" else ""))
        else:
            tools = ""
        allbox = ('<label class="allbox"><input type="checkbox" '
                  'onclick="pickSection(this)"> all</label>')
        parts.append('<section class="grp sec-%s"><h2 title="%s">%s<span class="gcount">%d</span>%s</h2>%s'
                     % (title.lower(), esc(sub), esc(title), len(items), allbox, tools))
        parts.extend(flat_html(items) if sort_by == "recent"
                     else folders_html(items, title))
        parts.append('</section>')
    return "".join(parts)


def side_for(dev_id="", sort_by="projects"):
    """Только левая колонка — для обновления порядка без перезагрузки."""
    devs = C.devices()
    cur = C.device_by(dev_id) or devs[0]
    url = (cur.get("url") or "") if cur.get("id") != "local" else ""
    snap = json.loads(remote_get(url, "/api/state")) if url else C.snapshot()
    return side_html(snap, sort_by)


# HQ polls every few seconds over a link that loses a third of its packets
# (10-16: «больше минуты не открывались чаты»). Parts that rarely change go
# out once; after that only their signature, until they change. Row texts
# (summaries, last lines) are split off the rows for the same reason.
_DIET_KEYS = ("sleeping", "ach", "focus", "models", "efforts", "projects", "quests", "rows_txt")
_ROW_TXT = ("title", "name", "summary", "last_a", "last_u")


def game_diet(out, have):
    rows = out.get("rows")
    if isinstance(rows, list):
        out["rows_txt"] = dict((r.get("short"), [r.get(k) for k in _ROW_TXT]) for r in rows)
        for r in rows:
            for k in _ROW_TXT:
                r.pop(k, None)
    got = dict(x.split(":", 1) for x in (have or "").split(",") if ":" in x)
    sigs = {}
    for k in _DIET_KEYS:
        if k not in out:
            continue
        sig = hashlib.md5(json.dumps(out[k], sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:10]
        sigs[k] = sig
        if got.get(k) == sig:
            del out[k]
    out["sigs"] = sigs


def row_entry(r, devs, cur):
    h = row_head_html(r, devs, cur)
    dg = r.get("digest") or {}
    return {"name": r["name"] or r["short"],
            "acts": h["acts"], "opts": h["opts"], "extra": h["extra"],
            "menu": h["menu"],
            "pid": r["pid"] or 0, "ts": int(r.get("la_ts") or 0),
            "live": bool(r["live"]), "busy": bool(r.get("busy")),
            "ctx": dg.get("ctx") or 0, "ctxmax": dg.get("ctx_max") or 0,
            "model": dg.get("model_id") or "",
            "effort": r.get("effort") or dg.get("effort_now") or ""}


def page(dev_id="", sort_by="projects", light=False, only=None):
    devs = C.devices()
    cur = C.device_by(dev_id) or devs[0]
    url = (cur.get("url") or "") if cur.get("id") != "local" else ""
    if url:
        snap = json.loads(remote_get(url, "/api/state"))
    else:
        snap = C.snapshot()
    m, rows, app = snap["meta"], snap["rows"], snap["app"]
    # HQ's chat (light): the side list is hidden there and was 272 KB of every
    # load over a lossy link; other sessions come one by one from /api/row
    side = "" if light else side_html(snap, sort_by)

    # Лимиты берём из снимка: для соседа локальные цифры были бы чужими.
    u = snap["meta"].get("limits") or {}
    # Три окна, которые считает сам Claude Code: пятичасовое («Current session»),
    # недельное по всем моделям и отдельное недельное по Fable. Раньше в панель
    # попадало только недельное, а упираешься обычно в пятичасовое.
    def bar(label, pct, right, title, cls="", key="", res="", extra=""):
        """Одна полоска «сколько занято из потолка».

        `res` — когда лимит обнулится. Раньше это жило только в подсказке при
        наведении, то есть для человека, который уже знает, что там что-то есть.
        `key` метит полоску, чтобы pulse() обновлял её на месте.
        """
        fill = 0 if pct is None else max(0, min(100, int(round(pct))))
        return ('<div class="lim%s"%s%s title="%s"><span class="lname">%s</span>'
                '<span class="tr"><i style="width:%d%%"></i></span>'
                '<b>%s</b><em class="lres">%s</em></div>'
                % (cls, (' data-k="%s"' % esc(key)) if key else "", extra,
                   esc(title), esc(label), fill, esc(right), esc(res)))

    def short_reset(s):
        """«Aug 30 at 7:00am (Europe/Helsinki)» → «Aug 30 7:00am». Часовой пояс
        свой же, а колонка узкая."""
        s = re.sub(r"\s*\([^)]*\)\s*$", "", s or "").strip()
        return s.replace(" at ", " ")[:22]

    limits = []
    for key, label in (("session", "5h"), ("week", "Week"), ("week_fable", "Fable")):
        v = u.get(key)
        cls = "" if v is None else (" hot" if v >= 80 else (" warm" if v >= 60 else ""))
        limits.append(bar(label, v, "—" if v is None else "%d%%" % v,
                          "resets " + (u.get(key + "_resets") or "?")[:34], cls,
                          key, short_reset(u.get(key + "_resets"))))
    # Когда цифры сняты. `claude -p /usage` — настоящая CLI-сессия, а не чтение
    # файла, поэтому отставание тут нормальное явление и его надо видеть: без
    # отметки «52%» получасовой давности неотличимы от свежих.
    limits.append('<div class="limage">read <time data-ts="%d"></time>%s</div>'
                  % (int(snap["meta"].get("usage_ts") or 0),
                     ' · the last poll failed'
                     if snap["meta"].get("usage_stale") else ''))
    limits_html = '<div class="limits">%s</div>' % "".join(limits)

    # 05.11: все аккаунты с последними снятыми лимитами. /usage видит только
    # текущий аккаунт, у остальных — цифры на момент, когда они были текущими.
    accs = snap["meta"].get("accounts") or []
    if accs:
        arows = ['<div class="limage">Accounts · current in bold</div>']
        for a in accs:
            name = (a.get("account") or "?").split("@")[0][:18]
            wk = a.get("week")
            if wk is None:
                right, res, cls = "—", "no data", ""
            elif a.get("week_reset_passed"):
                right, res, cls = "0%?", "reset", ""
            else:
                right = "%d%%" % wk
                res = short_reset(a.get("week_resets"))
                cls = " hot" if wk >= 80 else (" warm" if wk >= 60 else "")
            title = "%s%s · week %s · 5h %s · resets %s" % (
                a.get("account") or "?", " (current)" if a.get("current") else "",
                "—" if wk is None else "%d%%" % wk,
                "—" if a.get("session") is None else "%d%%" % a["session"],
                a.get("week_resets") or "?")
            arows.append(bar(name, wk, right, title, cls + (" cur" if a.get("current") else ""), "", res))
            if a.get("ts") and not a.get("current"):
                arows.append('<div class="limage">read <time data-ts="%d"></time></div>' % int(a["ts"]))
        limits_html += '<div class="limits accs">%s</div>' % "".join(arows)

    over = m["live_total"] > m["cap"]
    # Про перелимит уже кричит красный чип «18/13 живых» — второй раз абзацем
    # не нужно. От предупреждений остаётся только то, что даёт кнопку.
    empties = [r for r in rows if r["live"] and r.get("empty")]
    warn = ('<div class="topbar"><button class="danger" onclick="killEmpty()">'
            'Kill empty (%d)</button></div>' % len(empties)) if empties else ""
    hot = " hot" if over else ""
    # Красным — то, что реально мешает работать: очередь стоит при load выше
    # порога, а при свободной памяти под сотню мегабайт машина уходит в своп.
    load_hot = " hot" if m["load1"] > m["load_gate"] else (
        " warm" if m["load1"] > m["load_gate"] * 0.6 else "")
    swap_hot = " hot" if m["swap_mb"] >= 8000 else (
        " warm" if m["swap_mb"] >= 4000 else "")
    free_hot = " hot" if m["free_mb"] <= 300 else (
        " warm" if m["free_mb"] <= 800 else "")
    # Всё, у чего есть потолок, показываем одинаково — полоской. Цифра без
    # потолка («22.26 load») не отвечает на вопрос «это много?», а рядом с
    # порогом отвечает сразу.
    def _pair(used, total):
        """«сколько из скольких» — цифра без потолка не отвечает на вопрос
        «это много?», а рядом с потолком отвечает сразу."""
        if total >= 1024:
            return "%.1f/%.1fG" % (used / 1024.0, total / 1024.0)
        return "%d/%dM" % (used, total)

    mem_mb = m.get("mem_mb") or 0
    mach = [bar("Live", (m["live_total"] * 100.0 / m["cap"]) if m["cap"] else 0,
                "%d/%d" % (m["live_total"], m["cap"]),
                "live sessions of %d slots" % m["cap"], hot),
            bar("Load", (m["load1"] * 100.0 / m["load_gate"]) if m["load_gate"] else 0,
                "%d/%d" % (round(m["load1"]), round(m["load_gate"])),
                "1-minute load average, queue threshold %g · click: processes"
                % m["load_gate"], load_hot + " clk",
                extra=' id="loadbar" onclick="toggleProcs()"')]
    if mem_mb:
        # 10-15: «занято = всё, кроме free» давало 99% красным при давлении
        # «норма»: кэш файлов считался занятым. Теперь как в Мониторинге
        # системы, а цвет — давление памяти по ядру. У соседа без used_mb
        # остаётся старый расчёт.
        used_mb = m.get("used_mb")
        if used_mb is None:
            used_mb = mem_mb - m["free_mb"]
            mem_cls = free_hot
            mem_tip = "used %d MB of %d, free %d" % (
                used_mb, mem_mb, m["free_mb"])
        else:
            pr = m.get("pressure") or 1
            mem_cls = " hot" if pr >= 4 else (" warm" if pr >= 2 else "")
            mem_tip = ("used %d MB of %d (apps + wired + compressed, as in "
                       "Activity Monitor; macOS frees the file cache itself). "
                       "Memory pressure: %s"
                       % (used_mb, mem_mb, {1: "normal", 2: "elevated",
                                            4: "critical"}.get(pr, str(pr))))
        mach.append(bar("Mem", used_mb * 100.0 / mem_mb,
                        _pair(used_mb, mem_mb), mem_tip, mem_cls))
        # Потолок свопа — фактический, а не размер памяти: macOS наращивает его
        # сам, и мерить своп памятью значило бы показывать больше 100%% без
        # всякого повода.
        swap_cap = m.get("swap_total_mb") or mem_mb
        mach.append(bar("Swap", m["swap_mb"] * 100.0 / swap_cap,
                        _pair(m["swap_mb"], swap_cap),
                        "swap %d MB of %d" % (m["swap_mb"], swap_cap),
                        swap_hot))
    limits_html += '<div class="limits">%s</div>' % "".join(mach)
    # Список процессов раскрывается кликом по Load: сюда JS кладёт /api/procs.
    limits_html += '<div id="procs" class="procs" hidden></div>'

    # Остаются чипами только те цифры, у которых потолка нет вовсе.
    stats_html = ('<div class="chips">'
                  '<span class="chip busy"><b>%d</b> working</span>'
                  '<span class="chip"><b>%d</b> queued</span>'
                  '<span class="chip"><b>%d</b> retired</span></div>'
                  % (m["busy"], m["queue"], m["retired"]))

    dev_opts = "".join(
        '<option value="%s"%s>%s</option>'
        % (esc(d.get("id")), " selected" if d.get("id") == cur.get("id") else "",
           esc("%s · %s" % (d.get("name") or d.get("id"), d.get("kind") or "")))
        for d in devs)

    # Шапку чата рисует питон, а не JS: логика кнопок одна и та же, и
    # раздваивать её между языками — верный способ развести их со временем.
    # «</» экранируем, иначе имя сессии с этими символами закрыло бы <script>.
    heads = dict((r["short"], row_entry(r, devs, cur)) for r in rows
                 if not (light and only) or r["short"] == only)
    rows_json = json.dumps(heads, ensure_ascii=False).replace("</", "<\\/")

    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Vibe Wars</title><link rel="icon" href="data:,"><link rel="stylesheet" href="%s"></head><body><div id="toploader"><i></i></div>
<div id="shell"><aside id="side">
<div class="devbar"><select id="devsel" onchange="switchDev(this.value)">%s</select></div>
<div id="flash"></div>
%s
%s
%s
<div class="topbar">
<button onclick="location.reload()">Refresh</button>
<button onclick="accOpen()">Account</button>
<a class="lnk hqlink" href="/play" title="Vibe HQ: the game view of these sessions">🎮 HQ</a>
<span class="acct" title="%s">%s</span>
</div>
<div id="newbox"><button class="go" onclick="newDraft()">+ New session</button></div>
<div id="sbox"><input id="sq" type="search" autocomplete="off" spellcheck="false"
 placeholder="Search all sessions" title="Names, summaries and the text of every conversation, retired included. Several words = all of them; &quot;quotes&quot; = exact phrase. Press / to focus, Esc to clear."
 oninput="sqInput()" onkeydown="sqKey(event)"><span id="sqn"></span></div>
<div id="sres" hidden></div>
<div id="sidelist">%s</div>
<footer>%s · %s:%d</footer>
</aside>
<div class="gutter" id="g1" title="Drag to resize \u00b7 double-click to reset"
 onmousedown="gutDrag(event,'side')" ondblclick="gutReset('side')"></div>
<main id="main"><div id="dropzone"><b>Drop it here</b>
<span>the file lands on the device and its path goes into the message</span></div>
<div id="drhead">
<div id="drtop"><span id="drdot" class="dot"
 title="pulses while the session is writing"></span>
<div id="drtitle">Pick a session on the left</div>
<div id="dracts"></div>
<button id="drfiles" onclick="openArtifacts()" title="Files and artifacts of this session"
 style="display:none"></button>
<select id="drmenu" class="rowopt" title="actions" onchange="menuPick(this)"
 style="display:none"></select></div>
<div id="drmid"><div id="drmeta"></div><span id="drtui"></span><span class="hsep"></span>
<label id="tgwrap"><input type="checkbox" id="tgSteps" checked
 onchange="applyFilters()"> steps</label>
<div id="drextraopts"></div></div>
<div id="drextra"></div></div>
<div id="drbody"><div id="mainempty">Sessions are listed on the left.<br>Pick one —
its conversation, context and controls appear here.</div></div>
<div id="drsend"><div id="hqst"></div><div id="netwarn"></div><div id="permbox"></div><div id="askwarn"></div><div id="attachbar"></div><div class="sendrow"><div class="composer">
<textarea id="drtext" rows="1" placeholder="Message this session — Enter to send, Shift+Enter for a new line"></textarea>
<input type="file" id="drfile" multiple style="display:none" onchange="uploadFile(this)">
<button class="iconbtn" onclick="document.getElementById('drfile').click()"
 title="Attach a file — it is saved on the computer and its path goes into the message">&#128206;</button>
<button id="drmic" class="iconbtn" onclick="toggleMic()" title="Voice input">&#127908;</button>
<button id="drstop" class="iconbtn stopbtn" onclick="stopTurn()" style="display:none"
 title="Stop the current step — the same as Esc in the terminal">&#9632;</button>
<button class="go iconbtn" onclick="sendTx()" title="Send (Enter)">&#8593;</button>
</div></div></div></main>
<div class="gutter" id="g2" title="Drag to resize \u00b7 double-click to reset"
 onmousedown="gutDrag(event,'file')" ondblclick="gutReset('file')"></div>
<aside id="filepane"><div id="fhead"><span id="fname"></span>
<button onclick="fileDownload()" title="Open in a new tab">open</button>
<button onclick="closeFile()">close</button></div>
<div id="fbody"></aside></div>
<div id="accscrim" onclick="accClose()"></div>
<div id="accmodal"><h2>Account<span class="gcount">%s</span></h2>
<p class="ctx">The guardian detects an account switch on its own and migrates
sessions without any button. These are for when it missed one — say the account
was switched while the computer was asleep.</p>
<div class="acts">
<button onclick="report()">Check migration readiness</button>
<button class="danger" onclick="migrate()">Migrate sessions to this account</button>
</div>
<div class="acts">
<input id="authmail" type="email" placeholder="other account email (optional)" size="32">
<button class="go" id="authgo" onclick="authStart()">Sign in to another account</button>
</div>
<div id="authbox" style="display:none">
<p class="ctx">1 — open the link and confirm. 2 — the browser shows a code, paste it here.</p>
<p class="ctx"><a id="authurl" class="lnk" target="_blank" rel="noreferrer">… waiting for the link</a></p>
<div class="acts">
<input id="authcode" placeholder="code from the browser" size="32">
<button class="go" id="authok" onclick="authCode()">Confirm</button>
<button onclick="authCancel()">Cancel</button>
</div>
</div>
<pre id="report" style="display:none"></pre>
<div class="acts"><button onclick="accClose()">Close</button></div></div>
<div id="bulkbar"><b><span id="bulkn">0</span> selected</b>
<button class="danger" onclick="bulk('bulk_kill','Stop and retire. Bring back with Queue in the Retired section.')">Kill</button>
<button onclick="bulk('bulk_retire','Retire — the guardian stops restoring them. Nothing is deleted.')">Retire</button>
<button class="danger" onclick="bulk('bulk_delete','DELETE for good: running ones are stopped, rows leave the panel, transcripts go to ~/.claude/rc-trash and are purged after 7 days.')">Delete</button>
<button class="go" onclick="bulk('bulk_unretire','Put back in the queue.')">Queue</button>
<button onclick="bulk('bulk_restart','Restart: stop and requeue at once. This is how model/effort apply and an input channel is granted.')">Restart</button>
<button class="go" onclick="bulk('bulk_up','Launch now, bypassing the cap. Each one holds memory and a slot.')">Launch</button>
<select id="selmodel">%s</select>
<button onclick="bulkOpt('model')">Model</button>
<select id="seleffort"><option value="">default effort</option><option>low</option><option>medium</option><option>high</option><option>xhigh</option><option>max</option></select>
<button onclick="bulkOpt('effort')">Effort</button>
<button onclick="clearPicks()">Clear</button>
</div>
<script>const ROWS=%s;const DRAFT_MODELS=%s;const UI_VER=%s;</script>
<script src="%s"></script></body></html>"""
            % (ASSET_URL["css"], dev_opts, limits_html, stats_html,
               warn, esc(m["account"]), esc(m["account"]), side,
               esc(m["ts"]), BIND, PORT, esc(m["account"]), BULK_MODEL_OPTS,
               rows_json, DRAFT_MODELS_JSON, json.dumps(UI_TAG), ASSET_URL["js"]))


# CSS and JS were inlined into every page: 250 KB of the 349 KB, re-sent on
# every load and every reload after an action. Served once under a name that
# carries their hash, so the browser keeps them until the code changes.
def _asset(body, ctype):
    raw = body.encode("utf-8")
    return {"raw": raw, "gz": gzip.compress(raw, 6), "ctype": ctype,
            "tag": hashlib.sha1(raw).hexdigest()[:12]}


ASSETS = {"js": _asset(JS, "application/javascript; charset=utf-8"),
          "css": _asset(CSS, "text/css; charset=utf-8")}
ASSET_URL = {k: "/static/panel-%s.%s" % (a["tag"], k) for k, a in ASSETS.items()}
# версия интерфейса для открытых вкладок: меняется с любым из двух файлов
UI_TAG = ASSETS["js"]["tag"] + ASSETS["css"]["tag"]


def _act_continue(target):
    """19.11 launch bay, mode «continue the best match»: pick the live or
    archived session this request continues and type it there (an archived one
    is raised by act_say). Nothing fits: launch a new one, as the plain mode
    would. Runs without the global lock (the pick asks a model, the send may
    raise a session): the send takes that session's own lock."""
    if not isinstance(target, dict):
        raise ValueError("expected an object {text}")
    text = (target.get("text") or "").strip()
    if not text:
        raise ValueError("a first prompt is required")
    pick = C.continue_pick(text)
    if pick and pick.get("uuid"):
        with _session_lock(pick["id"]):
            res = C.act_say({"id": pick["uuid"], "text": text})
        out = dict(res) if isinstance(res, dict) else {"msg": res}
        if out.get("wedged"):
            raise ValueError("picked %s «%s», but it is not reading its input: %s"
                             % (pick["id"], pick["title"], out.get("msg") or ""))
        was = {"archived": " (raised from the archive)", "idle": " (woken up)"}.get(pick["state"], "")
        out["msg"] = "continued session %s «%s»%s: %s" % (
            pick["id"], pick["title"], was, pick.get("why") or "best match")
        out["picked"] = {"id": pick["id"], "title": pick["title"],
                         "state": pick["state"], "why": pick.get("why") or ""}
        return out
    with _act_lock:
        msg = C.act_new(target)
    why = (pick or {}).get("why") or ""
    return {"msg": "no earlier session fits%s, so %s"
                   % (" (" + why + ")" if why else "", msg),
            "picked": None}


ACTIONS = {
    "continue": _act_continue,
    "kill": C.act_kill,
    "kill_requeue": C.act_kill_requeue,
    "up": C.act_up,
    "retire": C.act_retire,
    "unretire": C.act_unretire,
    "killapp": C.act_kill_app,
    "kill_empty": C.act_kill_empty,
    "retire_older": C.act_retire_older,
    "delete": C.act_delete,
    "bulk_delete": C.act_bulk_delete,
    "bulk_kill": C.act_bulk_kill,
    "bulk_retire": C.act_bulk_retire,
    "bulk_unretire": C.act_bulk_unretire,
    "bulk_restart": C.act_bulk_restart,
    "bulk_up": C.act_bulk_up,
    "set_model": C.act_set_model,
    "set_effort": C.act_set_effort,
    "auth_start": C.act_auth_start,
    "auth_code": C.act_auth_code,
    "auth_cancel": C.act_auth_cancel,
    "acct_switch": C.act_acct_switch,
    "acct_auto": C.act_acct_auto,
    "acct_forget": C.act_acct_forget,
    "preflight": lambda _t: C.preflight_text(),
    "say": C.act_say,
    "answer": C.act_answer,
    "permit": C.act_permit,
    "interrupt": C.act_interrupt,
    "set_project": C.act_set_project,
    "set_title": C.act_set_title,
    "new": C.act_new,
    "migrate": C.act_migrate,
}

_act_lock = threading.Lock()
SLOW_ACTIONS = {"transfer", "move_note", "move_prepare", "continue"}
# ⛔ Everything used to queue behind _act_lock, and `say` holds it for its whole
# delivery check — up to 25 s, 100 s during a compact. Send a message, then
# change the model or kill another session: nothing happened until the send
# had been confirmed (09-24, "switching models lags"). Registry edits are
# serialised by C.with_lock on their own; these need nothing more.
QUICK_ACTIONS = {"interrupt", "set_model", "set_effort", "set_title", "set_project", "retire",
                 "unretire", "bulk_retire", "bulk_unretire", "preflight",
                 "auth_start", "auth_code", "auth_cancel",
                 "acct_switch", "acct_auto", "acct_forget"}
# One session at a time, not one action at a time: two sends into the same
# FIFO must not interleave, and a double-clicked Launch must not start two
# claudes on one transcript. Other sessions are not held up.
SESSION_ACTIONS = {"say", "answer", "permit", "up", "kill", "kill_requeue", "delete"}
_sess_locks = {}
_sess_locks_mu = threading.Lock()


def _session_lock(target):
    key = target.get("id") if isinstance(target, dict) else target
    key = str(key or "")[:8]
    with _sess_locks_mu:
        return _sess_locks.setdefault(key, threading.Lock())


def _lock_for(action, target):
    if action in SLOW_ACTIONS or action in QUICK_ACTIONS:
        return _NULL_LOCK
    if action in SESSION_ACTIONS:
        return _session_lock(target)
    return _act_lock


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_NULL_LOCK = _NullLock()


def remote_get(url, path, timeout=15):
    import urllib.request
    with urllib.request.urlopen(url + path, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def remote_post(url, path, body, timeout=120):
    import urllib.request
    req = urllib.request.Request(url + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def dev_url(dev_id):
    """Peer URL for a device id. v0.1: the multi-machine hub is disabled (no
    peers, no session moves), so every request is served by this machine."""
    return None


# Exceptions this codebase raises on purpose: their text is written for the
# operator and is meant to reach the panel. Everything else is a real fault,
# and its text is not safe to forward — urllib puts the failing URL into the
# message, and once this process carries an account token that URL can carry
# it too. Same reason the traceback never goes out: it quotes source lines.
SPEAKABLE = (ValueError, KeyError, C.LockBusy)


def fault(where):
    """Log the traceback to stderr (launchd keeps it), return a short id."""
    eid = os.urandom(3).hex()
    sys.stderr.write("[%s] %s\n%s\n" % (eid, where, traceback.format_exc()))
    sys.stderr.flush()
    return eid


def emsg(where, exc):
    """Client-facing text: the operator message if deliberate, else an id."""
    if isinstance(exc, SPEAKABLE):
        return "%s: %s" % (type(exc).__name__, exc)
    # ⛔ 10-02: 19 «new session» clicks during a full disk all said «internal
    # error, id …». Say what it is: no click fixes it, freeing space does.
    if isinstance(exc, OSError) and exc.errno == 28:
        fault(where)
        return "Disk is full on this machine: nothing can be written, so new " \
               "sessions cannot start and open ones drop messages. Free space first."
    return "internal error — details in the log, id %s" % fault(where)


_TS_NETS = None
_TS_SUFFIX = {"v": None, "at": 0.0}


def _tailnet_suffix():
    """Our own MagicDNS suffix (e.g. «tailXXXX.ts.net»), from RC_TAILNET_SUFFIX or `tailscale status --json`.
    Retried at most once a minute while unknown, so a panel started before tailscaled still learns it."""
    v = os.environ.get("RC_TAILNET_SUFFIX", "").strip().strip(".").lower()
    if v:
        return v
    if _TS_SUFFIX["v"] or time.time() - _TS_SUFFIX["at"] < 60:
        return _TS_SUFFIX["v"]
    _TS_SUFFIX["at"] = time.time()
    import shutil
    import socket
    import subprocess
    cache = CFG.state("rc-tailnet-suffix")

    def found(s, how):
        _TS_SUFFIX["v"] = s
        sys.stderr.write("[hostcheck] tailnet suffix %s (%s)\n" % (s, how))
        try:
            with open(cache, "w") as fh:
                fh.write(s + "\n")
        except OSError:
            pass
        return s
    # ⛔ 17.11: under launchd the Mac App Store CLI (/Applications/Tailscale.app/.../Tailscale) printed NOTHING, the
    # suffix stayed unknown and https://<box>.ts.net (tailscale serve, the phone) got 403. Reverse DNS of our own
    # tailnet IP through MagicDNS answers in 10 ms in any context, so it goes first; the CLI and the cache file follow.
    try:
        name = socket.gethostbyaddr(os.environ.get("RC_PANEL_BIND") or BIND)[0].strip(".").lower()
        if name.endswith(".ts.net") and name.count(".") >= 3:
            return found(name.split(".", 1)[1], "reverse dns")
    except Exception as exc:
        sys.stderr.write("[hostcheck] reverse dns: %r\n" % (exc,))
    for cli in (shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                "/usr/bin/tailscale", "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"):
        if not cli or not os.path.exists(cli):
            continue
        try:
            out = subprocess.run([cli, "status", "--json"], capture_output=True, timeout=8).stdout
            s = (json.loads(out.decode("utf-8", "replace")).get("MagicDNSSuffix") or "").strip(".").lower()
            if s.endswith(".ts.net"):
                return found(s, cli)
        except Exception as exc:
            sys.stderr.write("[hostcheck] %s: %r\n" % (cli, exc))
    try:
        with open(cache) as fh:
            s = fh.read().strip().strip(".").lower()
        if s.endswith(".ts.net"):
            _TS_SUFFIX["v"] = s
            sys.stderr.write("[hostcheck] tailnet suffix %s (cache file)\n" % s)
            return s
    except OSError:
        pass
    return None


def _ours_host(n):
    """⛔ 17.11 audit: the old test was startswith("100.") or endswith(".ts.net"), so «100.evil.com» passed and DNS
    rebinding from any web page reached /act and /file; any other tailnet's Funnel page passed as Origin too.
    Ours now: loopback, a LITERAL Tailscale address (100.64.0.0/10, fd7a:115c:a1e0::/48; a literal IP cannot be
    rebound), or a name inside OUR tailnet's MagicDNS suffix."""
    global _TS_NETS
    n = (n or "").lower().rstrip(".")
    if n in ("127.0.0.1", "localhost", "::1"):
        return True
    extra = [h.strip().lower().rstrip(".") for h in os.environ.get("RC_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if n in extra:
        return True
    import ipaddress
    if _TS_NETS is None:
        _TS_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
    try:
        ip = ipaddress.ip_address(n)
        return any(ip.version == net.version and ip in net for net in _TS_NETS)
    except ValueError:
        pass
    suf = _tailnet_suffix()
    return bool(suf) and n.endswith("." + suf)


def _foreign_request(headers, check_origin=True):
    """⛔ 11-08 audit: /act starts unsandboxed sessions and has no login, and json.loads ignored the content type, so any
    web page open on this Mac or on a tailnet device could POST to it as text/plain, or reach it through DNS rebinding.
    Ours: a local or tailnet Host, and no Origin from anywhere else. curl, the hub and watchers send no Origin.
    What counts as ours: _ours_host (17.11, exact tailnet addresses and our own suffix only)."""
    ours = _ours_host
    try:
        host = urlparse("//" + (headers.get("Host") or "")).hostname or ""
        if host and not ours(host.lower()):
            return True
        origin = headers.get("Origin")
        if check_origin and origin is not None:
            return not ours((urlparse(origin).hostname or "").lower())
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------- auth
# Every request needs the panel token: as a cookie (set once by opening
# /?token=<token>) or as "Authorization: Bearer <token>". The token lives in
# <state>/panel-token (0600) and is created on first start. Print the login
# URL with: python3 rc_panel.py --url
AUTH_COOKIE = CFG.NAME.replace("-", "_") + "_auth"
_TOKEN = {"v": None}


def panel_token():
    if _TOKEN["v"]:
        return _TOKEN["v"]
    p = CFG.PANEL_TOKEN_FILE
    try:
        with open(p) as fh:
            t = fh.read().strip()
    except OSError:
        t = ""
    if len(t) < 20:
        import secrets
        t = secrets.token_urlsafe(32)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(t + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    _TOKEN["v"] = t
    return t


def _token_ok(cand):
    import hmac
    return bool(cand) and hmac.compare_digest(cand.encode("utf-8", "replace"), panel_token().encode())


def _req_token(headers):
    a = headers.get("Authorization") or ""
    if a.startswith("Bearer "):
        return a[7:].strip()
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == AUTH_COOKIE:
            return v.strip()
    return ""


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Vibe Wars: sign in</title>
<style>body{font:16px system-ui,sans-serif;background:#111;color:#eee;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0;padding:16px;box-sizing:border-box}
form{max-width:420px;width:100%%}input{width:100%%;box-sizing:border-box;padding:12px;font-size:16px;
margin:12px 0;border-radius:8px;border:1px solid #444;background:#222;color:#eee}
button{padding:12px 20px;font-size:16px;border-radius:8px;border:0;background:#4a7cff;color:#fff}
p{color:#aaa;line-height:1.4}</style></head><body><form method="post" action="/login">
<h2>Vibe Wars</h2><p>Paste the access token. It is in <code>~/.vibe-wars/panel-token</code>
on the computer, or ask Claude Code there for your Vibe Wars link.</p><p style="color:#f77">%s</p>
<input type="password" name="token" autocomplete="current-password" autofocus>
<input type="hidden" name="next" value="%s"><button type="submit">Open</button></form></body></html>"""

MAX_JSON = 1024 * 1024
MAX_VOICE = 25 * 1024 * 1024
MAX_UPLOAD = int(os.environ.get("RC_MAX_UPLOAD_MB") or 200) * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "rc-panel"
    # ⛔ HTTP/1.0 opened a new connection for every poll: one extra round trip
    # each time, and the link from abroad to the main Mac is 70-250 ms. With
    # 1.1 the browser keeps its connections; every response here carries a
    # Content-Length, which keep-alive requires. Idle ones are dropped after
    # `timeout` so they do not pin threads forever.
    protocol_version = "HTTP/1.1"
    timeout = 120

    def log_message(self, fmt, *a):
        pass  # the access log is noise; real errors go to stderr via launchd

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        raw = body.encode("utf-8")
        # State, side and transcript are JSON/HTML that shrink 5-8x: the page
        # went 349 KB -> ~20 KB, a long chat 350 KB -> ~60 KB.
        gz = len(raw) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            raw = gzip.compress(raw, 5)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _cookie_hdr(self, tok):
        return "%s=%s; Path=/; Max-Age=34560000; HttpOnly; SameSite=Lax" % (AUTH_COOKIE, tok)

    def _auth_gate(self, method):
        """True when the request may go on; otherwise the answer is already sent."""
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if method == "GET" and q.get("token"):
            tok = q["token"][0]
            if _token_ok(tok):
                rest = "&".join(p for p in u.query.split("&") if not p.startswith("token="))
                self._send(302, "", extra={"Location": (u.path or "/") + ("?" + rest if rest else ""),
                                           "Set-Cookie": self._cookie_hdr(tok)})
                return False
        if method == "POST" and u.path == "/login":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                self._send(413, "too large", "text/plain; charset=utf-8")
                return False
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            tok = (form.get("token") or [""])[0].strip()
            nxt = (form.get("next") or ["/"])[0]
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/"
            if _token_ok(tok):
                self._send(303, "", extra={"Location": nxt, "Set-Cookie": self._cookie_hdr(tok)})
            else:
                self._send(401, LOGIN_PAGE % ("Wrong token.", esc(nxt)))
            return False
        if _token_ok(_req_token(self.headers)):
            return True
        accept = self.headers.get("Accept") or ""
        if method == "GET" and "text/html" in accept:
            self._send(401, LOGIN_PAGE % ("", esc(u.path or "/")))
        else:
            self._send(401, '{"ok":false,"msg":"unauthorized"}', "application/json; charset=utf-8")
        return False

    def do_GET(self):
        if _foreign_request(self.headers, check_origin=False):
            self._send(403, '{"ok":false,"msg":"forbidden"}', "application/json; charset=utf-8")
            return
        if not self._auth_gate("GET"):
            return
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        dev = (q.get("dev") or [""])[0]
        url = dev_url(dev)
        if url and path in ("/api/state", "/transcript", "/api/auth", "/api/move_check",
                            "/act_result", "/api/search",
                            "/api/procs"):
            # Проксируем как есть: сосед отдаёт ровно тот же формат, разбирать
            # и пересобирать его тут — только повод разъехаться версиям.
            try:
                body = remote_get(url, self.path.replace("dev=" + dev, "dev="))
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:
                self._send(200, json.dumps(
                    {"ok": False, "msg": "%s unreachable: %s"
                     % (dev, str(exc)[:120])}, ensure_ascii=False),
                    "application/json; charset=utf-8")
            return
        if path == "/":
            try:
                embed = (q.get("embed") or [""])[0] == "1"
                doc = page(dev, (q.get("sort") or [""])[0], light=embed,
                           only=((q.get("s") or [""])[0][:8] or None) if embed else None)
                if embed:
                    doc = embed_page(doc)
                self._send(200, doc)
            except Exception:
                self._send(500, "<pre>panel failed to render — id %s</pre>"
                           % fault("GET /"))
        elif path == "/transcript":
            pref = (q.get("uuid") or [""])[0]
            try:
                out = {"ok": True}
                out.update(C.transcript(pref, (q.get("limit") or [C.TX_LIMIT])[0],
                                        (q.get("sig") or [""])[0]))
            except Exception as exc:
                out = {"ok": False, "msg": emsg("GET /transcript", exc)}
            self._send(200, json.dumps(out, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path == "/api/row":
            # HQ: a chat for a session launched after the page loaded opens at
            # once and takes its header from here, instead of reloading the page
            try:
                sid = (q.get("id") or [""])[0][:8]
                devs = C.devices()
                snap = GAME.snapshot_cached() if GAME is not None else C.snapshot()
                row = next((r for r in snap["rows"] if r["short"] == sid), None)
                out = {"ok": bool(row), "row": row_entry(row, devs, devs[0]) if row else None}
            except Exception as exc:
                out = {"ok": False, "msg": emsg("GET /api/row", exc)}
            self._send(200, json.dumps(out, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path == "/api/auth":
            try:
                out = C.auth_state()
            except Exception as exc:
                out = {"running": False, "url": "", "account": "",
                       "switched": False, "tail": emsg("GET /api/auth", exc)}
            self._send(200, json.dumps(out, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path.startswith("/static/panel-"):
            a = ASSETS.get(path.rsplit(".", 1)[-1])
            if not a or path != ASSET_URL.get(path.rsplit(".", 1)[-1]):
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
            raw = a["gz"] if gz else a["raw"]
            self.send_response(200)
            self.send_header("Content-Type", a["ctype"])
            self.send_header("Content-Length", str(len(raw)))
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            # the name changes with the content, so it never needs revalidating
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(raw)
            return
        elif path == "/side":
            try:
                self._send(200, side_for((q.get("dev") or [""])[0],
                                         (q.get("sort") or [""])[0]),
                           "text/html; charset=utf-8")
            except Exception as exc:
                self._send(500, emsg("GET /side", exc),
                           "text/plain; charset=utf-8")
            return
        elif path == "/play" and GAME is not None:
            try:
                self._send(200, play_page())
            except Exception:
                self._send(500, "<pre>HQ failed to render — id %s</pre>" % fault("GET /play"))
        elif path == "/api/game" and GAME is not None:
            try:
                fl = (q.get("fleet") or [""])[0]
                since = int((q.get("since") or ["0"])[0] or 0)
                if url:
                    # 17.11 the owner: «не вижу переключения на девайсы ... хочу заходить в акки и менять их на двух
                    # других машинах». Peers run the older panel without /api/game: HQ gets their /api/state through
                    # here, dressed by rc_game; every /act and /api/auth already goes to them with dev.
                    out = GAME.api(since, fleet=None, snap=json.loads(remote_get(url, "/api/state", 20)))
                else:
                    out = GAME.api(since, fleet=int(fl) if fl.isdigit() else None)
                out["dev"] = dev if url else "local"
                out["devices"] = [{"id": d.get("id") or "", "name": d.get("name") or d.get("id") or "?",
                                   "kind": d.get("kind") or ""} for d in C.devices()]
                # the page reloads itself when its own script changed
                out["ui"] = next((u for u in play_assets() if u.endswith(".js")), "")
                game_diet(out, (q.get("have") or [""])[0])
            except Exception as exc:
                out = {"ok": False, "msg": emsg("GET /api/game", exc)}
            self._send(200, json.dumps(out, ensure_ascii=False),
                       "application/json; charset=utf-8", {"X-Panel-UI": UI_TAG})
        elif path.startswith("/static/play-"):
            a = play_assets().get(path)
            if not a:
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
            raw = a["gz"] if gz else a["raw"]
            self.send_response(200)
            self.send_header("Content-Type", a["ctype"])
            self.send_header("Content-Length", str(len(raw)))
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(raw)
            return
        elif path == "/act_result":
            self._send(200, json.dumps(nonce_state((q.get("nonce") or [""])[0]),
                                       ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path == "/api/search":
            # 12.11 «поиск по всем сессиям»: names, summaries and the text of
            # every conversation, retired ones included (rc_search.py).
            try:
                if SEARCH is None:
                    raise RuntimeError("rc_search is not installed on this device")
                snap = GAME.snapshot_cached() if GAME is not None else C.snapshot()
                out = SEARCH.search((q.get("q") or [""])[0][:200], snap["rows"])
            except Exception as exc:
                out = {"ok": False, "msg": emsg("GET /api/search", exc)}
            self._send(200, json.dumps(out, ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path == "/api/state":
            snap = GAME.snapshot_cached() if GAME is not None else C.snapshot()
            if (q.get("lite") or [""])[0] == "1":
                snap = lite_state(snap)
            self._send(200, json.dumps(snap, ensure_ascii=False),
                       "application/json; charset=utf-8",
                       {"X-Panel-UI": UI_TAG})
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        # Several branches below answer before reading the body (401, too
        # large, not found). On a kept-alive connection those unread bytes
        # would be parsed as the next request; POSTs are rare, so they close.
        self.close_connection = True
        if _foreign_request(self.headers):
            self._send(403, '{"ok":false,"msg":"forbidden"}', "application/json; charset=utf-8")
            return
        if not self._auth_gate("POST"):
            return
        # Body size: required and capped per route, refused before reading.
        p0 = urlparse(self.path).path
        cap = {"/upload": MAX_UPLOAD, "/voice": MAX_VOICE}.get(p0, MAX_JSON)
        try:
            n0 = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            n0 = -1
        if n0 < 0 or n0 > cap:
            self._send(413 if n0 > cap else 411, '{"ok":false,"msg":"body too large or no length"}',
                       "application/json; charset=utf-8")
            return
        # Funnel отдаёт весь сайт на 8443 в эту ручку и дописывает путь
        # запроса: POST на «/» приходит как «/ingest/», на «/api/state» — как
        # «/ingest/api/state». Принимаем всё поддерево, иначе снаружи вместо
        # честного 401 приходило «not found».
        if urlparse(self.path).path == "/diag":
            # Короткая строка от страницы (txDiag): браузер и состояние ленты
            # после открытия чата. Только для разбора «белого поля».
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                d = json.loads(self.rfile.read(n) or b"{}")
                path = CFG.state("rc-panel-diag.log")
                if os.path.exists(path) and os.path.getsize(path) > 1024 * 1024:
                    os.replace(path, path + ".1")
                with open(path, "a") as fh:
                    fh.write("%s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                              self.client_address[0],
                                              json.dumps(d, ensure_ascii=False)[:1500]))
            except Exception:
                pass
            self._send(200, '{"ok":true}', "application/json; charset=utf-8")
            return
        if urlparse(self.path).path == "/voice":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > C.VOICE_MAX:
                    raise ValueError("recording too large")
                q = parse_qs(urlparse(self.path).query)
                got = C.transcribe(self.rfile.read(n) if n else b"",
                                   (q.get("name") or ["voice.webm"])[0])
                out = {"ok": True}
                out.update(got)
                if GAME is not None:
                    GAME.on_voice(True)
            except Exception as exc:
                out = {"ok": False, "msg": emsg("POST /voice", exc)}
            self._send(200, json.dumps(out, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        if urlparse(self.path).path == "/upload":
            # Сырое тело, а не multipart: разбирать его в BaseHTTPRequestHandler
            # руками — лишний код и лишний способ ошибиться, а браузеру всё
            # равно, чем слать один файл.
            try:
                q = parse_qs(urlparse(self.path).query)
                n = int(self.headers.get("Content-Length") or 0)
                if n > C.UPLOAD_MAX:
                    raise ValueError("file too large")
                blob = self.rfile.read(n) if n else b""
                # ⛔ 10-15: a connection cut mid-body gave rfile.read() fewer
                # bytes, and the stub was saved as the file. The page retries.
                if len(blob) != n:
                    raise ValueError("upload cut off at %d of %d bytes" % (len(blob), n))
                want = int((q.get("size") or ["-1"])[0] or -1)
                if want >= 0 and want != len(blob):
                    raise ValueError("upload cut off at %d of %d bytes" % (len(blob), want))
                # A retry whose first try did land (only the answer was lost)
                # gets the same path back instead of a «-1» copy of the file.
                key = (q.get("key") or [""])[0][:80]
                dest = _upload_seen(key, len(blob))
                if not dest:
                    dest = C.save_upload((q.get("uuid") or [""])[0],
                                         (q.get("name") or [""])[0], blob)
                    _upload_remember(key, dest, len(blob))
                out = {"ok": True, "path": dest}
            except Exception as exc:
                out = {"ok": False, "msg": emsg("POST /upload", exc)}
            self._send(200, json.dumps(out, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        if urlparse(self.path).path != "/act":
            self._send(404, '{"ok":false,"msg":"not found"}',
                       "application/json; charset=utf-8")
            return
        t0 = time.time()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) or b"{}"
            req = json.loads(raw)
            # HQ scores what the owner does: a browser, not the planner or a watcher
            # script posting to /act (rc_game.on_act)
            req["_web"] = "Mozilla" in (self.headers.get("User-Agent") or "")
            url = dev_url(req.get("dev") or "")
            if url:
                # Действие исполняет тот, кому принадлежит сессия: только у него
                # есть её процесс, FIFO и реестр.
                req.pop("dev", None)
                body = remote_post(url, "/act",
                                   json.dumps(req, ensure_ascii=False).encode("utf-8"))
                self._send(200, body, "application/json; charset=utf-8")
                return
            fn = ACTIONS.get(req.get("action"))
            if not fn:
                raise ValueError("unknown action %r" % req.get("action"))
            replay = _nonce_claim(req)
            if replay is not None:
                act_log(req, replay, t0)
                self._send(200, json.dumps(replay, ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            # one mutation at a time: two concurrent restores would fight over
            # the guardian lock and one would lose its registry write.
            # Долгие действия переезда (ждут соседа или TUI минутами) идут без
            # общего лока: реестр они правят под локом гардиана сами.
            with _lock_for(req.get("action"), req.get("target")):
                msg = fn(req.get("target"))
            # Действие может вернуть словарь, когда одного текста мало: так
            # act_say помечает доставку, которую не удалось подтвердить.
            if isinstance(msg, dict):
                out = {"ok": True}
                out.update(msg)
            else:
                out = {"ok": True, "msg": msg}
        except Exception as exc:
            out = {"ok": False, "msg": emsg("POST /act", exc)}
        _nonce_done(locals().get("req"), out)
        act_log(locals().get("req"), out, t0)
        self._send(200, json.dumps(out, ensure_ascii=False),
                   "application/json; charset=utf-8")


# ⛔ 10-14: the MacBook reaches the mini through a Tailscale DERP relay, and the
# answer to a send was lost on the way: the server had delivered the message
# in 2 s, Safari said «TypeError: Load failed» 8-20 s later, the page put the
# text back in the box, and the owner sent it again (and again). A send now carries
# a nonce; the page retries a lost answer with the SAME nonce, and the server
# replays the first result instead of typing the message a second time.
# ⛔ 10-15: the direct Tailscale path to the MacBook lost 60% of its packets.
# A try waited up to 170 s on a dead connection, and a try that never reached
# the server could only be retried blind. The page now asks /act_result by
# nonce with short timeouts, so the server keeps a nonce for as long as a page
# may come back for it (an outbox survives reloads), panel restarts included.
NONCE_TTL_S = 6 * 3600
NONCE_FILE = CFG.state("rc-panel-nonces.json")
_NONCES = {}
_NONCES_MU = threading.Lock()
_NONCE_SAVE_MU = threading.Lock()


def _nonce_persist():
    with _NONCES_MU:
        snap = dict((k, {"t": v["t"], "out": v["out"]}) for k, v in _NONCES.items())
    try:
        with _NONCE_SAVE_MU:
            tmp = NONCE_FILE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(snap, fh, ensure_ascii=False)
            os.replace(tmp, NONCE_FILE)
    except OSError:
        pass


def _nonce_restore():
    """Nonces from before a restart. One that was still being delivered when
    the panel went down may or may not have reached the session: it answers
    «unconfirmed», which keeps the bubble with put-back/dismiss instead of
    letting the page type it again."""
    try:
        with open(NONCE_FILE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    now = time.time()
    with _NONCES_MU:
        for k, v in data.items():
            if not isinstance(v, dict) or now - (v.get("t") or 0) > NONCE_TTL_S:
                continue
            out = v.get("out")
            if out is None:
                out = {"ok": True, "unconfirmed": True,
                       "msg": "the panel restarted while this send was being delivered "
                              "— check the chat before sending it again"}
            ev = threading.Event()
            ev.set()
            _NONCES[k] = {"ev": ev, "out": out, "t": v.get("t") or now}


def nonce_state(nonce):
    """What /act_result answers: done (with the result), running, or unknown."""
    nonce = str(nonce or "")[:80]
    with _NONCES_MU:
        ent = _NONCES.get(nonce) if nonce else None
        if ent is None:
            return {"ok": True, "state": "unknown"}
        if ent["out"] is None:
            return {"ok": True, "state": "running"}
        return {"ok": True, "state": "done", "out": dict(ent["out"])}


def _nonce_of(req):
    # «new» too (10-16): HQ's launch bay retries a lost answer with the same
    # nonce, and a second «new» would launch a second session.
    # ⛔ 19.11 «continue» too: without it the bay's re-post (every 6 s of
    # «unknown») ran the pick and the send again, and the test session got the
    # same order 4 times while it was being raised from the archive.
    if not isinstance(req, dict) or req.get("action") not in ("say", "new", "continue"):
        return ""
    return str(req.get("nonce") or "")[:80]


def _nonce_claim(req):
    """None = first attempt, go on. A dict = the stored result of this send."""
    nonce = _nonce_of(req)
    if not nonce:
        return None
    now = time.time()
    with _NONCES_MU:
        for k in [k for k, v in _NONCES.items() if now - v["t"] > NONCE_TTL_S]:
            _NONCES.pop(k, None)
        ent = _NONCES.get(nonce)
        if ent is None:
            _NONCES[nonce] = {"ev": threading.Event(), "out": None, "t": now}
            fresh = True
        else:
            fresh = False
    if fresh:
        _nonce_persist()
        return None
    # the first attempt may still be confirming delivery: wait for its answer
    ent["ev"].wait(170)
    # ok + unconfirmed, not a failure: a failure puts the text back in the box,
    # and the first try may well deliver it a second later
    out = dict(ent["out"] or {"ok": True, "unconfirmed": True,
                              "msg": "the first try of this send is still running "
                                     "— check the chat before resending"})
    out["replayed"] = True
    return out


def _nonce_done(req, out):
    nonce = _nonce_of(req)
    if not nonce:
        return
    with _NONCES_MU:
        ent = _NONCES.get(nonce)
    if ent is not None and ent["out"] is None:
        ent["out"] = dict(out)
        ent["ev"].set()
        _nonce_persist()


# upload key -> (path, size, time): see /upload
_UPLOADS = {}
_UPLOADS_MU = threading.Lock()


def _upload_seen(key, size):
    if not key:
        return ""
    with _UPLOADS_MU:
        now = time.time()
        for k in [k for k, v in _UPLOADS.items() if now - v[2] > 3600]:
            _UPLOADS.pop(k, None)
        ent = _UPLOADS.get(key)
    if ent and ent[1] == size and os.path.exists(ent[0]):
        return ent[0]
    return ""


def _upload_remember(key, path, size):
    if key:
        with _UPLOADS_MU:
            _UPLOADS[key] = (path, size, time.time())


# ⛔ 10-14: the owner saw "send error" twice while both messages were in the
# transcript, and nothing on the server said what the panel had answered. Every
# /act result is logged now; the page reports its own send failures to /diag.
ACT_LOG = CFG.state("rc-panel-act.log")


def act_log(req, out, t0):
    if GAME is not None:
        GAME.on_act(req, out)
    try:
        if not isinstance(req, dict):
            req = {}
        tgt = req.get("target")
        tid = (tgt.get("id") if isinstance(tgt, dict) else tgt) or ""
        line = "%s %s %s %s %.1fs %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), req.get("action"), str(tid)[:8],
            "ok" if out.get("ok") else "FAIL", time.time() - t0,
            " ".join(str(out.get("msg") or "").split())[:200]
            + (" [unconfirmed]" if out.get("unconfirmed") else ""))
        if os.path.exists(ACT_LOG) and os.path.getsize(ACT_LOG) > 2 << 20:
            os.replace(ACT_LOG, ACT_LOG + ".1")
        with open(ACT_LOG, "a") as fh:
            fh.write(line)
    except Exception:
        pass


# --- HQ (/play) ------------------------------------------------------------
PLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "play")
FONT_LINK = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
             '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=Press+Start+2P&display=swap">')
_PLAY = {"mtime": None, "assets": {}, "html": ""}


def play_assets():
    """play.css / play.js / play.html from PLAY_DIR, re-read when they change,
    served under a hashed name like the classic assets."""
    try:
        mt = tuple(os.path.getmtime(os.path.join(PLAY_DIR, f))
                   for f in ("play.css", "play.js", "play.html"))
    except OSError:
        return _PLAY["assets"]
    if mt != _PLAY["mtime"]:
        assets = {}
        urls = {}
        for f, ctype in (("play.css", "text/css; charset=utf-8"),
                         ("play.js", "application/javascript; charset=utf-8")):
            with open(os.path.join(PLAY_DIR, f), encoding="utf-8") as fh:
                a = _asset(fh.read(), ctype)
            url = "/static/play-%s.%s" % (a["tag"], f.rsplit(".", 1)[1])
            assets[url] = a
            urls[f] = url
        with open(os.path.join(PLAY_DIR, "play.html"), encoding="utf-8") as fh:
            doc = fh.read()
        _PLAY["html"] = (doc.replace("{{FONTS}}", FONT_LINK)
                         .replace("{{CSS}}", urls["play.css"])
                         .replace("{{JS}}", urls["play.js"]))
        _PLAY["assets"] = assets
        _PLAY["mtime"] = mt
    return _PLAY["assets"]


def play_page():
    play_assets()
    return _PLAY["html"].replace("{{UI_VER}}", json.dumps(UI_TAG))


# The page polls /api/state every 6 s. In full it is 270 KB (90 KB gzipped),
# and 245 KB of that are the retired rows' digests the poll never reads. On a
# lossy link it queued in front of sends on the same connection (10-15).
_LITE_DIGEST = ("waiting", "ctx", "ctx_max", "model_id", "effort_now", "ts")
_LITE_RETIRED = ("uuid", "short", "live", "retired", "state", "la_ts", "busy", "busy_age",
                 "tui", "title")


def lite_state(snap):
    rows = []
    for r in snap.get("rows") or []:
        if r.get("retired") and not r.get("live"):
            x = dict((k, r.get(k)) for k in _LITE_RETIRED)
            x["digest"] = {}
        else:
            x = dict(r)
            x.pop("summary", None)
            dg = r.get("digest") or {}
            x["digest"] = dict((k, dg.get(k)) for k in _LITE_DIGEST if k in dg)
        rows.append(x)
    out = dict(snap)
    out["rows"] = rows
    return out


def embed_page(doc):
    """The classic page as the chat pane of HQ: no session column, game skin."""
    return doc.replace('<html lang="en">', '<html lang="en" class="embed game">', 1)


def main():
    _nonce_restore()
    C.usage_refresher()          # keeps the limit tile fresh off the request path
    C.digest_refresher()         # same deal for the per-row «на чём закончили»
    C.summary_refresher()        # пересказ «чем занята» — тоже мимо запроса
    if GAME is not None:
        GAME.watcher()           # XP, quests, achievements for /play
    if SEARCH is not None:
        # first build is ~7 s; do it now, not on the first search
        threading.Thread(target=SEARCH.update, daemon=True).start()
    # 17.11 host check: learn our tailnet suffix now, not on the phone's first request
    threading.Thread(target=_tailnet_suffix, daemon=True).start()

    # Default bind is 127.0.0.1 only; a phone reaches it through
    # `tailscale serve`. Binding another address is an explicit choice.
    servers = [("127.0.0.1", PORT)]
    if BIND not in ("127.0.0.1", "localhost"):
        servers.append((BIND, PORT))
    for i, addr in enumerate(servers):
        srv = ThreadingHTTPServer(addr, Handler)
        srv.daemon_threads = True
        if i < len(servers) - 1:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
    panel_token()
    sys.stderr.write("panel on %s (open the URL from: python3 rc_panel.py --url)\n"
                     % ", ".join("http://%s:%d" % a for a in servers))
    sys.stderr.flush()
    srv.serve_forever()


def print_url():
    t = panel_token()
    print("http://127.0.0.1:%d/?token=%s" % (PORT, t))
    for h in [x.strip() for x in os.environ.get("RC_ALLOWED_HOSTS", "").split(",") if x.strip()]:
        print("https://%s/?token=%s" % (h, t))


if __name__ == "__main__":
    if "--url" in sys.argv[1:]:
        print_url()
    else:
        main()
