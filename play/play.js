'use strict';
/* HQ — the game face of the RC panel.
 *
 * Every Claude session is an agent on the squad. The server (rc_game.py) keeps
 * the score: XP for commands, finished turns, commits and deploys, daily
 * goals of the day (from the day planner), trophies. This page polls /api/game, draws the board and the HUD,
 * and plays the juice. The chat itself is the classic panel in an iframe
 * (/?embed=1), so sending keeps its retries, uploads, voice and question menus.
 */

const $ = (s, el) => (el || document).querySelector(s);
const NOW = () => Date.now() / 1000;
const COMPACTING = {};  // sid → when /compact was sent from the stage bar
const LS = {
  get(k, d) { try { const v = localStorage.getItem('hq.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('hq.' + k, JSON.stringify(v)); } catch (e) {} },
};
const MY_UI = (function () { try { return new URL(document.currentScript.src).pathname; } catch (e) { return ''; } })();

let G = null;            // last /api/game answer
let SEQ = 0;             // last event id seen
let FIRST = true;
let CUR = null;          // open agent short id, or 'new'
let ORDER = [];          // board order of live agents, for 1-9 and N
let STATE = {};          // sid -> lane key
let SEEN = LS.get('seen', {});
let SND = LS.get('snd', true);
let NTF = LS.get('ntf', false);
let SLEEP_OPEN = LS.get('sleepOpen', false);
let LAST_LV = null, LAST_RANK = null;
let IFRAME_READY = false;
let POLL_T = null;
let FLOW = { v: 0, at: NOW() };
let UI_NOTED = false;
const CARD = new Map();
// the fleet screen (G): trails by agent, the last trail version seen, the open filter
const FLEET = { on: false, v: 0, tr: {}, filter: '', tiles: new Map(), tick: null, ret: false, n: -1, min: 0, req: 0, got: 0 };

// ------------------------------------------------------------------ helpers
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, NOW() - ts);
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}
function dur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
}
function hhmm(ts) { const d = new Date(ts * 1000); return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
function tok(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n || 0); }
function rowBy(sid) {
  if (!G) return null;
  return G.rows.find(r => r.short === sid) || G.sleeping.find(r => r.short === sid) || null;
}
function agentBy(sid) { return (G && G.agents[sid]) || null; }
function titleOf(r) { return (r && (r.title || r.name || r.short)) || ''; }
function modelLabel(r) {
  const id = (r && (r.model_id || r.model)) || '';
  const m = /claude-([a-z]+)-(\d+)-(\d+)/.exec(id);
  const name = m ? m[1].charAt(0).toUpperCase() + m[1].slice(1) + ' ' + m[2] + '.' + m[3] : (id || 'Claude');
  const eff = (r && r.effort) || '';
  return name + (eff ? ' · ' + eff : '');
}

// Pixel icons. Most are pixelarticons 1.8.1 (MIT, (c) 2019 Gerrit Halfmann,
// https://github.com/halfmage/pixelarticons); question, flame, rocket, mic, star
// and bot are drawn for HQ on the same 12x12 grid of 2 px cells.
const PXI = {"user":"M15 2H9v2H7v6h2V4h6V2zm0 8H9v2h6v-2zm0-6h2v6h-2V4zM4 16h2v-2h12v2H6v4h12v-4h2v6H4v-6z","login":"M5 3H3v4h2V5h14v14H5v-2H3v4h18V3H5zm12 8h-2V9h-2V7h-2v2h2v2H3v2h10v2h-2v2h2v-2h2v-2h2v-2z","external-link":"M21 11V3h-8v2h4v2h-2v2h-2v2h-2v2H9v2h2v-2h2v-2h2V9h2V7h2v4h2zM11 5H3v16h16v-8h-2v6H5V7h6V5z","card-id":"M2 4h20v16H2V4zm2 2v4h16V6H4zm16 6H10v2h10v-2zm0 4h-4v2h4v-2zm-6 2v-2H4v2h10zM4 14h4v-2H4v2z","repeat":"M11 1H9v2h2v2H5v2H3v10h2v2h2v-2H5V7h6v2H9v2h2V9h2V7h2V5h-2V3h-2V1zm8 4h-2v2h2v10h-6v-2h2v-2h-2v2h-2v2H9v2h2v2h2v2h2v-2h-2v-2h6v-2h2V7h-2V5z","alert":"M13 1h-2v2H9v2H7v2H5v2H3v2H1v2h2v2h2v2h2v2h2v2h2v2h2v-2h2v-2h2v-2h2v-2h2v-2h2v-2h-2V9h-2V7h-2V5h-2V3h-2V1zm0 2v2h2v2h2v2h2v2h2v2h-2v2h-2v2h-2v2h-2v2h-2v-2H9v-2H7v-2H5v-2H3v-2h2V9h2V7h2V5h2V3h2zm0 4h-2v6h2V7zm0 8h-2v2h2v-2z","archive":"M22 4H2v6h2v10h16V10h2V4zM6 10h12v8H6v-8zm14-4v2H4V6h16zm-5 6H9v2h6v-2z","arrow-right":"M4 11v2h12v2h2v-2h2v-2h-2V9h-2v2H4zm10-4h2v2h-2V7zm0 0h-2V5h2v2zm0 10h2v-2h-2v2zm0 0h-2v2h2v-2z","arrow-up":"M11 20h2V8h2V6h-2V4h-2v2H9v2h2v12zM7 10V8h2v2H7zm0 0v2H5v-2h2zm10 0V8h-2v2h2zm0 0v2h2v-2h-2z","article":"M5 3H3v18h18V3H5zm14 2v14H5V5h14zm-2 2H7v2h10V7zM7 11h10v2H7v-2zm7 4H7v2h7v-2z","briefcase":"M8 3h8v4h6v14H2V7h6V3zm2 4h4V5h-4v2zM4 9v10h16V9H4z","bullseye":"M18 2H6v2H4v2H2v12h2v2h2v2h12v-2h2v-2h2V6h-2V4h-2V2zm0 2v2h2v12h-2v2H6v-2H4V6h2V4h12zm-8 6h4v4h-4v-4zM8 6h8v2H8V6zm0 10H6V8h2v8zm8 0v2H8v-2h8zm0 0h2V8h-2v8z","cellular-signal-1":"M16 4v16h6V4h-6Zm2 2h2v12h-2V6Zm-9 4v10h6V10H9Zm2 8v-6h2v6h-2Zm-3-4H2v6h6v-6Z","cellular-signal-off":"M4 2H2v2h2v2H2v2h2V6h2v2h2V6H6V4h2V2H6v2H4V2Zm12 2v16h6V4h-6Zm2 2h2v12h-2V6Zm-9 4v10h6V10H9Zm2 8v-6h2v6h-2Zm-3-4v6H2v-6h6Zm-2 4v-2H4v2h2Z","chat":"M20 2H2v20h2V4h16v12H6v2H4v2h2v-2h16V2h-2z","check":"M18 6h2v2h-2V6zm-2 4V8h2v2h-2zm-2 2v-2h2v2h-2zm-2 2h2v-2h-2v2zm-2 2h2v-2h-2v2zm-2 0v2h2v-2H8zm-2-2h2v2H6v-2zm0 0H4v-2h2v2z","chevron-left":"M16 5v2h-2V5h2zm-4 4V7h2v2h-2zm-2 2V9h2v2h-2zm0 2H8v-2h2v2zm2 2v-2h-2v2h2zm0 0h2v2h-2v-2zm4 4v-2h-2v2h2z","clock":"M19 3H5v2H3v14h2v2h14v-2h2V5h-2V3zm0 2v14H5V5h14zm-8 2h2v6h4v2h-6V7z","close-box":"M5 3H3v18h18V3H5zm14 2v14H5V5h14zm-8 4H9V7H7v2h2v2h2v2H9v2H7v2h2v-2h2v-2h2v2h2v2h2v-2h-2v-2h-2v-2h2V9h2V7h-2v2h-2v2h-2V9z","close":"M5 5h2v2H5V5zm4 4H7V7h2v2zm2 2H9V9h2v2zm2 0h-2v2H9v2H7v2H5v2h2v-2h2v-2h2v-2h2v2h2v2h2v2h2v-2h-2v-2h-2v-2h-2v-2zm2-2v2h-2V9h2zm2-2v2h-2V7h2zm0 0V5h2v2h-2z","coin":"M6 2h12v2H6V2zM4 6V4h2v2H4zm0 12V6H2v12h2zm2 2v-2H4v2h2zm12 0v2H6v-2h12zm2-2v2h-2v-2h2zm0-12h2v12h-2V6zm0 0V4h-2v2h2zm-9-1h2v2h3v2h-6v2h6v6h-3v2h-2v-2H8v-2h6v-2H8V7h3V5z","command":"M4 2H2v8h2V2zm16 0h2v8h-2V2zm-6 6h-4V2H4v2h4v4H4v2h4v4H4v2h4v4H4v2h6v-6h4v6h2v-6h4v-2h-4v-4h4V8h-4V2h-2v6zm-4 6v-4h4v4h-4zM20 2h-4v2h4V2zM2 14h2v8H2v-8zm14 6h4v2h-4v-2zm6-6h-2v8h2v-8z","dashboard":"M3 3h8v10H3V3zm2 2v6h4V5H5zm8-2h8v6h-8V3zm2 2v2h4V5h-4zm-2 6h8v10h-8V11zm2 2v6h4v-6h-4zM3 15h8v6H3v-6zm2 2v2h4v-2H5z","debug-stop":"M6 2h2v2H6V2Zm10 2h-2v2h-4V4H8v2H6v3H4V7H2v2h2v2h2v2H2v2h4v2H4v2H2v2h2v-2h2v3h8v-2H8V8h8v6h2v-3h2V9h2V7h-2v2h-2V6h-2V4Zm0 0V2h2v2h-2Zm-6 7h4v2h-4v-2Zm4 4h-4v2h4v-2Zm8 1h-6v6h6v-6Z","dice":"M5 3H3v18h18V3H5zm14 2v14H5V5h14zM9 7H7v2h2V7zm6 0h2v2h-2V7zm-6 8H7v2h2v-2zm6 0h2v2h-2v-2zm-2-4h-2v2h2v-2z","download":"M13 17V3h-2v10H9v-2H7v2h2v2h2v2h2zm8 2v-4h-2v4H5v-4H3v6h18v-2zm-8-6v2h2v-2h2v-2h-2v2h-2z","eye":"M8 6h8v2H8V6zm-4 4V8h4v2H4zm-2 2v-2h2v2H2zm0 2v-2H0v2h2zm2 2H2v-2h2v2zm4 2H4v-2h4v2zm8 0v2H8v-2h8zm4-2v2h-4v-2h4zm2-2v2h-2v-2h2zm0-2h2v2h-2v-2zm-2-2h2v2h-2v-2zm0 0V8h-4v2h4zm-10 1h4v4h-4v-4z","file-alt":"M21 22H3V2h12v2h2v2h2v2h2v14zM17 6h-2v2h2V6zM5 4v16h14V10h-6V4H5zm8 12H7v2h6v-2zm-6-4h10v2H7v-2zm4-4H7v2h4V8z","file":"M3 22h18V8h-2V6h-2v2h-2V6h2V4h-2V2H3v20zm2-2V4h8v6h6v10H5z","flag":"M3 2h10v2h8v14H11v-2H5v6H3V2zm2 12h8v2h6V6h-8V4H5v10z","gift":"M19 12v8h-7m7-8h2V8h-3m1 4H5m13-4V4h-6m6 4H6m0 0V4h6M6 8H3v4h2m0 0v8h7m0 0V4","git-commit":"M7 7h10v4h5v2h-5v4H7v-4H2v-2h5V7zm2 2v6h6V9H9z","group":"M3 3h18v18H3V3zm2 2v14h14V5H5zm2 2h4v4H7V7zm6 0h4v4h-4V7zm-6 6h4v4H7v-4zm6 0h4v4h-4v-4z","home":"M14 2h-4v2H8v2H6v2H4v2H2v2h2v10h7v-6h2v6h7V12h2v-2h-2V8h-2V6h-2V4h-2V2zm0 2v2h2v2h2v2h2v2h-2v8h-3v-6H9v6H6v-8H4v-2h2V8h2V6h2V4h4z","hourglass":"M18 2H6v6h2v2h2v4H8v2H6v6h12v-6h-2v-2h-2v-4h2V8h2V2zm-2 6h-2v2h-4V8H8V4h8v4zm-2 6v2h2v4H8v-4h2v-2h4z","human-run":"M10 3H8v2H6v2h2V5h2v2h2v2h-2v2H8v2H6v2H4v-2H2v2h2v2h2v-2h4v2h2v2h-2v2h2v-2h2v-2h-2v-4h2v-2h2v2h2v2h2v-2h2v-2h-2v2h-2v-2h-2V9h2V5h-4v2h-2V5h-2V3z","image":"M4 3H2v18h20V3H4zm16 2v14H4V5h16zm-6 4h-2v2h-2v2H8v2H6v2h2v-2h2v-2h2v-2h2v2h2v2h2v-2h-2v-2h-2V9zM8 7H6v2h2V7z","inbox":"M3 2h18v20H3V2zm2 2v10h4v2h6v-2h4V4H5zm14 12h-2v2H7v-2H5v4h14v-4z","info-box":"M3 3h2v18H3V3zm16 0H5v2h14v14H5v2h16V3h-2zm-8 6h2V7h-2v2zm2 8h-2v-6h2v6z","lightbulb":"M8 2h8v2H8V2ZM6 6V4h2v2H6Zm0 6H4V6h2v6Zm2 2H6v-2h2v2Zm8 0v4H8v-4h2v2h4v-2h2Zm2-2v2h-2v-2h2Zm0-6h2v6h-2V6Zm0 0V4h-2v2h2Zm-2 14H8v2h8v-2Z","loader":"M13 2h-2v6h2V2zm0 14h-2v6h2v-6zm9-5v2h-6v-2h6zM8 13v-2H2v2h6zm7-6h2v2h-2V7zm4-2h-2v2h2V5zM9 7H7v2h2V7zM5 5h2v2H5V5zm10 12h2v2h2v-2h-2v-2h-2v2zm-8 0v-2h2v2H7v2H5v-2h2z","message-processing":"M4 2h18v16H6v2H4v-2h2v-2h14V4H4v18H2V2h2zm5 7H7v2h2V9zm2 0h2v2h-2V9zm6 0h-2v2h2V9z","moon-stars":"M20 0h2v2h2v2h-2v2h-2V4h-2V2h2V0ZM8 4h8v2h-2v2h-2V6H8V4ZM6 8V6h2v2H6Zm0 8H4V8h2v8Zm2 2H6v-2h2v2Zm8 0v2H8v-2h8Zm2-2v2h-2v-2h2Zm-2-4v-2h2V8h2v8h-2v-4h-2Zm-4 0h4v2h-4v-2Zm0 0V8h-2v4h2Zm-8 6H2v2H0v2h2v2h2v-2h2v-2H4v-2Z","moon":"M6 2h8v2h-2v2h-2V4H6V2ZM4 6V4h2v2H4Zm0 10H2V6h2v10Zm2 2H4v-2h2v2Zm2 2H6v-2h2v2Zm10 0v2H8v-2h10Zm2-2v2h-2v-2h2Zm-2-4h2v4h2v-8h-2v2h-2v2Zm-6 0v2h6v-2h-6Zm-2-2h2v2h-2v-2Zm0 0V6H8v6h2Z","more-horizontal":"M1 9h6v6H1V9zm2 2v2h2v-2H3zm6-2h6v6H9V9zm2 2v2h2v-2h-2zm6-2h6v6h-6V9zm2 2v2h2v-2h-2z","movie":"M3 3h18v18H3V3zm2 2v2h2V5H5zm4 0v6h6V5H9zm8 0v2h2V5h-2zm2 4h-2v2h2V9zm0 4h-2v2h2v-2zm0 4h-2v2h2v-2zm-4 2v-6H9v6h6zm-8 0v-2H5v2h2zm-2-4h2v-2H5v2zm0-4h2V9H5v2z","music":"M8 4h12v16h-8v-8h6V8h-8v12H2v-8h6V4zm0 10H4v4h4v-4zm10 0h-4v4h4v-4z","next":"M6 4h2v2h2v2h2v2h2v4h-2v2h-2v2H8v2H6V4zm12 0h-2v16h2V4z","notification-off":"M14 2v2h5v2h-8V2h3zM5 16h9v2h2v4h-6v-2h4v-2h-4v4H8v-4H3v-6h2v-2h2v4H5v2zm16-2h-2v-2h-2V6h2v6h2v2zM5 2H3v2h2v2h2v2h2v2h2v2h2v2h2v2h2v2h2v2h2v-2h-2v-2h-2v-2h-2v-2h-2v-2h-2V8H9V6H7V4H5V2z","notification":"M14 4V2h-4v2H5v2h14V4h-5zm5 12H5v-4H3v6h5v4h2v-4h4v2h-4v2h6v-4h5v-6h-2V6h-2v8h2v2zM5 6v8h2V6H5z","paperclip":"M5 5h16v10H7V9h10v2H9v2h10V7H5v10h14v2H3V5h2z","pause":"M10 4H5v16h5V4zm9 0h-5v16h5V4z","play":"M10 20H8V4h2v2h2v3h2v2h2v2h-2v2h-2v3h-2v2z","plus":"M11 4h2v7h7v2h-7v7h-2v-7H4v-2h7V4z","power":"M20 2h-2v4H6v2H4v8h2v2h2v4h8v-2h4v-2h-4v-2h4v-2h-4v-2H8v4H6V8h12V6h2V2zm-6 18h-4v-6h4v6z","radio-handheld":"M9 2v5h8v15H7V2h2zm0 7v4h6V9H9zm6 6H9v5h6v-5z","radio-tower":"M22 2h-2v2h2v12h-2v2h2v-2h2V4h-2V2ZM2 4H0v12h2v2h2v-2H2V4Zm0 0V2h2v2H2Zm4 2H4v8h2V6Zm0 0V4h2v2H6Zm4 0h4v2h-4V6Zm0 6H8V8h2v4Zm4 0h-4v2H8v4H6v4h2v-4h2v-4h4v4h2v4h2v-4h-2v-4h-2v-2Zm0 0h2V8h-2v4Zm6-6h-2V4h-2v2h2v8h2V6Z","reload":"M16 2h-2v2h2v2H4v2H2v5h2V8h12v2h-2v2h2v-2h2V8h2V6h-2V4h-2V2zM6 20h2v2h2v-2H8v-2h12v-2h2v-5h-2v5H8v-2h2v-2H8v2H6v2H4v2h2v2z","server":"M3 3h18v18H3V3zm2 2v6h14V5H5zm14 8H5v6h14v-6zM7 7h2v2H7V7zm2 8H7v2h2v-2z","speed-fast":"M15 5H9v2H5v2H3v2H1v6h2v2h2v-2H3v-6h2V9h4V7h6V5zm8 6h-2v6h-2v2h2v-2h2v-6zm-13 2h4v4h-4v-4zm6-2h-2v2h2v-2zm2-2v2h-2V9h2zm0 0V7h2v2h-2z","sun":"M13 3h-2v2h2V3zm4 2h2v2h-2V5zm-6 6h2v2h-2v-2zm-8 0h2v2H3v-2zm18 0h-2v2h2v-2zM5 5h2v2H5V5zm14 14h-2v-2h2v2zm-8 2h2v-2h-2v2zm-4-2H5v-2h2v2zM9 7h6v2H9V7zm0 8H7V9h2v6zm0 0v2h6v-2h2V9h-2v6H9z","sync":"M4 9V7h12V5h2v2h2v2h-2v2h-2V9H4zm12 2h-2v2h2v-2zm0-6h-2V3h2v2zm4 12v-2H8v-2h2v-2H8v2H6v2H4v2h2v2h2v2h2v-2H8v-2h12z","table":"M2 3h20v18H2V3zm2 4v5h7V7H4zm9 0v5h7V7h-7zm7 7h-7v5h7v-5zm-9 5v-5H4v5h7z","tournament":"M9 2H2v2h5v4H2v2h7V7h5v10H9v-3H2v2h5v4H2v2h7v-3h7v-6h6v-2h-6V5H9V2z","trash":"M16 2v4h6v2h-2v14H4V8H2V6h6V2h8zm-2 2h-4v2h4V4zm0 4H6v12h12V8h-4zm-5 2h2v8H9v-8zm6 0h-2v8h2v-8z","trending-up":"M14 6h8v8h-2v-4h-2V8h-4V6zm2 6v-2h2v2h-2zm-2 2v-2h2v2h-2zm-2 0h2v2h-2v-2zm-2-2h2v2h-2v-2zm-2 0v-2h2v2H8zm-2 2v-2h2v2H6zm-2 2v-2h2v2H4zm0 0v2H2v-2h2z","trophy":"M16 3H6v2H2v10h6V5h8v10h6V5h-4V3h-2zm4 4v6h-2V7h2zM6 13H4V7h2v6zm12 2H6v2h12v-2zm-7 2h2v2h3v2H8v-2h3v-2z","upload":"M11 5V3h2v2h2v2h2v2h-2V7h-2v10h-2V7H9v2H7V7h2V5h2zM3 15v6h18v-6h-2v4H5v-4H3z","user-plus":"M18 2h-6v2h-2v6h2V4h6V2zm0 8h-6v2h6v-2zm0-6h2v6h-2V4zM7 16h2v-2h12v2H9v4h12v-4h2v6H7v-6zM3 8h2v2h2v2H5v2H3v-2H1v-2h2V8z","users":"M11 0H5v2H3v6h2v2h6V8H5V2h6V0zm0 2h2v6h-2V2zM0 14h2v4h12v2H0v-6zm2 0h12v-2H2v2zm14 0h-2v6h2v-6zM15 0h4v2h-4V0zm4 8h-4v2h4V8zm0-6h2v6h-2V2zm5 12h-2v4h-4v2h6v-6zm-6-2h4v2h-4v-2z","video":"M2 5h14v4h2V7h2V5h2v14h-2v-2h-2v-2h-2v4H2V5zm2 12h10V7H4v10z","volume-3":"M11 2H9v2H7v2H5v2H1v8h4v2h2v2h2v2h2V2zM7 18v-2H5v-2H3v-4h2V8h2V6h2v12H7zm6-8h2v4h-2v-4zm8-6h-2V2h-6v2h6v2h2v12h-2v2h-6v2h6v-2h2v-2h2V6h-2V4zm-2 4h-2V6h-4v2h4v8h-4v2h4v-2h2V8z","volume-x":"M13 2h-2v2H9v2H7v2H3v8h4v2h2v2h2v2h2V2zM9 18v-2H7v-2H5v-4h2V8h2V6h2v12H9zm10-6.777h-2v-2h-2v2h2v2h-2v2h2v-2h2v2h2v-2h-2v-2zm0 0h2v-2h-2v2z","warning-box":"M3 3h16v2H5v14h14v2H3V3zm18 0h-2v18h2V3zM11 15h2v2h-2v-2zm2-8h-2v6h2V7z","wind":"M12 3H8v2h4v2H2v2h12V3h-2zm10 8V7h-6v2h4v2H2v2h20v-2zM2 17v-2h14v6h-6v-2h4v-2H2z","zap":"M12 1h2v8h8v4h-2v-2h-8V5h-2V3h2V1zM8 7V5h2v2H8zM6 9V7h2v2H6zm-2 2V9h2v2H4zm10 8v2h-2v2h-2v-8H2v-4h2v2h8v6h2zm2-2v2h-2v-2h2zm2-2v2h-2v-2h2zm0 0h2v-2h-2v2z","question":"M8 0h8v2h-8zM6 2h12v2h-12zM4 4h4v2h-4zM16 4h4v2h-4zM4 6h4v2h-4zM16 6h4v2h-4zM16 8h4v2h-4zM14 10h4v2h-4zM12 12h4v2h-4zM10 14h4v2h-4zM10 16h4v2h-4zM10 20h4v2h-4zM10 22h4v2h-4z","flame":"M10 0h2v2h-2zM10 2h4v2h-4zM8 4h2v2h-2zM12 4h2v2h-2zM8 6h2v2h-2zM14 6h2v2h-2zM6 8h2v2h-2zM14 8h2v2h-2zM18 8h2v2h-2zM6 10h2v2h-2zM16 10h6v2h-6zM4 12h2v2h-2zM20 12h2v2h-2zM4 14h2v2h-2zM12 14h2v2h-2zM20 14h2v2h-2zM2 16h2v2h-2zM10 16h2v2h-2zM14 16h2v2h-2zM20 16h2v2h-2zM2 18h2v2h-2zM10 18h2v2h-2zM14 18h2v2h-2zM20 18h2v2h-2zM4 20h2v2h-2zM10 20h2v2h-2zM14 20h2v2h-2zM18 20h2v2h-2zM6 22h12v2h-12z","rocket":"M4 0h18v2h-18zM4 2h4v2h-4zM2 4h8v2h-8zM2 6h10v2h-10zM2 8h12v2h-12zM0 10h24v2h-24zM0 12h24v2h-24zM2 14h12v2h-12zM2 16h10v2h-10zM2 18h8v2h-8zM4 20h4v2h-4zM4 22h18v2h-18z","mic":"M8 0h8v2h-8zM6 2h2v2h-2zM16 2h2v2h-2zM6 4h2v2h-2zM16 4h2v2h-2zM6 6h2v2h-2zM16 6h2v2h-2zM6 8h2v2h-2zM16 8h2v2h-2zM2 10h2v2h-2zM6 10h2v2h-2zM16 10h2v2h-2zM20 10h2v2h-2zM2 12h2v2h-2zM8 12h8v2h-8zM20 12h2v2h-2zM4 14h2v2h-2zM18 14h2v2h-2zM6 16h12v2h-12zM10 18h4v2h-4zM10 20h4v2h-4zM6 22h12v2h-12z","star":"M10 0h4v2h-4zM10 2h4v2h-4zM8 4h2v2h-2zM14 4h2v2h-2zM0 6h10v2h-10zM14 6h10v2h-10zM2 8h2v2h-2zM20 8h2v2h-2zM4 10h2v2h-2zM18 10h2v2h-2zM6 12h2v2h-2zM16 12h2v2h-2zM4 14h2v2h-2zM10 14h4v2h-4zM18 14h2v2h-2zM4 16h2v2h-2zM8 16h2v2h-2zM14 16h2v2h-2zM18 16h2v2h-2zM2 18h2v2h-2zM6 18h2v2h-2zM16 18h2v2h-2zM20 18h2v2h-2zM2 20h4v2h-4zM18 20h4v2h-4z","bot":"M6 0h12v2h-12zM4 2h2v2h-2zM18 2h2v2h-2zM2 4h2v2h-2zM8 4h4v2h-4zM20 4h2v2h-2zM2 6h2v2h-2zM8 6h4v2h-4zM16 6h2v2h-2zM20 6h2v2h-2zM2 8h20v2h-20zM2 10h2v2h-2zM20 10h2v2h-2zM2 12h2v2h-2zM6 12h12v2h-12zM20 12h2v2h-2zM2 14h2v2h-2zM20 14h2v2h-2zM0 16h4v2h-4zM6 16h12v2h-12zM20 16h4v2h-4zM0 18h4v2h-4zM20 18h4v2h-4zM0 20h4v2h-4zM6 20h4v2h-4zM14 20h4v2h-4zM20 20h4v2h-4zM6 22h4v2h-4zM14 22h4v2h-4z","cellular-signal-3":"M16 4h6v16h-6V4ZM2 14h6v6H2v-6Zm13-4H9v10h6V10Z","message-arrow-right":"M4 2h18v10h-2V4H4v18H2V2h2zm2 14h4v2H6v2H4v-2h2v-2zm16 0h-2v-2h-2v-2h-2v2h2v2h-6v2h6v2h-2v2h2v-2h2v-2h2v-2z","cellular-signal-2":"M16 4v16h6V4h-6Zm4 2v12h-2V6h2ZM2 14h6v6H2v-6Zm13-4H9v10h6V10Z","keyboard":"M21 3H3v18h18V3zM5 19V5h14v14H5zM9 7H7v2h2V7zm8 8H7v2h10v-2zm-2-8h2v2h-2V7zm-2 0h-2v2h2V7zm-6 4h2v2H7v-2zm10 0h-2v2h2v-2zm-6 0h2v2h-2v-2z","lock":"M15 2H9v2H7v4H4v14h16V8h-3V4h-2V2zm0 2v4H9V4h6zm-6 6h9v10H6V10h3zm4 3h-2v4h2v-4z","shield":"M22 2H2v12h2V4h16v10h2V2zM6 14H4v2h2v-2zm0 2h2v2h2v2H8v-2H6v-2zm4 4v2h4v-2h2v-2h-2v2h-4zm10-6h-2v2h-2v2h2v-2h2v-2z","edit":"M18 2h-2v2h-2v2h-2v2h-2v2H8v2H6v2H4v2H2v6h6v-2h2v-2h2v-2h2v-2h2v-2h2v-2h2V8h2V6h-2V4h-2V2zm0 8h-2v2h-2v2h-2v2h-2v2H8v-2H6v-2h2v-2h2v-2h2V8h2V6h2v2h2v2zM6 16H4v4h4v-2H6v-2z","gamepad":"M2 5h20v14H2V5zm18 12V7H4v10h16zM8 9h2v2h2v2h-2v2H8v-2H6v-2h2V9zm6 0h2v2h-2V9zm4 4h-2v2h2v-2z","monitor":"M20 3H2v14h8v2H8v2h8v-2h-2v-2h8V3h-2zm-6 12H4V5h16v10h-6z","heart":"M9 2H5v2H3v2H1v6h2v2h2v2h2v2h2v2h2v2h2v-2h2v-2h2v-2h2v-2h2v-2h2V6h-2V4h-2V2h-4v2h-2v2h-2V4H9V2zm0 2v2h2v2h2V6h2V4h4v2h2v6h-2v2h-2v2h-2v2h-2v2h-2v-2H9v-2H7v-2H5v-2H3V6h2V4h4z","sliders":"M17 4h2v10h-2V4zm0 12h-2v2h2v2h2v-2h2v-2h-4zm-4-6h-2v10h2V10zm-8 2H3v2h2v6h2v-6h2v-2H5zm8-8h-2v2H9v2h6V6h-2V4zM5 4h2v6H5V4z","volume-2":"M11 2h2v20h-2v-2H9v-2h2V6H9V4h2V2zM7 8V6h2v2H7zm0 8H3V8h4v2H5v4h2v2zm0 0v2h2v-2H7zm10-6h-2v4h2v-4zm2-2h2v8h-2V8zm0 8v2h-4v-2h4zm0-10v2h-4V6h4z"};
// Server events and trophies still carry emoji from their first days; they are
// only names here and are drawn with the pixel set above (the owner 10-16: replace the emoji
// with prettier icons in our own style).
const ICON_OF = { '✅': 'check', '✓': 'check', '🏆': 'trophy', '🚀': 'rocket', '📦': 'git-commit', '➤': 'arrow-right',
  '⚡': 'zap', '🪖': 'user-plus', '💫': 'power', '🏁': 'flag', '🛰': 'radio-tower', '📜': 'article', '🎯': 'bullseye',
  '⬆': 'upload', '📣': 'video', '🔥': 'flame', '☄': 'star', '🗡': 'flag', '🤹': 'dice', '🐙': 'group', '🐝': 'users',
  '🦉': 'moon-stars', '🐓': 'sun', '🌪': 'wind', '🏃': 'human-run', '🧹': 'trash', '🎙': 'radio-handheld',
  '💯': 'trending-up', '🏛': 'tournament', '🌈': 'speed-fast', '🧠': 'lightbulb', '•': 'bot' };
const ACH_ICON = { first_blood: 'flag', juggler: 'dice', octopus: 'group', swarm: 'users', quick_draw: 'zap',
  night_owl: 'moon-stars', early_bird: 'sun', shipper: 'rocket', ship_storm: 'wind', committed: 'git-commit',
  marathon: 'human-run', clean_desk: 'trash', recruiter: 'user-plus', voice: 'radio-handheld', centurion: 'trending-up',
  thousand: 'tournament', streak3: 'flame', streak7: 'star', flow: 'speed-fast', dailies: 'trophy', big_brain: 'lightbulb' };
function ico(name, cls) {
  const d = PXI[name] || PXI[ICON_OF[name]] || PXI.bot;
  return '<svg class="px' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="' + d + '" fill="currentColor"/></svg>';
}
function icoOf(e) { return ico(ICON_OF[e] || e || 'bot'); }
function fillIcons(root) { (root || document).querySelectorAll('[data-ico]').forEach(el => { el.innerHTML = ico(el.dataset.ico); }); }

// ------------------------------------------------------------------ sound
const SFX = {
  ctx: null,
  init() {
    if (!this.ctx) { const A = window.AudioContext || window.webkitAudioContext; if (!A) return false; this.ctx = new A(); }
    if (this.ctx.state === 'suspended') this.ctx.resume();
    return true;
  },
  tone(f, d, type, at, vol, f2) {
    const c = this.ctx, t = c.currentTime + (at || 0);
    const o = c.createOscillator(), g = c.createGain();
    o.type = type || 'square';
    o.frequency.setValueAtTime(f, t);
    if (f2) o.frequency.exponentialRampToValueAtTime(f2, t + d);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.09 * (vol || 1), t + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + d);
    o.connect(g); g.connect(this.out());
    o.start(t); o.stop(t + d + 0.02);
  },
  // every effect goes through one gain: the volume slider in settings
  out() {
    if (!this.mg) { this.mg = this.ctx.createGain(); this.mg.gain.value = LS.get('vol', 0.8); this.mg.connect(this.ctx.destination); }
    return this.mg;
  },
  noise() {
    if (!this.nb) {
      const c = this.ctx, n = Math.floor(c.sampleRate * 1.2), b = c.createBuffer(1, n, c.sampleRate), d = b.getChannelData(0);
      for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
      this.nb = b;
    }
    return this.nb;
  },
  whoosh(at, dur, f0, f1, vol) {
    const c = this.ctx, t = c.currentTime + (at || 0);
    const src = c.createBufferSource(); src.buffer = this.noise();
    const bp = c.createBiquadFilter(); bp.type = 'bandpass'; bp.Q.value = 1.6;
    bp.frequency.setValueAtTime(f0, t); bp.frequency.exponentialRampToValueAtTime(f1, t + dur);
    const g = c.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.18 * (vol || 1), t + dur * 0.3);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    src.connect(bp); bp.connect(g); g.connect(this.out());
    src.start(t); src.stop(t + dur + 0.05);
  },
  // A bot «talks»: a few quick glides, a different phrase every time.
  chirp(mood, at) {
    let t = at || 0;
    const n = mood === 'sad' ? 3 : mood === 'ask' ? 3 : 4 + Math.floor(Math.random() * 3);
    for (let i = 0; i < n; i++) {
      const base = mood === 'sad' ? 1300 - i * 260 : 900 + Math.random() * 1800;
      const f2 = mood === 'sad' ? base * 0.55 : (Math.random() < 0.5 ? base * 1.9 : base * 0.6);
      const d = 0.035 + Math.random() * 0.06;
      this.tone(base, d, i % 2 ? 'sine' : 'square', t, 0.45, f2);
      t += d + 0.012 + Math.random() * 0.03;
    }
    if (mood === 'ask') this.tone(1100, 0.16, 'sine', t, 0.5, 2700);
    if (mood === 'happy') this.tone(1900, 0.09, 'sine', t, 0.45, 3300);
    return t;
  },
  // filtered noise: door hiss, thrusters, static, key clicks
  nburst(at, dur, type, f0, f1, vol, q) {
    const c = this.ctx, t = c.currentTime + (at || 0);
    const src = c.createBufferSource(); src.buffer = this.noise();
    const fl = c.createBiquadFilter(); fl.type = type || 'bandpass'; fl.Q.value = q || 1;
    fl.frequency.setValueAtTime(f0, t); fl.frequency.exponentialRampToValueAtTime(Math.max(20, f1 || f0), t + dur);
    const g = c.createGain();
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.2 * (vol || 1), t + Math.min(0.02, dur / 4));
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    src.connect(fl); fl.connect(g); g.connect(this.out());
    src.start(t, Math.random() * 0.5); src.stop(t + dur + 0.05);
  },
  // a light blade lighting up: a rising hiss into a detuned hum with a wobble
  saber(at, len) {
    const c = this.ctx, t = c.currentTime + (at || 0), d = len || 1.1;
    this.nburst(at, 0.32, 'bandpass', 260, 2600, 0.9, 0.8);
    const g = c.createGain(), lp = c.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 700;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.11, t + 0.12); g.gain.exponentialRampToValueAtTime(0.0001, t + d);
    const lfo = c.createOscillator(), lg = c.createGain(); lfo.frequency.value = 5.5; lg.gain.value = 6; lfo.connect(lg);
    [88, 91.5].forEach(fq => { const o = c.createOscillator(); o.type = 'sawtooth'; o.frequency.value = fq; lg.connect(o.frequency); o.connect(lp); o.start(t); o.stop(t + d + 0.05); });
    lp.connect(g); g.connect(this.out()); lfo.start(t); lfo.stop(t + d + 0.05);
  },
  // the blaster: a fast falling zap with a sub tone
  blaster(at) {
    this.tone(1900, 0.17, 'sine', at, 0.8, 170);
    this.tone(950, 0.15, 'square', at, 0.25, 90);
    this.nburst(at, 0.06, 'highpass', 3000, 3000, 0.25);
  },
  // a heroic brass-ish rise (not anybody's theme): saw through a soft filter
  fanfare(at) {
    const c = this.ctx;
    [[392, 0], [523, 0.12], [659, 0.24], [784, 0.36], [1047, 0.5]].forEach(([fq, dt], i) => {
      const t = c.currentTime + (at || 0) + dt, d = i === 4 ? 0.7 : 0.16;
      const o = c.createOscillator(), o2 = c.createOscillator(), lp = c.createBiquadFilter(), g = c.createGain();
      o.type = 'sawtooth'; o2.type = 'sawtooth'; o.frequency.value = fq; o2.frequency.value = fq * 1.006;
      lp.type = 'lowpass'; lp.frequency.setValueAtTime(900, t); lp.frequency.linearRampToValueAtTime(2600, t + 0.06);
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.07, t + 0.03); g.gain.exponentialRampToValueAtTime(0.0001, t + d);
      o.connect(lp); o2.connect(lp); lp.connect(g); g.connect(this.out());
      o.start(t); o2.start(t); o.stop(t + d + 0.05); o2.stop(t + d + 0.05);
    });
  },
  play(name) {
    if (!SND || SOUNDS[SOUND_CAT[name] || 'ui'] === false) return;
    try {
      if (!this.init()) return;
      const T = (f, d, ty, at, v, f2) => this.tone(f, d, ty, at, v, f2);
      // 10-16 «сделай все звуки под space и космический корабль»
      switch (name) {
        case 'launch': this.blaster(0); this.whoosh(0.04, 0.3, 600, 3800, 0.6); break;
        case 'ack': this.chirp('happy', 0.02); break;
        case 'done': { const e = this.chirp('happy'); T(1568, 0.22, 'triangle', e, 0.45); break; }
        case 'sad': case 'error': this.chirp('sad'); break;
        case 'comm-open': T(1400, 0.05, 'square', 0, 0.45); T(1800, 0.06, 'square', 0.06, 0.45); this.nburst(0, 0.16, 'bandpass', 2600, 4200, 0.45, 2); break;
        case 'comm-close': T(1800, 0.05, 'square', 0, 0.45); T(1200, 0.07, 'square', 0.06, 0.45); this.nburst(0.02, 0.12, 'bandpass', 4200, 1800, 0.4, 2); break;
        case 'heard': this.chirp('happy'); break;
        case 'count': T(2200, 0.05, 'sine', 0, 0.6); T(2200, 0.05, 'sine', 0.08, 0.5); break;
        case 'spool': T(60, 1.7, 'sawtooth', 0, 0.22, 380); this.nburst(0, 1.7, 'lowpass', 220, 1400, 0.7, 0.7); break;
        case 'jump': this.whoosh(0, 1.0, 180, 8000, 1.3); T(110, 0.9, 'sawtooth', 0, 0.18, 2000); break;
        // the launch scene: hangar doors and alarm, the main engines, the jump to jump
        case 'hype': T(150, 0.15, 'square', 0, 0.42, 70); this.nburst(0, 0.24, 'lowpass', 2600, 240, 0.85, 0.8); T(880, 0.09, 'square', 0.07, 0.16, 1400); break;
        case 'hangar': this.nburst(0, 0.55, 'lowpass', 4200, 300, 1.1, 0.8); T(660, 0.12, 'square', 0.06, 0.22); T(520, 0.12, 'square', 0.22, 0.22); T(660, 0.12, 'square', 0.38, 0.22); break;
        case 'liftoff': T(70, 0.32, 'sine', 0, 1.4, 36); this.nburst(0, 2.3, 'lowpass', 140, 2600, 1.9, 0.9); T(55, 2.1, 'sawtooth', 0, 0.34, 170); T(110, 1.7, 'square', 0.1, 0.1, 360); this.whoosh(0.8, 1.3, 300, 2800, 0.9); break;
        case 'warp': this.whoosh(0, 0.8, 200, 9000, 1.9); T(160, 0.62, 'sawtooth', 0, 0.22, 5200); T(320, 0.55, 'square', 0.05, 0.07, 7000); T(58, 0.9, 'sine', 0.52, 1.5, 28); this.nburst(0.52, 1.0, 'lowpass', 2000, 90, 1.7, 0.7); break;
        case 'land': this.nburst(0, 0.5, 'lowpass', 900, 160, 1, 0.8); T(150, 0.16, 'square', 0.35, 0.6, 70); this.chirp('happy', 0.5); break;
        case 'deploy': this.nburst(0, 1.3, 'bandpass', 380, 2600, 1.1, 1.3); T(220, 1.2, 'sawtooth', 0, 0.16, 120); this.blaster(0.5); this.blaster(0.75); break;
        case 'open': this.nburst(0, 0.26, 'lowpass', 3200, 350, 0.75, 0.8); T(1600, 0.04, 'square', 0.03, 0.3); break;
        case 'close': this.nburst(0, 0.18, 'lowpass', 450, 2600, 0.5, 0.8); break;
        case 'click': T(1300, 0.025, 'sine', 0, 0.45); this.nburst(0, 0.02, 'bandpass', 3500, 3500, 0.3, 3); break;
        case 'hover': T(2600, 0.025, 'sine', 0, 0.3); T(2600, 0.025, 'sine', 0.05, 0.25); break;
        case 'xp': T(2300 + Math.random() * 600, 0.04, 'sine', 0, 0.35, 3200); break;
        case 'loot': T(2000, 0.04, 'square', 0, 0.35); T(2600, 0.05, 'square', 0.05, 0.35); T(3100, 0.07, 'sine', 0.1, 0.3); break;
        case 'quest': T(2200, 0.04, 'sine', 0, 0.5); T(2200, 0.04, 'sine', 0.07, 0.5); this.chirp('happy', 0.14); break;
        case 'goal': this.saber(0, 0.7); this.chirp('happy', 0.2); break;
        case 'ach': this.fanfare(0); this.saber(0.05, 1.3); break;
        case 'level': this.whoosh(0, 0.8, 200, 7000, 1.1); this.fanfare(0.55); this.saber(0.6, 1.6); break;
        case 'gulp': T(320, 0.25, 'sine', 0, 0.6, 720); T(720, 0.12, 'triangle', 0.22, 0.4, 1400); break;
        case 'key': this.nburst(0, 0.022, 'bandpass', 2600 + Math.random() * 1600, 0, 0.32, 4); T(1200 + Math.random() * 700, 0.012, 'square', 0, 0.12); break;
        case 'key-del': this.nburst(0, 0.03, 'bandpass', 1400, 0, 0.3, 4); T(700, 0.015, 'square', 0, 0.12); break;
        case 'key-enter': this.nburst(0, 0.05, 'bandpass', 1800, 900, 0.45, 3); T(320, 0.06, 'square', 0, 0.25, 140); break;
        case 'send': this.blaster(0); break;
        case 'coin': this.chirp('happy'); break;
        case 'open-big': this.nburst(0, 0.38, 'lowpass', 4200, 300, 0.9, 0.8); T(900, 0.06, 'square', 0.05, 0.3); T(1350, 0.06, 'square', 0.12, 0.3); break;
      }
    } catch (e) {}
  },
};

// 10-16 «расширенное управление звуками, чтобы можно было отключить любой»:
// every sound belongs to a group, and each group has its own switch.
const SOUND_CAT = { launch: 'send', send: 'send', ack: 'bot', done: 'bot', sad: 'bot', error: 'bot', heard: 'bot', coin: 'bot',
  'comm-open': 'comms', 'comm-close': 'comms', count: 'launch', spool: 'launch', jump: 'launch', land: 'launch', deploy: 'deploy',
  hangar: 'launch', liftoff: 'launch', warp: 'launch', hype: 'hype', score: 'score',
  open: 'doors', close: 'doors', 'open-big': 'doors', click: 'ui', hover: 'ui', xp: 'ui', loot: 'loot', gulp: 'loot',
  quest: 'goals', goal: 'goals', ach: 'glory', level: 'glory', key: 'keys', 'key-del': 'keys', 'key-enter': 'keys' };
const SOUND_GROUPS = [['send', 'Sending: the blaster', 'arrow-up', 'launch'], ['bot', 'Bot voices: replies, results, errors', 'bot', 'done'],
  ['comms', 'Voice recording: the comlink', 'radio-handheld', 'comm-open'], ['doors', 'Doors: windows and panels', 'more-horizontal', 'open-big'],
  ['ui', 'Console clicks and blips', 'command', 'click'], ['goals', 'Goals: the saber', 'bullseye', 'goal'],
  ['glory', 'Trophies and level-ups', 'trophy', 'ach'], ['loot', 'Commits, files, uploads', 'git-commit', 'loot'],
  ['launch', 'Launch sequence', 'user-plus', 'spool'], ['deploy', 'Starfighter fly-by on deploys', 'rocket', 'deploy'],
  ['keys', 'Typing: console keys', 'keyboard', 'key'], ['hype', 'Hype pop-ups: the slam', 'zap', 'hype'], ['score', 'Launch scene music', 'music', 'score']];
let SOUNDS = LS.get('sounds', {});
if (LS.get('keys', true) === false && SOUNDS.keys === undefined) SOUNDS.keys = false;

// ------------------------------------------------------------------ sprites
const PALETTE = [['#ff8a3d', '#b4521b'], ['#ff5fa2', '#a8326a'], ['#4de1ff', '#1f86a3'], ['#9dff57', '#4f9a1e'],
  ['#b36bff', '#6a35a8'], ['#ffd23f', '#a8861a'], ['#ff6f61', '#a53b31'], ['#5ef0c0', '#239574']];
const ACCS = ['none', 'antenna', 'headset', 'leaf', 'horns', 'cap', 'bow', 'antenna'];
function hash(s) { let h = 2166136261; s = String(s); for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
function specFor(sid, lv) {
  const h = hash(sid), p = PALETTE[h % PALETTE.length];
  return { c: p[0], d: p[1], acc: ACCS[(h >>> 5) % ACCS.length], ac: PALETTE[(h >>> 9) % PALETTE.length][0],
    crown: (lv || 0) >= 10, seed: h % 97, ...botParts(sid) };
}
// 10-16 «дроидов должно быть сильно больше»: a bot is put together from
// parts, so the crew rarely repeats: 12 bodies × 7 metals × 8×8 colours ×
// 5 eyes × 7 tops × 4 decals.
const BODIES = ['astro', 'ball', 'mouse', 'proto', 'probe', 'tread', 'tripod', 'hover', 'spider', 'tower', 'box', 'orb'];
const METALS = [['#ece8ff', '#b4add9', '#6f68a0'], ['#d5dbee', '#8f97bd', '#565d86'], ['#ffe08a', '#d1a23b', '#8a6421'],
  ['#6a6494', '#433d6c', '#231e45'], ['#e4a173', '#b0663c', '#6d3a1f'], ['#cfe6d6', '#86a993', '#4d6b58'], ['#ff9a8a', '#d0503e', '#7a2a22']];
function botParts(sid) {
  const h = hash(sid + '#bot');
  return { body: BODIES[h % BODIES.length], metal: METALS[(h >>> 4) % METALS.length],
    c2: PALETTE[(h >>> 8) % PALETTE.length][0], eyeType: (h >>> 12) % 5, top: (h >>> 15) % 7, decal: (h >>> 18) % 4 };
}
// 10-16 «можно попробовать агентов в стиле space»: the crew can be bots
// (astromech, ball bot, mouse bot) or the original Pilot buddies.
let CREW = LS.get('crew', 'bots');
function drawBot(ctx, sp, st, f) {
  ctx.clearRect(0, 0, 16, 16);
  const P = (x, y, w, h, c) => px(x, y, w, h, c, ctx);
  const M = sp.metal || METALS[0], L = M[0], Md = M[1], Dk = M[2], K = '#140b26', C = sp.c, C2 = sp.c2 || sp.d;
  const on = st !== 'sleep';
  const busy = st === 'busy';
  const blink = ((f + sp.seed) % 9) < 4;
  const eye = !on ? '#3a3360' : st === 'ask' ? '#d9a6ff' : st === 'happy' ? '#ffd23f' : busy ? '#4de1ff' : '#7aa8ff';
  const lamp = on && (busy ? (f % 2) : blink) ? '#ff4d6d' : '#4a1d2b';
  const floats = sp.body === 'probe' || sp.body === 'hover' || sp.body === 'orb';
  P(3, 15, 10, 1, floats ? 'rgba(0,0,0,.25)' : 'rgba(0,0,0,.45)');
  let bob = st === 'happy' ? ((f % 4 < 2) ? -1 : 0) : st === 'sleep' ? 1 : 0;
  if (floats && on) bob += (f % 6 < 3) ? -1 : 0;
  ctx.save(); ctx.translate(0, bob);
  // eyes: single lens, twin, visor, triple, cyclops
  const eyes = (x, y, w) => {
    const cx = x + Math.floor(w / 2);
    const look = busy ? [-1, 0, 1, 0][f % 4] : 0;
    if (sp.eyeType === 0) { P(cx - 1 + look, y, 2, 2, K); P(cx + look, y, 1, 1, eye); }
    else if (sp.eyeType === 1) { P(cx - 2 + look, y, 1, 1, eye); P(cx + 1 + look, y, 1, 1, eye); }
    else if (sp.eyeType === 2) { P(x + 1, y, w - 2, 1, K); P(x + 1 + ((f % (w - 2)) || 0) * (busy ? 1 : 0), y, busy ? 2 : w - 2, 1, eye); }
    else if (sp.eyeType === 3) { P(cx - 2, y, 1, 1, eye); P(cx, y, 1, 1, eye); P(cx + 2, y, 1, 1, eye); }
    else { P(cx - 1, y - 1, 3, 3, K); P(cx, y, 1, 1, eye); if (on) P(cx - 1, y - 1, 1, 1, 'rgba(255,255,255,.5)'); }
  };
  // top: antenna, twin antennas, dish, light bar, periscope, fin
  const top = (cx, y) => {
    if (sp.top === 1) { P(cx, y - 2, 1, 2, Md); P(cx, y - 3, 1, 1, lamp); }
    else if (sp.top === 2) { P(cx - 2, y - 2, 1, 2, Md); P(cx + 2, y - 2, 1, 2, Md); P(cx - 2, y - 3, 1, 1, C); P(cx + 2, y - 3, 1, 1, C); }
    else if (sp.top === 3) { P(cx - 1, y - 1, 1, 1, Md); P(cx - 2, y - 3, 3, 1, L); P(cx - 3, y - 2, 1, 1, L); P(cx + 1, y - 2, 1, 1, L); }
    else if (sp.top === 4) { P(cx - 2, y - 1, 5, 1, Dk); P(cx - 2 + (f % 5), y - 1, 1, 1, lamp); }
    else if (sp.top === 5) { P(cx + 1, y - 3, 1, 3, Md); P(cx + 1, y - 4, 2, 1, Md); P(cx + 2, y - 4, 1, 1, eye); }
    else if (sp.top === 6) { P(cx - 1, y - 2, 2, 2, C2); P(cx - 1, y - 3, 1, 1, C2); }
  };
  const decal = (x, y) => {
    if (sp.decal === 1) P(x, y, 3, 1, C2);
    else if (sp.decal === 2) { P(x, y, 1, 1, C2); P(x + 2, y, 1, 1, C2); }
    else if (sp.decal === 3) { P(x, y, 1, 1, C2); P(x + 1, y + 1, 1, 1, C2); P(x + 2, y, 1, 1, C2); }
  };
  const b = sp.body;
  if (b === 'ball') {
    const ox = busy ? [0, 1, 0, -1][f % 4] : 0;
    P(5, 8, 6, 1, L); P(4, 9, 8, 1, L); P(3, 10, 10, 3, L); P(4, 13, 8, 1, L); P(5, 14, 6, 1, Md);
    P(6 + ox, 10, 4, 1, C); P(5 + ox, 11, 1, 1, C); P(10 + ox, 11, 1, 1, C); P(6 + ox, 12, 4, 1, C); P(7 + ox, 11, 2, 1, Md);
    P(6, 4, 4, 1, L); P(5, 5, 6, 2, L); P(5, 7, 6, 1, C2);
    eyes(5, 5, 6); top(8, 4);
  } else if (b === 'mouse') {
    const mx = busy ? [0, 1, 2, 1, 0, -1][f % 6] : 0;
    P(2 + mx, 9, 12, 1, Md); P(2 + mx, 10, 12, 3, Dk); P(3 + mx, 11, 10, 1, C);
    P(12 + mx, 10, 2, 1, K); P(13 + mx, 10, 1, 1, eye);
    P(3 + mx, 13, 2, 1, K); P(11 + mx, 13, 2, 1, K); P(7 + mx, 13, 2, 1, K);
    P(4 + mx, 8, 2, 1, lamp); decal(6 + mx, 10); top(9 + mx, 9);
  } else if (b === 'proto') {
    const arm = busy ? (f % 2) : st === 'happy' ? -2 : 0;
    P(6, 1, 4, 1, L); P(5, 2, 6, 3, L); P(6, 5, 4, 1, Md);
    eyes(5, 3, 6);
    P(7, 6, 2, 1, Dk); P(5, 7, 6, 4, Md); P(6, 8, 4, 2, C); decal(6, 10);
    P(3, 7 + arm, 2, 4, Md); P(11, 7 - arm, 2, 4, Md);
    P(5, 11, 2, 3, Md); P(9, 11, 2, 3, Md); P(4, 14, 3, 1, Dk); P(9, 14, 3, 1, Dk);
    top(8, 1);
  } else if (b === 'probe') {
    P(5, 3, 6, 1, Dk); P(4, 4, 8, 4, Dk); P(5, 8, 6, 1, Dk); P(5, 4, 2, 1, Md);
    eyes(4, 5, 8);
    const sw = busy ? (f % 2) : 0;
    P(5, 9, 1, 4 + sw, Md); P(8, 9, 1, 5 - sw, Md); P(10, 9, 1, 4, Md); P(5, 13 + sw, 2, 1, C); P(10, 13, 2, 1, C);
    top(8, 3);
  } else if (b === 'tread') {
    P(2, 11, 12, 3, K); P(3, 12, 1, 1, Md); P(6, 12, 1, 1, Md); P(9, 12, 1, 1, Md); P(12, 12, 1, 1, Md);
    if (busy) P(3 + (f % 4) * 3, 11, 1, 1, Md);
    P(4, 10, 8, 1, Dk); P(5, 6, 6, 4, L); P(6, 7, 4, 1, C); decal(6, 9);
    P(6, 3, 4, 3, Md); eyes(6, 4, 4); top(8, 3);
  } else if (b === 'tripod') {
    P(5, 3, 6, 1, L); P(4, 4, 8, 4, L); P(5, 8, 6, 1, Md); P(4, 6, 8, 1, C);
    eyes(4, 4, 8);
    const k = busy ? (f % 2) : 0;
    P(5, 9, 1, 2, Md); P(4, 11, 1, 3 + k, Md); P(8, 9, 1, 5, Md); P(10, 9, 1, 2, Md); P(11, 11, 1, 3 - k, Md);
    P(3, 14, 2, 1, Dk); P(7, 14, 3, 1, Dk); P(11, 14, 2, 1, Dk);
    top(8, 3);
  } else if (b === 'hover') {
    P(6, 4, 4, 1, L); P(5, 5, 6, 3, L); eyes(5, 5, 6);
    P(2, 8, 12, 1, Md); P(1, 9, 14, 1, Md); P(2, 10, 12, 1, Dk); P(3, 9, 10, 1, C); decal(6, 8);
    if (on) { P(6, 11, 4, 1, (f % 2) ? '#4de1ff' : '#9df4ff'); P(7, 12, 2, 1, 'rgba(77,225,255,.5)'); }
    top(8, 4);
  } else if (b === 'spider') {
    P(4, 6, 7, 4, Dk); P(5, 7, 5, 2, Md); P(10, 6, 3, 3, Dk);
    P(11, 7, 1, 1, eye); P(12, 7, 1, 1, eye); P(11, 8, 1, 1, eye);
    const k = busy ? (f % 2) : 0;
    P(3, 9, 1, 2, Md); P(2, 11 + k, 1, 3, Md); P(6, 10, 1, 4 - k, Md); P(9, 10, 1, 4, Md); P(12, 9, 1, 2, Md); P(13, 11 - k, 1, 3, Md);
    P(6, 6, 2, 1, C); top(7, 6);
  } else if (b === 'tower') {
    P(6, 2, 4, 3, L); eyes(6, 3, 4);
    P(7, 5, 2, 8, Md); P(6, 6, 4, 1, C); P(6, 9, 4, 1, C2);
    if (busy) P(7, 6 + (f % 4) * 2, 2, 1, '#ffffff');
    P(4, 13, 8, 1, Dk); P(5, 14, 6, 1, Dk); top(8, 2);
  } else if (b === 'box') {
    P(3, 4, 10, 9, Md); P(3, 4, 10, 1, L); P(4, 6, 8, 4, Dk); P(5, 7, 6, 2, C);
    if (on) P(5 + (busy ? f % 6 : 2), 7, 1, 2, '#ffffff');
    eyes(3, 5, 10); decal(4, 11);
    const k = busy ? (f % 2) : 0;
    P(4, 13, 2, 1 + k, Dk); P(10, 13, 2, 2 - k, Dk); top(8, 4);
  } else if (b === 'orb') {
    P(6, 3, 4, 1, L); P(5, 4, 6, 1, L); P(4, 5, 8, 4, L); P(5, 9, 6, 1, Md); P(6, 10, 4, 1, Md);
    const ry = busy ? [7, 7, 6, 6][f % 4] : 7;
    P(1, ry, 14, 1, C); P(3, ry, 2, 1, C2);
    eyes(4, 5, 8); top(8, 3);
  } else {
    P(2, 7, 2, 7, Md); P(12, 7, 2, 7, Md); P(1, 13, 3, 2, Dk); P(12, 13, 3, 2, Dk);
    P(4, 8, 8, 5, L); P(4, 13, 8, 1, Md);
    P(5, 9, 2, 3, C); P(9, 9, 2, 1, C2); P(9, 11, 2, 1, Dk); P(7, 10, 1, 2, Dk);
    P(4, 7, 8, 1, Md);
    P(5, 2, 6, 1, Md); P(4, 3, 8, 1, Md); P(3, 4, 10, 3, Md);
    P(5, 2, 2, 1, C); P(9, 3, 2, 1, C2); P(3, 6, 10, 1, C);
    eyes(4, 4, 8);
    P(10, 4, 1, 1, lamp);
    if (busy && f % 3 === 0) { P(14, 9, 1, 1, '#ffd23f'); P(15, 8, 1, 1, '#ffffff'); }
    top(8, 2);
  }
  ctx.restore();
  if (st === 'ask' && on && f % 2) { P(6, 0, 4, 1, 'rgba(217,166,255,.6)'); P(7, 1, 2, 1, 'rgba(217,166,255,.4)'); }
}
function px(x, y, w, h, c, ctx) { ctx.fillStyle = c; ctx.fillRect(x, y, w, h); }
function drawAgent(ctx, sp, st, f) {
  ctx.clearRect(0, 0, 16, 16);
  const P = (x, y, w, h, c) => px(x, y, w, h, c, ctx);
  P(4, 14, 8, 1, 'rgba(0,0,0,.45)');
  let bob = 0;
  if (st === 'busy') bob = (f % 2);
  else if (st === 'happy' || st === 'ask') bob = (f % 4 < 2) ? 0 : -1;
  ctx.save(); ctx.translate(0, bob);
  // feet
  if (st === 'busy') { P(5, 13 - (f % 2), 2, 1, sp.d); P(9, 13 - ((f + 1) % 2), 2, 1, sp.d); }
  else { P(5, 13, 2, 1, sp.d); P(9, 13, 2, 1, sp.d); }
  // body
  P(4, 5, 8, 1, sp.c); P(3, 6, 10, 6, sp.c); P(4, 12, 8, 1, sp.d); P(12, 7, 1, 5, sp.d);
  P(4, 6, 2, 1, 'rgba(255,255,255,.45)');
  // accessory
  if (sp.crown) { P(5, 3, 6, 2, '#ffd23f'); P(5, 2, 1, 1, '#ffd23f'); P(7, 2, 2, 1, '#ffd23f'); P(10, 2, 1, 1, '#ffd23f'); P(7, 3, 2, 1, '#ff4d6d'); }
  else if (sp.acc === 'antenna') { P(8, 2, 1, 3, '#cfc6ff'); P(7, 1, 3, 1, sp.ac); P(8, 0, 1, 1, sp.ac); }
  else if (sp.acc === 'headset') { P(4, 4, 8, 1, '#2b2747'); P(2, 7, 1, 3, sp.ac); P(13, 7, 1, 3, sp.ac); }
  else if (sp.acc === 'leaf') { P(8, 3, 1, 2, '#3c9a2a'); P(9, 2, 2, 1, '#5ef08b'); P(9, 3, 1, 1, '#5ef08b'); }
  else if (sp.acc === 'horns') { P(4, 3, 1, 2, '#f3f0ff'); P(11, 3, 1, 2, '#f3f0ff'); }
  else if (sp.acc === 'cap') { P(4, 3, 8, 2, sp.ac); P(10, 4, 4, 1, sp.ac); }
  else if (sp.acc === 'bow') { P(9, 3, 1, 2, sp.ac); P(12, 3, 1, 2, sp.ac); P(10, 4, 2, 1, sp.ac); }
  // face
  const E = '#160c2a';
  const blink = ((f + sp.seed) % 31) === 0;
  if (st === 'sleep') { P(5, 9, 2, 1, E); P(9, 9, 2, 1, E); }
  else if (st === 'happy') { P(5, 9, 1, 1, E); P(6, 8, 1, 1, E); P(7, 9, 1, 1, E); P(9, 9, 1, 1, E); P(10, 8, 1, 1, E); P(11, 9, 1, 1, E); P(7, 10, 2, 1, sp.d); }
  else if (blink) { P(6, 9, 1, 1, E); P(9, 9, 1, 1, E); }
  else if (st === 'ask') { P(6, 7, 1, 2, E); P(9, 7, 1, 2, E); P(7, 10, 2, 2, sp.d); }
  else if (st === 'busy') { P(6, 9, 1, 2, E); P(9, 9, 1, 2, E); }
  else { P(6, 8, 1, 2, E); P(9, 8, 1, 2, E); }
  ctx.restore();
  // props
  if (st === 'busy') {
    P(1, 10, 5, 3, '#2a2260'); P(2, 11, 3, 1, (f % 2) ? '#4de1ff' : '#9dff57'); P(0, 13, 7, 1, '#cfc6ff');
    const sx = [13, 14, 12, 15][f % 4], sy = [3, 2, 1, 4][f % 4];
    P(sx, sy, 1, 1, '#ffd23f');
  }
}
// 10-16 «персонаж должен меняться на персонажей из space по мере уровней»:
// the commander grows from a desert kid in the pink cap to a grand master.
const CMDR = [[1, 'kid'], [3, 'mechanic'], [5, 'pilot'], [8, 'smuggler'], [12, 'padawan'], [16, 'knight'],
  [20, 'general'], [25, 'master'], [30, 'grand']];
function cmdrKind(lv) { let k = 'kid'; CMDR.forEach(([at, n]) => { if ((lv || 1) >= at) k = n; }); return k; }
function drawCmdr(ctx, f, lv) {
  ctx.clearRect(0, 0, 16, 16);
  const P = (x, y, w, h, c) => px(x, y, w, h, c, ctx);
  const k = cmdrKind(lv);
  const skin = '#f2c29b', hair = '#3a2418', E = '#1a1030', mouth = '#b5654a';
  const blink = (f % 29) === 0;
  const face = (y0) => {
    P(4, y0, 8, 8, skin);
    if (blink) { P(6, y0 + 4, 1, 1, E); P(9, y0 + 4, 1, 1, E); } else { P(6, y0 + 3, 1, 2, E); P(9, y0 + 3, 1, 2, E); }
    P(6, y0 + 6, 4, 1, mouth);
  };
  const saber = (x, col) => {
    const hum = (f % 4 < 2) ? col : '#ffffff';
    P(x, 2, 1, 9, hum); P(x - 1, 3, 1, 7, col + '55'); P(x + 1, 3, 1, 7, col + '55'); P(x, 11, 1, 3, '#9aa0b8');
  };
  if (k === 'kid' || k === 'mechanic') {
    P(3, 14, 10, 2, k === 'kid' ? '#d9b98c' : '#ff8a3d'); P(6, 13, 4, 1, skin);
    face(5); P(3, 6, 1, 4, hair); P(12, 6, 1, 4, hair);
    P(3, 3, 10, 3, '#ff5fa2'); P(4, 2, 8, 1, '#ff5fa2'); P(10, 5, 5, 1, '#c2306f'); P(5, 3, 2, 1, '#ff9cc8');
    if (k === 'mechanic') { P(13, 10, 1, 4, '#9aa0b8'); P(12, 10, 3, 1, '#9aa0b8'); P(3, 14, 2, 2, '#c45f1c'); }
  } else if (k === 'pilot') {
    P(3, 14, 10, 2, '#ff8a3d'); P(5, 14, 6, 1, '#ffffff'); P(6, 13, 4, 1, skin);
    face(6);
    P(3, 2, 10, 4, '#f3f0ff'); P(2, 4, 1, 7, '#f3f0ff'); P(13, 4, 1, 7, '#f3f0ff'); P(4, 1, 8, 1, '#f3f0ff');
    P(3, 3, 10, 1, '#ff4d6d'); P(7, 1, 2, 4, '#ff4d6d'); P(2, 7, 1, 2, '#4de1ff'); P(13, 7, 1, 2, '#4de1ff');
  } else if (k === 'smuggler') {
    P(3, 14, 10, 2, '#f3f0ff'); P(3, 14, 3, 2, '#1f1a35'); P(10, 14, 3, 2, '#1f1a35'); P(6, 13, 4, 1, skin);
    face(5); P(4, 3, 8, 2, hair); P(3, 4, 1, 4, hair); P(12, 4, 1, 3, hair); P(5, 2, 6, 1, hair);
    P(13, 13, 2, 3, '#3a3456');
  } else if (k === 'padawan') {
    P(3, 14, 10, 2, '#e9d8b4'); P(7, 14, 2, 2, '#b49a6a'); P(6, 13, 4, 1, skin);
    face(5); P(4, 3, 8, 2, '#8a5a2b'); P(4, 5, 1, 1, '#8a5a2b'); P(12, 5, 1, 1, '#8a5a2b');
    P(12, 6, 1, 6, '#8a5a2b'); P(12, 12, 1, 1, '#ffd23f');
    P(14, 11, 1, 3, '#9aa0b8');
  } else if (k === 'knight' || k === 'master') {
    const robe = '#7a5634', hood = '#5d3f26';
    P(2, 13, 12, 3, robe); P(6, 13, 4, 1, skin); P(7, 14, 2, 2, '#e9d8b4');
    if (k === 'master') { P(3, 2, 10, 4, hood); P(2, 4, 2, 9, hood); P(12, 4, 2, 9, hood); face(5); P(4, 5, 8, 1, hood); }
    else { face(5); P(4, 3, 8, 2, '#c99a5b'); P(3, 4, 1, 4, '#c99a5b'); P(12, 4, 1, 4, '#c99a5b'); P(2, 12, 3, 2, hood); P(11, 12, 3, 2, hood); }
    saber(14, k === 'master' ? '#9dff57' : '#4de1ff');
  } else if (k === 'general') {
    P(3, 14, 10, 2, '#d5dbee'); P(5, 14, 2, 1, '#ff4d6d'); P(9, 14, 2, 1, '#4de1ff'); P(6, 13, 4, 1, skin);
    face(5); P(3, 2, 10, 3, '#8f97bd'); P(3, 5, 10, 1, '#565d86'); P(7, 3, 2, 1, '#ffd23f');
  } else {
    const aura = ['#ffd23f', '#fff3b0', '#ffd23f'][f % 3];
    P(1, 1, 1, 1, aura); P(14, 2, 1, 1, aura); P(0, 9, 1, 1, aura); P(15, 7, 1, 1, aura);
    P(2, 13, 12, 3, '#ece8ff'); P(6, 13, 4, 1, skin);
    P(3, 2, 10, 4, '#ece8ff'); P(2, 4, 2, 9, '#ece8ff'); P(12, 4, 2, 9, '#ece8ff');
    face(5); P(4, 5, 8, 1, '#ece8ff');
    saber(14, '#b36bff');
  }
}
const SPRITES = new Map();   // canvas -> {sp, st} or {cmdr:true}
let FRAME = 0;
function spriteTick() {
  FRAME++;
  SPRITES.forEach((v, cv) => {
    if (!cv.isConnected) { SPRITES.delete(cv); return; }
    if (cv.offsetParent === null && cv.id !== 'me') return;
    const ctx = cv.getContext('2d');
    if (v.cmdr) drawCmdr(ctx, FRAME, v.lv || (G && G.lv) || 1);
    else if (v.ship) { ctx.clearRect(0, 0, 48, 26); ctx.save(); ctx.translate(24, 13); drawShip(ctx, FRAME); ctx.restore(); }
    else (CREW === 'bots' ? drawBot : drawAgent)(ctx, v.sp, v.st, FRAME + (v.sp.seed || 0));
  });
}
setInterval(spriteTick, 140);

// ------------------------------------------------------------------ fx
// 10-16 «ракета нравится, но это должен быть космический корабль из space»:
// a pixel starfighter, nose to +x, 2 px cells, centred on 0,0.
const SHIP = ['....GGGGGGGGGGGGG.......', '...WWWWWR...............', '...WWWWWRR..............', '..OWWWWWWRR.............',
  '..OWWWWWWWWW............', '.SSSSSSSSSSSSSSSSSKK....', '.SSSSSSSSSSSSSSSSSSSSWW.', '.SSSSSSSSSSSSSSSSSKK....',
  '..OWWWWWWWWW............', '..OWWWWWWRR.............', '...WWWWWRR..............', '...WWWWWR...............',
  '....GGGGGGGGGGGGG.......'];
const SHIPC = { G: '#8a83b9', W: '#ece8ff', R: '#ff4d6d', S: '#c9c2ee', K: '#2a2260' };
function drawShip(c, f) {
  for (let y = 0; y < SHIP.length; y++) for (let x = 0; x < SHIP[y].length; x++) {
    const ch = SHIP[y][x];
    if (ch === '.') continue;
    c.fillStyle = ch === 'O' ? (f % 2 ? '#ffb547' : '#ff5fa2') : SHIPC[ch];
    c.fillRect((x - 12) * 2, (y - 6) * 2, 2, 2);
  }
}
const FX = {
  cv: null, ctx: null, parts: [], on: false,
  init() { this.cv = $('#fx'); this.ctx = this.cv.getContext('2d'); this.size(); window.addEventListener('resize', () => this.size()); },
  size() { const r = window.devicePixelRatio || 1; this.cv.width = innerWidth * r; this.cv.height = innerHeight * r; this.ctx.setTransform(r, 0, 0, r, 0, 0); },
  burst(x, y, n, colors, power, grav) {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    for (let i = 0; i < n; i++) {
      const a = Math.random() * Math.PI * 2, s = (power || 6) * (0.4 + Math.random());
      this.parts.push({ x, y, vx: Math.cos(a) * s, vy: Math.sin(a) * s - (power || 6) * 0.6, g: grav == null ? 0.25 : grav,
        c: colors[i % colors.length], life: 50 + Math.random() * 40, sz: 3 + Math.floor(Math.random() * 3) });
    }
    this.run();
  },
  confetti(n) { this.burst(innerWidth / 2, innerHeight * 0.4, n || 120, ['#ff5fa2', '#ffd23f', '#9dff57', '#4de1ff', '#b36bff', '#ff8a3d'], 11); },
  rocket() {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    this.parts.push({ rocket: true, x: -60, y: innerHeight * (0.55 + Math.random() * 0.25), vx: 11, vy: -3.2, life: 220 });
    this.run();
  },
  bolt(x0, y0, x1, y1, color) {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const cx = (x0 + x1) / 2 + (Math.random() - 0.5) * 120, cy = Math.min(y0, y1) - 40;
    this.parts.push({ bolt: true, x0, y0, x1, y1, cx, cy, t: 0, n: 26, c: color || '#4de1ff', trail: [], life: 999 });
    for (let i = 0; i < 14; i++) {        // light-speed lines out of the launch point
      const a = -Math.PI / 2 + (Math.random() - 0.5) * 2.2, v = 6 + Math.random() * 7;
      this.parts.push({ streak: true, x: x0, y: y0, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 16 + Math.random() * 10, c: i % 3 ? '#ffffff' : (color || '#4de1ff') });
    }
    this.run();
  },
  run() { if (this.on) return; this.on = true; requestAnimationFrame(() => this.step()); },
  step() {
    const c = this.ctx; c.clearRect(0, 0, innerWidth, innerHeight);
    const keep = [];
    for (const p of this.parts) {
      if (p.bolt) {
        p.t++;
        const u = Math.min(1, p.t / p.n), v = 1 - u;
        const x = v * v * p.x0 + 2 * v * u * p.cx + u * u * p.x1, y = v * v * p.y0 + 2 * v * u * p.cy + u * u * p.y1;
        p.trail.push([x, y]); if (p.trail.length > 12) p.trail.shift();
        c.lineCap = 'square';
        for (let i = 1; i < p.trail.length; i++) {
          c.globalAlpha = i / p.trail.length; c.strokeStyle = p.c; c.lineWidth = 2 + i / 2;
          c.beginPath(); c.moveTo(p.trail[i - 1][0], p.trail[i - 1][1]); c.lineTo(p.trail[i][0], p.trail[i][1]); c.stroke();
        }
        c.globalAlpha = 1; c.fillStyle = '#ffffff'; c.fillRect(Math.round(x) - 3, Math.round(y) - 3, 6, 6);
        if (u < 1) keep.push(p);
        else { this.burst(p.x1, p.y1, 18, [p.c, '#ffffff', '#ffd23f'], 4, 0.08); if (p.done) p.done(); }
        continue;
      }
      if (p.streak) {
        p.life--; p.x += p.vx; p.y += p.vy;
        c.globalAlpha = Math.max(0, p.life / 26); c.strokeStyle = p.c; c.lineWidth = 2;
        c.beginPath(); c.moveTo(p.x, p.y); c.lineTo(p.x - p.vx * 3, p.y - p.vy * 3); c.stroke();
        if (p.life > 0) keep.push(p);
        continue;
      }
      p.life--; p.x += p.vx; p.y += p.vy;
      if (p.rocket) {
        p.vy += 0.03;
        const an = Math.atan2(p.vy, p.vx);
        c.save(); c.translate(p.x, p.y); c.rotate(an); c.scale(1.5, 1.5); drawShip(c, p.life); c.restore();
        const rx = p.x - Math.cos(an) * 32, ry = p.y - Math.sin(an) * 32;
        if (p.life % 2 === 0) keep.push({ x: rx, y: ry + (Math.random() - 0.5) * 14, vx: -Math.cos(an) * 2, vy: -Math.sin(an) * 2, g: 0, c: ['#ffb547', '#ff5fa2', '#ffffff'][p.life % 3], life: 26, sz: 3 });
        if (p.life % 14 === 0) keep.push({ streak: true, x: p.x + Math.cos(an) * 30, y: p.y + Math.sin(an) * 30, vx: Math.cos(an) * 18, vy: Math.sin(an) * 18, life: 22, c: '#ff4d6d' });
        if (p.life > 0 && p.x < innerWidth + 60 && p.y > -60) keep.push(p);
        continue;
      }
      p.vy += p.g; p.vx *= 0.985;
      if (p.life > 0 && p.y < innerHeight + 20) { c.globalAlpha = Math.min(1, p.life / 25); c.fillStyle = p.c; c.fillRect(Math.round(p.x), Math.round(p.y), p.sz, p.sz); keep.push(p); }
    }
    c.globalAlpha = 1;
    this.parts = keep;
    if (keep.length) requestAnimationFrame(() => this.step()); else { this.on = false; c.clearRect(0, 0, innerWidth, innerHeight); }
  },
};
function rectCenter(el) { const r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }
function floatXP(text, el) {
  const d = document.createElement('div');
  d.className = 'floatxp'; d.textContent = text;
  const r = (el || $('.xpbar')).getBoundingClientRect();
  d.style.left = (r.left + r.width / 2 - 16) + 'px'; d.style.top = (r.top + r.height) + 'px';
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 1300);
}

// ------------------------------------------------------------------ toasts, banner, modal
function toast(o) {
  const box = $('#toasts');
  const t = document.createElement('div');
  t.className = 'toast ' + (o.cls || '');
  t.innerHTML = '<div class="ti">' + icoOf(o.i || 'bot') + '</div><div><b>' + esc(o.t || '') + '</b>'
    + (o.x ? '<small>' + esc(o.x) + '</small>' : '') + '</div>'
    + (o.btn ? '<button class="tbtn">' + esc(o.btn[0]) + '</button>' : o.xp ? '<div class="tx">+' + o.xp + '</div>' : '<div></div>');
  t.onclick = e => {
    if (o.btn && e.target.closest('.tbtn')) { e.stopPropagation(); o.btn[1](); kill(); return; }
    if (o.sid) openAgent(o.sid);
    kill();
  };
  function kill() { t.classList.add('out'); setTimeout(() => t.remove(), 320); }
  box.appendChild(t);
  while (box.children.length > 3) box.firstChild.remove();
  setTimeout(kill, o.ms || 6500);
}
function banner(big, small) {
  const b = $('#banner');
  b.innerHTML = '<b>' + esc(big) + '</b>' + (small ? '<span>' + esc(small) + '</span>' : '');
  b.classList.remove('on'); void b.offsetWidth; b.classList.add('on');
}
let MODAL_CB = null;
function confirmBox(text, yes, cb) {
  $('#mtext').textContent = text; $('#myes').textContent = yes || 'OK';
  MODAL_CB = cb; $('#modal').hidden = false; $('#myes').focus(); SFX.play('open');
}
function modalClose(ok) { $('#modal').hidden = true; const cb = MODAL_CB; MODAL_CB = null; SFX.play(ok ? 'click' : 'close'); if (cb) cb(ok); }

// ------------------------------------------------------------------ server
// A stalled connection (lossy Tailscale path) must not freeze the board: every
// request gets a deadline, and the next poll opens a fresh one.
async function fetchT(url, opts, ms) {
  const ac = typeof AbortController === 'function' ? new AbortController() : null;
  const tm = ac ? setTimeout(() => ac.abort(), ms) : null;
  try { return await fetch(url, Object.assign({}, opts || {}, ac ? { signal: ac.signal } : {})); }
  finally { if (tm) clearTimeout(tm); }
}
// 17.11 the owner: «я не вижу переключения на девайсы в нашей основной панели, я также хочу заходить в акки и менять
// их на двух других машинах». ?dev=<id> puts the whole HQ on that machine: the board and limits come from its
// /api/state through the hub (/api/game?dev=), every /act, /api/auth, search and the chat carry dev, so sign-in,
// account switch, launch and typing happen THERE. The pilot (XP, trophies) stays this one. Switching reloads.
const HQ_DEV = (() => { const v = new URLSearchParams(location.search).get('dev') || ''; return v === 'local' ? '' : v; })();
function devq(sep) { return HQ_DEV ? sep + 'dev=' + encodeURIComponent(HQ_DEV) : ''; }
function devName(id) {
  const k = id === undefined ? (HQ_DEV || 'local') : (id || 'local');
  const d = ((G && G.devices) || []).find(x => x.id === k);
  return d ? d.name : (k === 'local' ? 'this computer' : k);
}
async function act(action, target, extra) {
  try {
    const r = await fetchT('/act', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ action, target }, HQ_DEV ? { dev: HQ_DEV } : {}, extra || {})) }, 60000);
    const j = await r.json();
    if (!j.ok) { SFX.play('error'); toast({ i: 'warning-box', t: action + ' failed', x: j.msg, cls: 'bad', ms: 9000 }); }
    return j;
  } catch (e) {
    SFX.play('error'); toast({ i: 'cellular-signal-off', t: 'No connection', x: String(e), cls: 'bad' });
    return { ok: false, msg: String(e) };
  }
}
// the server sends a slow-changing part once, then only its signature (game_diet)
const DIET = {};
function HAVE() { return Object.keys(DIET).map(k => k + ':' + DIET[k].sig).join(','); }
function dietMerge(j) {
  const sigs = j.sigs || {};
  Object.keys(sigs).forEach(k => {
    if (k in j) DIET[k] = { sig: sigs[k], v: j[k] };
    else if (DIET[k] && DIET[k].sig === sigs[k]) j[k] = DIET[k].v;
  });
  const txt = j.rows_txt || {};
  const K = ['title', 'name', 'summary', 'last_a', 'last_u'];
  (j.rows || []).forEach(r => { const t = txt[r.short]; if (t) K.forEach((k, i) => { if (!(k in r)) r[k] = t[i]; }); });
}
// the chat inside does not poll by itself: it gets the state from here
function pushChat(j) {
  try {
    const cw = $('#chat').contentWindow;
    if (!IFRAME_READY || !cw || !cw.hqPulse) return;
    const rows = (j.rows || []).map(r => ({ short: r.short, live: r.live, busy: r.busy, busy_age: r.busy_age, la_ts: r.la_ts, state: r.state,
      retired: r.retired, name: r.name, effort: r.effort, tui: { kind: r.tui || '', detail: '' },
      digest: { waiting: r.waiting, ctx: r.ctx, ctx_max: r.ctx_max, model_id: r.model_id } }));
    cw.hqPulse({ rows, meta: j.meta || {} }, j.ui_classic || '');
  } catch (e) {}
}
async function poll() {
  clearTimeout(POLL_T);
  const started = Date.now();
  try {
    const fq = ++FLEET.req;
    const r = await fetchT('/api/game?since=' + SEQ + '&have=' + encodeURIComponent(HAVE()) + (FLEET.on ? '&fleet=' + FLEET.v : '') + devq('&'), { cache: 'no-store' }, HQ_DEV ? 25000 : 15000);
    const j = await r.json();
    if (j.ok) {
      dietMerge(j);
      // two polls can overlap (a click asks for one at once): an older answer must not undo a newer one
      if (j.fleet && fq >= FLEET.got) { FLEET.got = fq; flMerge(j.fleet); }
      j.ui_classic = r.headers.get('X-Panel-UI') || ''; apply(j, started); pushChat(j);
    }
    else if (FIRST) toast({ i: 'warning-box', t: 'HQ offline', x: j.msg || '', cls: 'bad' });
    if (j.ui && MY_UI && j.ui !== MY_UI) uiChanged();
  } catch (e) {}
  POLL_T = setTimeout(poll, document.hidden ? 15000 : 3000);
}
function uiChanged() {
  const typing = (function () { try { const t = $('#chat').contentWindow.document.getElementById('drtext'); return t && t.value.trim(); } catch (e) { return false; } })();
  if (document.hidden && !typing) { location.reload(); return; }
  if (UI_NOTED) return;
  UI_NOTED = true;
  toast({ i: 'reload', t: 'HQ got an update', x: 'Tap to reload', ms: 60000, sid: null });
  const last = $('#toasts').lastChild;
  if (last) last.onclick = () => location.reload();
}

// ------------------------------------------------------------------ apply
// 17.11: the peers' account line is the LAST one they knew. The peer machines had not read their limits for
// 45 days and every session there answered «Failed to authenticate», while this box said ACTIVE NOW.
function devAuthTrouble() {
  if (!G || !G.remote) return '';
  const rows = (G.rows || []).concat(G.sleeping || []);
  const bad = /Failed to authenticate|Login expired|Please run \/login|Not logged in|OAuth session expired/i;
  if (rows.some(r => bad.test([r.summary, r.last_a, r.title, r.name].join(' ')))) return 'Claude on ' + devName() + ' is signed out: its sessions answer «Failed to authenticate». Sign in below.';
  const ts = (G.meta && G.meta.usage_ts) || 0, age = Date.now() / 1000 - ts;
  if (age > 6 * 3600) return 'No limits reading from ' + devName() + (ts ? ' for ' + Math.max(1, Math.round(age / 86400)) + ' days' : ' yet') + ': Claude there is probably signed out, and the 5H and WEEK bars are old. Sign in below.';
  return '';
}
let DEV_WARNED = false;
let DEV_SIG = '';
function devDraw() {
  const trouble = devAuthTrouble();
  $('#devbtn').classList.toggle('trouble', !!trouble);
  if (trouble && !DEV_WARNED) { DEV_WARNED = true; toast({ i: 'warning-box', t: devName() + ': sign-in needed', x: trouble + ' Open the account (A).', cls: 'bad', ms: 12000 }); }
  const list = (G && G.devices) || [];
  const sig = list.map(d => d.id + ':' + d.name).join('|') + '#' + HQ_DEV;
  if (sig === DEV_SIG) return;
  DEV_SIG = sig;
  const b = $('#devbtn');
  b.hidden = list.length < 2 && !HQ_DEV;
  b.innerHTML = ico('server') + '<span>' + esc(devName().toUpperCase()) + '</span>';
  b.title = 'Machine: ' + devName() + ' · switch the whole HQ to another one';
  b.classList.toggle('remote', !!HQ_DEV);
  document.body.classList.toggle('remote', !!HQ_DEV);
  $('#devlist').innerHTML = list.map(d => {
    const cur = d.id === (HQ_DEV || 'local');
    return '<button class="hb wide devpick' + (cur ? ' on' : '') + '" data-dev="' + esc(d.id) + '">' + ico('server')
      + '<span>' + esc(d.name) + '</span><em>' + esc(d.id === 'local' ? 'THIS MAC · HUB' : (d.kind || 'peer')) + '</em></button>';
  }).join('');
}
function showDev(on) {
  $('#devm').hidden = !on; SFX.play(on ? 'open' : 'close');
  if (on) { DEV_SIG = ''; devDraw(); const c = $('#devlist .on') || $('#devlist button'); if (c) c.focus({ preventScroll: true }); }
}
$('#devbtn').onclick = () => showDev(true);
$('#devok').onclick = () => showDev(false);
$('#devm').addEventListener('click', e => {
  if (e.target.id === 'devm') { showDev(false); return; }
  const pk = e.target.closest('[data-dev]');
  if (!pk) return;
  const id = pk.dataset.dev;
  if (id === (HQ_DEV || 'local')) { showDev(false); return; }
  SFX.play('click');
  location.href = '/play' + (id === 'local' ? '' : '?dev=' + encodeURIComponent(id));
});
function apply(j, started) {
  const prevLv = LAST_LV, prevRank = LAST_RANK;
  held(j);
  G = j;
  devDraw();
  FLOW = { v: j.flow, at: NOW() };
  hud(j);
  accShow();
  // while a file is dragged the cards hold still: a card that jumps lanes
  // under the cursor would leave its drop target behind (dropClose redraws)
  if (!DROP.on) board();
  trophiesDraw(j);
  stageBar();
  const evs = j.events || [];
  if (!FIRST) evs.forEach(react);
  logAdd(evs, FIRST);
  SEQ = Math.max(SEQ, j.seq || 0);
  if (prevLv !== null && j.lv > prevLv) levelUp(j, prevRank !== j.rank);
  LAST_LV = j.lv; LAST_RANK = j.rank;
  FIRST = false;
  if (FLEET.on) flDraw();
}

function hud(j) {
  $('#lv').textContent = 'LV ' + j.lv;
  $('#rank').textContent = j.rank;
  $('#cmdr').title = 'Commander · ' + j.rank + (j.next_rank ? ' · next: ' + j.next_rank : '') + ' · ' + j.xp + ' XP total';
  const span = Math.max(1, j.hi - j.lo), have = j.xp - j.lo;
  $('#xpfill').style.width = clamp(have * 100 / span, 0, 100) + '%';
  $('#xptxt').textContent = have + ' / ' + span + ' XP';
  $('#xpnum').textContent = have + '/' + span;
  const st = $('#streak');
  st.querySelector('b').textContent = j.streak;
  st.classList.toggle('zero', !j.streak);
  st.title = j.streak ? j.streak + ' days in a row with every must-do closed' : 'Close every must-do of the day to start a streak';
  flowDraw();
  const m = j.meta || {}, lim = m.limits || {}, rs = j.resets || {};
  power($('#pw5h'), lim.session, rs.session, 5 * 3600, '5-hour window');
  power($('#pwwk'), lim.week, rs.week, 7 * 86400, 'Weekly limit');
  accLims();
  vitals(m);
  const d = j.today || {};
  const tb = (ic, n, tip) => '<span class="tstat" title="' + tip + '">' + ico(ic) + '<b>' + (n || 0) + '</b></span>';
  $('#today').innerHTML = '<span class="tlab">TODAY</span>' + tb('arrow-right', d.cmds, 'commands sent') + tb('check', d.turns, 'results turned in')
    + tb('git-commit', d.commits, 'commits') + tb('rocket', (d.deploys || 0) + (d.publishes || 0), 'deploys and publishes')
    + tb('users', d.peak, 'most agents working at once');
}
// Life support (the owner 10-16: «нет нигде нагрузки RAM и активных сессий — сделать
// как жизнедеятельность корабля»): memory, reactor (load), crew (live sessions)
// and the cargo hold (disk), each a LED gauge that turns gold and red.
function vitals(m) {
  const set = (id, pct, val, lvl, tip) => {
    const el = $('#' + id);
    if (!el) return;
    el.className = 'vt ' + lvl;
    el.querySelector('u').style.width = clamp(pct, 0, 100) + '%';
    el.querySelector('b').textContent = val;
    el.title = tip;
  };
  const S = sysLevels(m), a = S.mem, c = S.cpu, w = S.crew, d = S.disk;
  set('vt-mem', a.pct, a.used.toFixed(1) + 'G', a.lvl, 'Life support (memory) ' + a.used.toFixed(1) + ' of ' + Math.round(a.tot) + ' GB, pressure '
    + (PRESS[a.pr] || a.pr) + ', swap ' + a.swap.toFixed(1) + ' GB · click: ship systems');
  set('vt-cpu', c.pct, c.load.toFixed(1), c.lvl, 'Reactor (CPU load) ' + c.load.toFixed(1) + ', the queue holds new sessions above ' + c.gate + ' · click: ship systems');
  set('vt-crew', w.pct, w.live + '/' + w.cap, w.lvl, 'Crew: ' + w.live + ' live sessions of ' + w.cap + ' slots, ' + w.busy + ' working right now · click: ship systems');
  if (d) set('vt-disk', d.pct, Math.round(d.free) + 'G', d.lvl, 'Cargo hold (disk): ' + d.free + ' GB free of ' + d.dt + '. Under 10 GB sessions start failing to save · click to clean up');
  $('#vitals').classList.toggle('alarm', [a, c, w, d].some(v => v && v.lvl === 'hot'));
  sysDraw();
}
function power(el, used, resetTs, win, name) {
  const fill = el.querySelector('.bar i'), num = el.querySelector('b'), em = el.querySelector('em'), pace = el.querySelector('.pace');
  // 05.11: the account has no 5-hour window at all (/usage and the API give none), only the week
  if (used == null) { num.textContent = '—'; fill.style.width = '0'; em.textContent = win < 86400 ? 'NO CAP' : ''; em.className = ''; el.classList.remove('paced'); el.title = win < 86400 ? name + ': this account has no 5-hour cap right now, only the weekly one counts' : name; return; }
  fill.style.width = clamp(used, 0, 100) + '%';
  num.textContent = used + '%';
  el.classList.toggle('hot', used >= 85);
  el.classList.toggle('warm', used >= 65 && used < 85);
  if (resetTs) {
    const frac = clamp((NOW() - (resetTs - win)) / win, 0, 1);
    pace.style.left = 'calc(' + (frac * 100).toFixed(1) + '% - 1px)';
    el.classList.add('paced');
    const diff = used - frac * 100;
    em.textContent = diff < -15 ? 'UNUSED' : diff > 10 ? 'FAST' : 'ON PACE';
    em.className = diff < -15 ? 'under' : diff > 10 ? 'over' : '';
    el.title = name + ': ' + used + '% used, ' + Math.round(frac * 100) + '% of the window gone, resets ' + new Date(resetTs * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })
      + (diff < -15 ? '. Unused power: launch more agents.' : diff > 10 ? '. Ahead of pace: you will hit the limit early.' : '. On pace.');
  }
}
let FLOW_M = null;
function flowDraw() {
  const v = Math.max(0, FLOW.v - (NOW() - FLOW.at) / 15);
  const el = $('#flow');
  el.querySelector('.bar i').style.width = v + '%';
  const m = v >= 80 ? 2 : v >= 50 ? 1.5 : 1;
  if (m === 2 && FLOW_M < 2 && FLOW_M !== null) hype('flow', 'mid', el);
  FLOW_M = m;
  el.querySelector('.mult').textContent = '×' + m;
  el.classList.toggle('hot', v >= 50);
  el.classList.toggle('max', v >= 80);
}
setInterval(flowDraw, 1000);

// ------------------------------------------------------------------ board
const LANES = [['ask', 'question', 'ASKING YOU'], ['fresh', 'alert', 'YOUR MOVE'], ['busy', 'loader', 'ON A MISSION'],
  ['idle', 'clock', 'STANDBY'], ['sleep', 'moon', 'HANGAR']];
// When the agent last handed back a result. A turn a background task started
// (render monitors and such) is not a result; HQ only falls back to the last
// transcript write while it has not seen a single turn of this agent end yet.
function doneTs(r, a) {
  if (a && (a.done || a.ping)) return a.done || 0;
  return (r && !r.busy && r.la_ts) || 0;
}
function cmdTs(a) { return a ? Math.max(a.cmd || 0, a.hum || 0) : 0; }
function laneOf(r, a) {
  if (!r.live) return 'sleep';
  if (r.busy || r.tui === 'working' || r.tui === 'compacting' || r.tui === 'queued') return 'busy';
  if ((a && a.asking) || r.waiting) return 'ask';
  const done = doneTs(r, a), cmd = cmdTs(a);
  if (done && done > cmd + 1 && NOW() - done < (G.fresh_s || 1800)) return 'fresh';
  return 'idle';
}
function statusText(r, a, lane) {
  if (lane === 'busy') {
    if (r.tui === 'compacting') return 'compacting memory';
    if (r.tui === 'queued') return 'message queued';
    const t0 = (a && (a.turn_start || a.busy_since)) || 0;
    return 'working' + (t0 ? ' ' + dur(NOW() - t0) : '');
  }
  if (lane === 'ask') return r.waiting || 'waiting on a question menu';
  if (lane === 'fresh') return 'done ' + ago(doneTs(r, a)) + ' ago · your move';
  if (lane === 'sleep') return 'in the hangar · last seen ' + ago(r.la_ts) + ' ago';
  return 'idle ' + ago(r.la_ts);
}
function statusIcon(r, lane) {
  if (lane === 'busy') return r.tui === 'compacting' ? 'sync' : r.tui === 'queued' ? 'hourglass' : 'loader';
  return { ask: 'question', fresh: 'alert', sleep: 'moon', idle: 'clock' }[lane] || 'clock';
}
function statusHtml(r, a, lane) { return ico(statusIcon(r, lane), lane === 'busy' ? 'spin' : '') + '<span>' + esc(statusText(r, a, lane)) + '</span>'; }
function spriteState(lane) { return { busy: 'busy', ask: 'ask', fresh: 'happy', idle: 'idle', sleep: 'sleep' }[lane]; }

// a session's project, or '' when it has none
function projOf(r) { const p = (r && r.project) || ''; return p && p !== 'No project' ? p : ''; }
const PROJ_COLORS = ['#ff8a3d', '#4de1ff', '#ff5fa2', '#9dff57', '#ffd23f', '#b36bff', '#7aa8ff', '#ff6f61', '#5ef0c0', '#f0a8ff'];
function projColor(p) {
  const i = ((G && G.projects) || []).indexOf(p);
  return PROJ_COLORS[(i >= 0 ? i : hash('proj:' + p)) % PROJ_COLORS.length];
}
function makeCard(sid) {
  const el = document.createElement('div');
  el.className = 'agent';
  el.tabIndex = 0;
  el.dataset.sid = sid;
  el.innerHTML = '<div class="pix"><canvas width="16" height="16"></canvas><span class="bubble"></span></div>'
    + '<div class="ag-main"><div class="ag-proj"><i></i><span></span></div><div class="ag-top"><b class="ag-name"></b><span class="ag-lv"></span></div>'
    + '<div class="ag-quest"></div><div class="ag-line"><span class="ag-st"></span>'
    + '<span class="ag-ctx"><i></i></span></div></div><span class="ag-key"></span>'
    + '<span class="ag-acts"><button class="ag-ren" aria-label="Rename" title="Rename (F2, or double-click the name)">' + ico('edit') + '</button>'
    + '<button class="ag-arc" aria-label="Archive">' + ico('archive') + '</button>'
    + '<button class="ag-del" aria-label="Delete" title="Delete for good: stops it and removes it (transcript kept 7 days in ~/.claude/rc-trash)">' + ico('trash') + '</button></span>';
  el.addEventListener('click', ev => {
    const b = ev.target.closest('.ag-acts button');
    if (b) { ev.stopPropagation(); if (b.classList.contains('ag-ren')) nameEdit(sid); else if (b.classList.contains('ag-del')) delSession(sid); else archive(sid); return; }
    if (ev.target.closest('.wake,.ag-edit')) return;
    openAgent(sid);
  });
  el.addEventListener('dblclick', ev => {
    if (!ev.target.closest('.ag-name')) return;
    try { getSelection().removeAllRanges(); } catch (e) {}
    nameEdit(sid);
  });
  el.addEventListener('keydown', ev => {
    if (ev.target !== el) return;
    if (ev.key === 'Enter') openAgent(sid);
    else if (ev.key === 'F2') { ev.preventDefault(); nameEdit(sid); }
  });
  CARD.set(sid, el);
  return el;
}
function fillCard(el, r, a, lane, key) {
  const sid = r.short;
  const lv = a ? a.lv : 1;
  el.className = 'agent st-' + lane + (sid === CUR ? ' sel' : '') + (LANDED[sid] && Date.now() - LANDED[sid] < 4000 ? ' landed' : '')
    + (lane === 'fresh' && doneTs(r, a) > (SEEN[sid] || 0) ? ' new' : '')
    + (el.classList.contains('bump') ? ' bump' : '') + (el.classList.contains('flash') ? ' flash' : '');
  const set = (sel, txt) => { const n = el.querySelector(sel); if (n.textContent !== txt) n.textContent = txt; };
  set('.ag-name', titleOf(r));
  set('.ag-lv', sid === CUR ? 'IN VIEW' : 'LV ' + lv);
  // the owner 10-16: «не видно присвоенного проекта»
  const pj = projOf(r), pe = el.querySelector('.ag-proj');
  pe.hidden = !pj;
  if (pj) { set('.ag-proj span', pj); pe.style.setProperty('--pc', projColor(pj)); }
  set('.ag-quest', r.summary || r.last_a || r.name || '');
  const sth = statusHtml(r, a, lane), stn = el.querySelector('.ag-st');
  if (stn.dataset.h !== sth) { stn.innerHTML = sth; stn.dataset.h = sth; }
  set('.bubble', lane === 'ask' ? '?' : lane === 'fresh' ? '!' : lane === 'sleep' ? 'z' : '');
  set('.ag-key', key ? String(key) : '');
  const arc = el.querySelector('.ag-arc'), at = r.live ? 'Archive: stop it and put it away (UNDO brings it back)' : 'Archive: take it out of the hangar';
  if (arc.title !== at) arc.title = at;
  const pc = r.ctx_max ? Math.round(r.ctx * 100 / r.ctx_max) : 0;
  const cx = el.querySelector('.ag-ctx');
  cx.style.display = r.ctx ? '' : 'none';
  cx.className = 'ag-ctx' + (pc >= 80 ? ' hot' : pc >= 60 ? ' warm' : '');
  cx.firstChild.style.width = pc + '%';
  cx.title = 'context ' + tok(r.ctx) + ' / ' + tok(r.ctx_max) + ' (' + pc + '%)' + (pc >= 80 ? ' · time to /compact' : '');
  el.title = titleOf(r) + ' · ' + modelLabel(r) + ' · ' + (a ? a.turns + ' turns seen by HQ' : '');
  const cv = el.querySelector('canvas');
  const sp = specFor(sid, lv);
  SPRITES.set(cv, { sp, st: spriteState(lane) });
  let wk = el.querySelector('.wake');
  if (lane === 'sleep' && !wk) {
    wk = document.createElement('button');
    wk.className = 'wake'; wk.innerHTML = ico('power') + 'WAKE';
    wk.title = 'Bring this agent back (it gets a live slot)';
    wk.onclick = async ev => {
      ev.stopPropagation(); wk.disabled = true; wk.textContent = '…';
      SFX.play('click');
      const res = await act('up', sid);
      wk.disabled = false; wk.innerHTML = ico('power') + 'WAKE';
      if (res.ok) { toast({ i: 'power', t: 'Waking up', x: titleOf(r), sid }); setTimeout(poll, 1500); }
    };
    el.appendChild(wk);
  } else if (lane !== 'sleep' && wk) wk.remove();
}
function board() {
  // a name being typed holds the board still: a card moved under the box takes the focus away from it
  if (EDIT.sid) return;
  const lanes = $('#lanes');
  const groups = { ask: [], fresh: [], busy: [], idle: [], sleep: [] };
  const st = {};
  G.rows.forEach(r => { const a = agentBy(r.short), k = laneOf(r, a); st[r.short] = k; groups[k].push(r); });
  G.sleeping.forEach(r => { st[r.short] = 'sleep'; groups.sleep.push(r); });
  const A = r => agentBy(r.short);
  groups.ask.sort((x, y) => doneTs(y, A(y)) - doneTs(x, A(x)));
  groups.fresh.sort((x, y) => doneTs(y, A(y)) - doneTs(x, A(x)));
  groups.busy.sort((x, y) => (((A(x) || {}).turn_start || (A(x) || {}).busy_since) || 1e12) - (((A(y) || {}).turn_start || (A(y) || {}).busy_since) || 1e12));
  groups.idle.sort((x, y) => (y.la_ts || 0) - (x.la_ts || 0));
  STATE = st;
  ORDER = [].concat(groups.ask, groups.fresh, groups.busy, groups.idle).map(r => r.short);
  const used = new Set();
  let key = 0;
  LANES.forEach(([k, icon, name]) => {
    let sec = lanes.querySelector('.lane[data-k="' + k + '"]');
    if (!sec) {
      sec = document.createElement('section');
      sec.className = 'lane ' + k; sec.dataset.k = k;
      sec.innerHTML = '<h2><i class="led"></i>' + ico(icon) + '<span class="nm">' + name + '</span><span class="n"></span></h2><div class="cards"></div>';
      if (k === 'sleep') {
        sec.querySelector('h2').onclick = () => { SLEEP_OPEN = !SLEEP_OPEN; LS.set('sleepOpen', SLEEP_OPEN); sec.classList.toggle('open', SLEEP_OPEN); SFX.play(SLEEP_OPEN ? 'open' : 'close'); };
      }
      lanes.appendChild(sec);
    }
    const list = groups[k];
    sec.hidden = !list.length;
    if (k === 'sleep') { sec.classList.toggle('open', SLEEP_OPEN); sec.querySelector('.n').textContent = G.sleeping_n; }
    else sec.querySelector('.n').textContent = list.length;
    const box = sec.querySelector('.cards');
    list.forEach((r, i) => {
      const el = CARD.get(r.short) || makeCard(r.short);
      used.add(r.short);
      const kk = (k !== 'sleep' && key < 9) ? ++key : 0;
      fillCard(el, r, agentBy(r.short), k, kk);
      if (box.children[i] !== el) box.insertBefore(el, box.children[i] || null);
    });
  });
  CARD.forEach((el, sid) => { if (!used.has(sid) && !el.classList.contains('gone')) { el.remove(); CARD.delete(sid); } });
  radarDraw(groups);
  const n = G.rows.length;
  $('#boardfoot').innerHTML = (n ? n + ' live · ' : 'No live agents. Recruit one or wake one from the barracks. · ')
    + (G.app_n ? G.app_n + ' app chats · ' : '') + '<a href="/" title="The archive lives in the classic panel (Retired): bring any of them back from there">' + ico('archive') + ' ' + G.retired_n + ' archived</a>';
  // attention: title badge + dock count
  const need = ORDER.filter(s => st[s] === 'ask' || (st[s] === 'fresh' && doneTs(rowBy(s), agentBy(s)) > (SEEN[s] || 0))).length;
  document.title = (need ? '(' + need + ') ' : '') + 'HQ';
  const dn = $('#dockn'); dn.textContent = need; dn.classList.toggle('on', need > 0);
}

// ------------------------------------------------------------------ rename and archive, right on a card
// the owner 10-19: «изменять названия сессий слева в табах и мочь закинуть сессии в
// архив, по кнопке слева в табах». ✎ (or a double click on the name, or F2)
// edits the name in place: Enter or a click away saves, Esc cancels, an empty
// name gives the automatic one back. The archive button stops a live session
// and retires it (the guardian no longer brings it back), a sleeping one only
// leaves the hangar; UNDO in the toast puts it back.
const EDIT = { sid: null, inp: null, was: '' };   // the card whose name is being typed
const NAMED = {};    // sid -> {v, at}: a new name shows at once, a poll sent before the save does not undo it
const GONE = {};     // sid -> archived at (ms): the card stays away until the server agrees
const ARCHIVED = {}; // sid -> ms: the server's own «mission complete» toast is not repeated for these
function held(j) {
  const now = Date.now();
  const all = (j.rows || []).concat(j.sleeping || []);
  Object.keys(NAMED).forEach(s => {
    const r = all.find(x => x.short === s);
    if (!r || r.title === NAMED[s].v || now - NAMED[s].at > 20000) delete NAMED[s];
    else r.title = NAMED[s].v;
  });
  Object.keys(GONE).forEach(s => {
    // gone from the server's lists = done; still there after 30 s = it did not work, show the truth
    if (!all.some(x => x.short === s) || now - GONE[s] > 30000) delete GONE[s];
  });
  if (!Object.keys(GONE).length) return;
  const keep = r => !GONE[r.short];
  const nl = (j.rows || []).length, ns = (j.sleeping || []).length;
  if (j.rows) j.rows = j.rows.filter(keep);
  if (j.sleeping) j.sleeping = j.sleeping.filter(keep);
  const hid = nl - (j.rows || []).length, hs = ns - (j.sleeping || []).length;
  j.sleeping_n = Math.max(0, (j.sleeping_n || 0) - hs);
  j.retired_n = (j.retired_n || 0) + hid + hs;
}
function nameEdit(sid) {
  const el = CARD.get(sid), r = rowBy(sid);
  if (!el || !r || EDIT.sid === sid) return;
  if (EDIT.sid) nameDone(true);
  const inp = document.createElement('input');
  inp.className = 'ag-edit';
  inp.maxLength = 40;
  inp.spellcheck = false;
  inp.value = titleOf(r);
  inp.placeholder = 'empty = automatic name';
  inp.setAttribute('aria-label', 'Session name');
  inp.addEventListener('keydown', e => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); nameDone(true); }
    else if (e.key === 'Escape') { e.preventDefault(); nameDone(false); }
  });
  inp.addEventListener('blur', () => nameDone(true));
  inp.addEventListener('click', e => e.stopPropagation());
  inp.addEventListener('dblclick', e => e.stopPropagation());
  EDIT.sid = sid; EDIT.inp = inp; EDIT.was = titleOf(r);
  el.classList.add('editing');
  el.querySelector('.ag-name').after(inp);
  inp.focus({ preventScroll: true });
  inp.select();
  SFX.play('click');
}
function nameDone(save) {
  const sid = EDIT.sid, inp = EDIT.inp, was = EDIT.was;
  if (!sid) return;
  EDIT.sid = null; EDIT.inp = null;
  const v = inp.value.split(/\s+/).filter(Boolean).join(' ').slice(0, 40);
  const el = CARD.get(sid);
  const back = el && el.contains(document.activeElement);
  if (el) el.classList.remove('editing');
  inp.remove();
  if (back) el.focus({ preventScroll: true });
  if (save && v !== was) nameSave(sid, v);
  else if (G) board();
}
async function nameSave(sid, v) {
  const r = rowBy(sid), old = r ? r.title : '';
  if (r && v) { r.title = v; NAMED[sid] = { v, at: Date.now() }; }
  if (G) { board(); stageBar(); }
  const j = await act('set_title', { ids: [(r && r.uuid) || sid], value: v });
  if (!j.ok) {
    delete NAMED[sid];
    if (r) r.title = old;
    if (G) { board(); stageBar(); }
    return;
  }
  SFX.play('ack');
  toast({ i: 'edit', t: v ? 'Renamed' : 'Automatic name back', x: v || '', ms: 3500 });
  setTimeout(poll, 300);
}
function stageClose() {
  CUR = null;
  $('#stage').classList.remove('has');
  $('#chat').src = 'about:blank';
  IFRAME_READY = false;
  goHQ();
  stageBar();
}
function archive(sid) {
  const r = rowBy(sid);
  if (!r || GONE[sid]) return;
  const live = !!r.live, lane = STATE[sid], name = titleOf(r);
  const go = async () => {
    const el = CARD.get(sid);
    GONE[sid] = ARCHIVED[sid] = Date.now();
    SFX.play('close');
    if (el) {
      const c = rectCenter(el);
      FX.burst(c[0], c[1], 24, ['#8a83b9', '#c9c2ee', '#ffffff'], 5);
      el.classList.add('gone');
      setTimeout(() => { if (CARD.get(sid) === el && el.classList.contains('gone')) { el.remove(); CARD.delete(sid); } }, 650);
    }
    if (CUR === sid) stageClose();
    const j = await act(live ? 'kill' : 'retire', sid);
    if (!j.ok) {
      delete GONE[sid]; delete ARCHIVED[sid];
      const e2 = CARD.get(sid);
      if (e2) e2.classList.remove('gone');
      if (G) board();
      return;
    }
    toast({ i: 'archive', t: 'Archived', x: name, ms: 10000, btn: ['UNDO', () => unarchive(sid, live, name)] });
    setTimeout(poll, 300);
  };
  if (lane === 'busy' || lane === 'ask')
    confirmBox('«' + name + '» is ' + (lane === 'busy' ? 'working right now' : 'asking you something')
      + '. Archive it anyway? It stops mid-step; UNDO or the classic panel brings it back.', 'ARCHIVE', ok => { if (ok) go(); });
  else go();
}
// the owner 16.11: «не вижу фичи удаления сессии» — Delete lived only in the classic
// panel. Same server action (C.act_delete): stops it if running, drops the row,
// the transcript goes to ~/.claude/rc-trash for 7 days. No UNDO, hence the confirm.
function delSession(sid) {
  if (!sid || sid === 'new' || GONE[sid]) return;
  const r = rowBy(sid), sr = HQS.by[sid];
  const name = r ? titleOf(r) : (sr && sr.title) || sid;
  const run = !!(r && r.live);
  confirmBox('Delete «' + name + '» for good?' + (run ? ' It is running and will be stopped.' : '')
    + ' It leaves HQ and the panel and does not come back; the transcript sits in ~/.claude/rc-trash for 7 days, then it is purged.', 'DELETE', async ok => {
    if (!ok) return;
    const el = CARD.get(sid);
    GONE[sid] = ARCHIVED[sid] = Date.now();
    if (el) {
      const c = rectCenter(el);
      FX.burst(c[0], c[1], 24, ['#ff4d6d', '#ff9aae', '#ffffff'], 5);
      el.classList.add('gone');
      setTimeout(() => { if (CARD.get(sid) === el && el.classList.contains('gone')) { el.remove(); CARD.delete(sid); } }, 650);
    }
    if (CUR === sid) stageClose();
    const j = await act('delete', sid);
    if (!j.ok) {
      delete GONE[sid]; delete ARCHIVED[sid];
      const e2 = CARD.get(sid);
      if (e2) e2.classList.remove('gone');
      if (G) board();
      return;
    }
    SFX.play('close');
    toast({ i: 'trash', t: 'Deleted', x: name, ms: 6000 });
    if (HQS.q) { delete HQS.by[sid]; hqsRun(true); }
    setTimeout(poll, 300);
  });
}

// ------------------------------------------------------------------ search every session
// the owner 16.11: «поиск по всем сессиям … я только и работаю в HQ». The same
// /api/search the classic panel uses (rc_search.py: titles, summaries and the
// text of every conversation, archived ones too). While a query is typed the
// lanes step aside and the hits take the board; a hit opens in the stage.
const HQS = { q: '', seq: 0, t: null, by: {}, list: [] };
function hqsWhen(ts) {
  if (!ts) return '';
  if (NOW() - ts < 2 * 86400) return ago(ts) + ' ago';
  const d = new Date(ts * 1000);
  return String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0');
}
function hqsMark(text, terms) {
  // escape first, then wrap each term; terms are lowercase, matching is case-insensitive
  const src = String(text || ''), low = src.toLowerCase();
  const marks = [];
  (terms || []).forEach(t => {
    if (!t) return;
    let i = 0;
    while ((i = low.indexOf(t, i)) >= 0) { marks.push([i, i + t.length]); i += t.length; }
  });
  marks.sort((a, b) => a[0] - b[0]);
  let out = '', at = 0;
  marks.forEach(m => {
    if (m[0] < at) return;
    out += esc(src.slice(at, m[0])) + '<mark>' + esc(src.slice(m[0], m[1])) + '</mark>';
    at = m[1];
  });
  return out + esc(src.slice(at));
}
const HQS_ST = { live: ['LIVE', 'live'], ret: ['ARCHIVED', 'ret'], queue: ['QUEUED', 'queue'], idle: ['ASLEEP', 'idle'] };
function hqsShow(on) {
  $('#board').classList.toggle('searching', on);
  $('#hqres').hidden = !on;
  if (!on) { $('#hqres').innerHTML = ''; $('#hqqn').textContent = ''; HQS.list = []; }
}
function hqsRender(j) {
  const box = $('#hqres');
  const res = j.results || [];
  HQS.list = res;
  res.forEach(x => { HQS.by[x.short] = x; });
  $('#hqqn').textContent = j.n > res.length ? res.length + ' of ' + j.n : String(res.length);
  if (!res.length) { box.innerHTML = '<div class="hqs-none">Nothing found for «' + esc(j.q) + '». All words must occur in one session; put a phrase in "quotes".</div>'; return; }
  box.innerHTML = res.map((x, i) => {
    const st = HQS_ST[x.cls] || HQS_ST.idle;
    const hits = (x.hits || []).map(h => '<div class="hqs-hit"><b>' + (h.role === 'user' ? 'YOU' : 'AGENT') + '</b>' + hqsMark(h.snip, j.terms) + '</div>').join('');
    const more = x.n > (x.hits || []).length ? '<div class="hqs-more">+' + (x.n - x.hits.length) + ' more messages</div>' : '';
    return '<div class="hqs-r st-' + st[1] + (x.short === CUR ? ' sel' : '') + '" tabindex="0" data-sid="' + esc(x.short) + '" data-i="' + i + '">'
      + '<div class="hqs-top"><span class="hqs-st">' + st[0] + (x.busy ? ' · WORKING' : '') + '</span><b class="hqs-t">' + hqsMark(x.title, j.terms) + '</b>'
      + '<span class="hqs-w">' + esc(hqsWhen(x.la_ts)) + '</span>'
      + '<button class="hqs-del" title="Delete for good" aria-label="Delete">' + ico('trash') + '</button></div>'
      + (x.project && x.project !== 'No project' ? '<div class="hqs-p">' + esc(x.project) + '</div>' : '')
      + (x.sub ? '<div class="hqs-sub">' + hqsMark(x.sub, j.terms) + '</div>' : '')
      + hits + more + '</div>';
  }).join('');
}
async function hqsRun(force) {
  const q = $('#hqq').value.trim();
  if (!q) { HQS.q = ''; hqsShow(false); return; }
  if (q === HQS.q && !force) return;
  HQS.q = q;
  const my = ++HQS.seq;
  hqsShow(true);
  $('#hqqn').textContent = '…';
  try {
    const r = await fetchT('/api/search?q=' + encodeURIComponent(q) + devq('&'), { cache: 'no-store' }, 30000);
    const j = await r.json();
    if (my !== HQS.seq) return;
    if (!j.ok) { $('#hqres').innerHTML = '<div class="hqs-none">Search failed: ' + esc(j.msg || '') + '</div>'; $('#hqqn').textContent = ''; return; }
    hqsRender(j);
  } catch (e) {
    if (my !== HQS.seq) return;
    $('#hqres').innerHTML = '<div class="hqs-none">No connection: ' + esc(String(e)) + '</div>'; $('#hqqn').textContent = '';
  }
}
function hqsOpen(sid) {
  if (!sid) return;
  SFX.play('click');
  openAgent(sid);
  $('#hqres').querySelectorAll('.hqs-r').forEach(el => el.classList.toggle('sel', el.dataset.sid === sid));
}
function hqsFocus() {
  flAway();
  if (innerWidth <= 860) document.body.classList.remove('staged', 'side');
  const q = $('#hqq'); q.focus(); q.select();
}
$('#hqq').addEventListener('input', () => { clearTimeout(HQS.t); HQS.t = setTimeout(hqsRun, 220); });
$('#hqq').addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    e.preventDefault(); e.stopPropagation();
    if (e.target.value) { e.target.value = ''; clearTimeout(HQS.t); hqsRun(); } else e.target.blur();
  } else if (e.key === 'Enter') {
    e.preventDefault(); clearTimeout(HQS.t);
    if (HQS.q !== e.target.value.trim()) hqsRun().then(() => { if (HQS.list[0]) hqsOpen(HQS.list[0].short); });
    else if (HQS.list[0]) hqsOpen(HQS.list[0].short);
  } else if (e.key === 'ArrowDown') {
    const f = $('#hqres .hqs-r'); if (f) { e.preventDefault(); f.focus(); }
  }
});
$('#hqres').addEventListener('click', e => {
  const r = e.target.closest('.hqs-r'); if (!r) return;
  if (e.target.closest('.hqs-del')) { e.stopPropagation(); delSession(r.dataset.sid); return; }
  hqsOpen(r.dataset.sid);
});
$('#hqres').addEventListener('keydown', e => {
  const r = e.target.closest('.hqs-r'); if (!r) return;
  if (e.key === 'Enter') { e.preventDefault(); hqsOpen(r.dataset.sid); }
  else if (e.key === 'ArrowDown' && r.nextElementSibling) { e.preventDefault(); e.stopPropagation(); r.nextElementSibling.focus(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); (r.previousElementSibling || $('#hqq')).focus(); }
});

async function unarchive(sid, live, name) {
  delete GONE[sid];
  SFX.play('open');
  toast({ i: 'power', t: live ? 'Bringing it back' : 'Back in the hangar', x: name, ms: 4000 });
  // quiet: an undo is not a revival worth XP
  const j = await act(live ? 'up' : 'unretire', sid, { quiet: true });
  if (j.ok) setTimeout(poll, 300);
}

function trophiesDraw(j) {
  const got = j.ach.filter(a => a.ts).length;
  $('#achn').textContent = got + '/' + j.ach.length;
  $('#ach').innerHTML = j.ach.map(a => '<div class="tro' + (a.ts ? ' got' : '') + '" title="' + esc(a.t + ' — ' + a.d
    + (a.ts ? ' · unlocked ' + new Date(a.ts * 1000).toLocaleDateString() : ' · locked')) + '">' + ico(a.ts ? (ACH_ICON[a.k] || 'trophy') : 'lock') + '</div>').join('');
}
function logAdd(evs, first) {
  const box = $('#log');
  if (first) box.innerHTML = '';
  evs.forEach(e => {
    if (e.k === 'cmd' && !e.x) return;          // plain sends would flood the log
    const d = document.createElement('div');
    d.className = 'ev' + (first ? '' : ' new');
    if (e.sid) d.dataset.sid = e.sid;
    d.innerHTML = '<span class="ei">' + icoOf(e.i || 'bot') + '</span><span class="et"><b>' + esc(e.t || e.k) + '</b>'
      + (e.x ? '<small>' + esc(e.x) + '</small>' : '') + '</span><span class="ex">' + (e.xp ? '+' + e.xp : '')
      + '<time>' + hhmm(e.ts) + '</time></span>';
    if (e.sid) d.onclick = () => openAgent(e.sid);
    box.insertBefore(d, box.firstChild);
  });
  while (box.children.length > 60) box.lastChild.remove();
}

// ------------------------------------------------------------------ events → juice
function cardOf(sid) { return sid ? CARD.get(sid.slice(0, 8)) : null; }
function pulseCard(sid, cls) {
  const el = cardOf(sid); if (!el) return;
  el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 1100);
}
function react(e) {
  const sid = e.sid || null;
  if (e.xp) floatXP('+' + e.xp + ' XP');
  if (e.xp) { const b = $('.xpbar'); b.classList.remove('gain'); void b.offsetWidth; b.classList.add('gain'); }
  switch (e.k) {
    case 'turn':
      SFX.play('done'); pulseCard(sid, 'flash');
      hype('turn', 'mid', (sid && sid === CUR) ? $('#stagepix') : (sid && CARD.get(sid)));
      toast({ i: e.i || 'check', t: e.t + ' finished', x: e.x, xp: e.xp, cls: 'turn', sid });
      notify(e.t + ' finished', e.x, sid);
      break;
    case 'cmd':
      SFX.play('xp');
      if (e.i === '⚡') { toast({ i: 'zap', t: 'Quick draw!', x: 'Replied within 2 min of a finish', xp: e.xp, sid }); hype('quick', 'mid', $('#stagepix')); }
      break;
    case 'loot':
      if (/deploy|publish/.test(e.x)) { SFX.play('deploy'); FX.rocket(); hype('deploy', 'big'); }
      else { SFX.play('loot'); hype('loot', 'mid', (sid && CARD.get(sid)) || $('#stagepix')); }
      toast({ i: e.i, t: e.t + ' ' + e.x, xp: e.xp, cls: 'loot', sid });
      pulseCard(sid, 'bump');
      break;
    case 'ach':
      SFX.play('ach'); FX.confetti(140); hype('ach', 'mid', $('#trobtn'));
      toast({ i: 'trophy', t: 'Trophy: ' + e.t, x: e.x, xp: e.xp, cls: 'ach', ms: 9000 });
      break;
    case 'quest':
      SFX.play('quest'); FX.confetti(70);
      toast({ i: e.i || 'article', t: e.t, x: e.x, xp: e.xp, cls: 'ach' });
      break;
    case 'won':
      SFX.play('level'); FX.confetti(260);
      hype('won', 'big', null, HYPE_MODE === 'clean' ? "LET'S GOOO" : "LET'S FUCKING GOOO");
      setTimeout(() => banner('DAY WON!', 'every must-do of the day is closed'), 1900);
      toast({ i: 'trophy', t: 'Day won', x: e.x, xp: e.xp, cls: 'ach', ms: 9000 });
      break;
    case 'new':
      SFX.play('quest');
      toast({ i: 'user-plus', t: e.t, x: e.x, xp: e.xp, sid });
      break;
    case 'up':
      toast({ i: 'power', t: e.t, xp: e.xp, sid });
      break;
    case 'retire':
      // archived from a card: its own toast with UNDO is already up
      if (sid && Date.now() - (ARCHIVED[String(sid).slice(0, 8)] || 0) < 120000) break;
      SFX.play('quest');
      toast({ i: 'flag', t: e.t, x: e.x, xp: e.xp });
      break;
    case 'hello':
      toast({ i: 'radio-tower', t: e.t, x: e.x });
      break;
  }
}
function levelUp(j, rankChanged) {
  SFX.play('level'); FX.confetti(220);
  banner('LEVEL UP!', 'LV ' + j.lv + (rankChanged ? ' · you are now a ' + j.rank : ' · ' + j.rank));
  setTimeout(() => hype('level', 'mid', $('#cmdr')), 900);
  const b = $('#banner');
  const cv = document.createElement('canvas'); cv.width = 16; cv.height = 16; cv.className = 'bpix';
  b.insertBefore(cv, b.firstChild);
  SPRITES.set(cv, { cmdr: true });
}
function notify(title, body, sid) {
  if (!NTF || !document.hidden || !('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    const n = new Notification(title, { body: body || '', tag: 'hq-' + (sid || 'x') });
    n.onclick = () => { window.focus(); if (sid) openAgent(sid); n.close(); };
  } catch (e) {}
}

// ------------------------------------------------------------------ stage
function chatUrl(hash) { return '/?embed=1&s=' + encodeURIComponent(hash) + devq('&') + '&t=' + Date.now().toString(36) + '#' + hash; }
// first orders of launches, shown in the chat until its transcript has them
const FIRSTORDER = {};
function firstShow(sid) {
  const t = FIRSTORDER[sid];
  if (!t) return;
  // TX_CUR is a `let` of the chat page, not a window property: hqFirst keys the bubble by sid itself
  try { const cw = $('#chat').contentWindow; if (cw.hqFirst) { cw.hqFirst(sid, t); delete FIRSTORDER[sid]; } } catch (e) {}
}
function openAgent(sid) {
  if (!sid) return;
  sid = sid.slice(0, 8);
  flAway();
  const changed = sid !== CUR;
  CUR = sid;
  SEEN[sid] = NOW(); LS.set('seen', SEEN);
  document.body.classList.add('staged');
  $('#stage').classList.add('has');
  if (changed) SFX.play('open');
  CARD.forEach((el, s) => el.classList.toggle('sel', s === sid));
  const el = CARD.get(sid);
  if (el) { el.classList.remove('new'); el.scrollIntoView({ block: 'nearest' }); }
  stageBar();
  const fr = $('#chat');
  let done = false;
  try { if (changed && fr.contentWindow.hqMore) fr.contentWindow.hqMore(false); } catch (e) {}
  bayShow(false);
  try {
    const cw = fr.contentWindow;
    if (IFRAME_READY && cw && cw.rcHas && cw.rcHas(sid)) { if (changed || !String(cw.location.hash).endsWith(sid)) cw.openTx(sid); done = true; }
    else if (IFRAME_READY && cw && cw.rcAdopt && cw.openTx) { cw.openTx(sid); cw.rcAdopt(sid); done = true; }
  } catch (e) {}
  if (!done) { IFRAME_READY = false; fr.src = chatUrl(sid); }
  else firstShow(sid);
  if (G) board();
}
function openRecruit() {
  flAway();
  CUR = 'new';
  document.body.classList.add('staged');
  $('#stage').classList.add('has');
  CARD.forEach(el => el.classList.remove('sel'));
  bayShow(true);
  SFX.play('open-big');
  stageBar();
  setTimeout(() => { const t = $('#bay-text'); if (t && !$('#bay-form').hidden) t.focus(); }, 80);
}
function goHQ() {
  if (FLEET.ret && !FLEET.on) {
    FLEET.ret = false;
    document.body.classList.remove('staged', 'side');
    showFleet(true);
    return;
  }
  if (document.body.classList.contains('staged') && innerWidth <= 860) SFX.play('close');
  document.body.classList.remove('staged', 'side');
  const el = CUR && CARD.get(CUR);
  if (el && innerWidth > 860) el.focus({ preventScroll: true });
}
function nextAgent(dir) {
  const need = ORDER.filter(s => STATE[s] === 'ask' || STATE[s] === 'fresh');
  const list = need.length ? need : ORDER;
  if (!list.length) { toast({ i: 'moon-stars', t: 'Nobody is waiting', x: 'Recruit a new agent or wake one from the barracks.' }); return; }
  let i = list.indexOf(CUR);
  i = i < 0 ? (dir > 0 ? 0 : list.length - 1) : (i + (dir || 1) + list.length) % list.length;
  openAgent(list[i]);
}
function stageBar() {
  const name = $('#st-name'), sub = $('#st-sub');
  const stop = $('#st-stop'), doneB = $('#st-done'), cmp = $('#st-compact');
  const cv = $('#stagepix');
  cmp.hidden = true;
  $('#st-proj').hidden = true;
  $('#st-del').hidden = true;
  if (CUR === 'new') {
    const L = BAY_SEQ && seqL();
    const cont = L ? L.mode === 'continue' : bayModeOf() === 'continue';
    name.textContent = !L ? 'Launch bay' : L.state === 'online' ? (L.picked ? 'Order delivered' : 'Agent online') : L.state === 'failed' ? 'Launch failed' : cont ? 'Finding the session…' : 'Launching…';
    sub.textContent = L ? (L.picked ? '→ ' + (L.picked.title || L.sid) + ' · ' : '') + (L.title || '') : cont ? 'The order goes to the session already doing this work' : 'A new agent: give it the first order';
    stop.hidden = true; doneB.hidden = true;
    // the same bot as in the hangar, not a new random one on every poll
    const key = L ? (L.state === 'online' && L.sid ? L.sid : L.nonce) : 'recruit-' + Math.floor(Date.now() / 60000);
    if (cv.dataset.key !== key) { cv.dataset.key = key; SPRITES.set(cv, { sp: specFor(key, 1), st: 'happy' }); }
    return;
  }
  cv.dataset.key = '';
  if (!CUR) { name.textContent = 'Pick an agent'; sub.textContent = ''; stop.hidden = true; doneB.hidden = true; return; }
  const r = rowBy(CUR), a = agentBy(CUR);
  $('#st-del').hidden = false;
  if (!r) {
    // opened from search: an archived or queued session is not on the board
    const sr = HQS.by[CUR];
    name.textContent = (sr && sr.title) || CUR;
    sub.textContent = sr ? ((HQS_ST[sr.cls] || HQS_ST.idle)[0] + ' · ' + hqsWhen(sr.la_ts)) : '';
    stop.hidden = true; doneB.hidden = true;
    return;
  }
  const lane = STATE[CUR] || laneOf(r, a);
  name.textContent = titleOf(r);
  // one short line: what it is doing, how full its memory is, its level.
  // Model, effort, pid and counters live behind ⋯ (the owner 10-16: «перегружена»).
  const pc = r.ctx_max ? Math.round(r.ctx * 100 / r.ctx_max) : 0;
  // 05.11 the owner «не вижу величины текущей сессии»: the context size in tokens, not only a bare percent
  const cl = pc >= 80 ? ' hot' : pc >= 50 ? ' warm' : '';
  const cx = r.ctx ? '<span class="sep">·</span><span class="ctxv' + cl + '" title="context ' + tok(r.ctx) + ' of ' + tok(r.ctx_max) + ' tokens">CTX <i><u style="width:' + Math.min(100, pc) + '%"></u></i>' + tok(r.ctx) + '/' + tok(r.ctx_max).replace('.0M', 'M') + '</span>' : '';
  // the size itself sits in the COMPACT button (the line is too narrow for both); the line keeps the status
  void cx;
  sub.innerHTML = statusHtml(r, a, lane) + '<span class="sep">·</span><span>LV ' + (a ? a.lv : 1) + '</span>';
  cmp.hidden = !r.ctx || COMPACTING[CUR] > NOW() - 240; cmp.className = 'hb stc' + cl;
  cmp.querySelector('.cn').textContent = r.ctx ? tok(r.ctx) : '';
  cmp.title = 'Compact: context ' + (r.ctx ? tok(r.ctx) + ' of ' + tok(r.ctx_max) + ' (' + pc + '%)' : '?') + ', squeeze it into a short summary (/compact). Every next message gets cheaper.';
  sub.title = modelLabel(r) + (r.ctx ? ' · context ' + tok(r.ctx) + ' of ' + tok(r.ctx_max) : '');
  const pj = projOf(r), pc2 = $('#st-proj');
  pc2.hidden = false;
  pc2.textContent = pj || '+ PROJECT';
  pc2.classList.toggle('none', !pj);
  pc2.style.setProperty('--pc', pj ? projColor(pj) : '#8f88bf');
  stop.hidden = lane !== 'busy';
  doneB.hidden = !r.live;
  SPRITES.set(cv, { sp: specFor(CUR, a ? a.lv : 1), st: spriteState(lane) });
  if (lane === 'ask' && !r.waiting && !ASK_HINTED[CUR]) {
    ASK_HINTED[CUR] = 1;
    toast({ i: 'question', t: 'A question menu is open in the terminal', x: 'It is not in the chat yet. Press the stop button to dismiss it, then type your answer.', ms: 12000 });
    stop.hidden = false;
  }
}
const ASK_HINTED = {};

// ------------------------------------------------------------------ project picker
function projPick(on) {
  const box = $('#projpick');
  if (!on) { box.hidden = true; return; }
  const r = rowBy(CUR);
  if (!r) return;
  const cur = projOf(r);
  const names = ((G && G.projects) || []).slice();
  if (cur && names.indexOf(cur) < 0) names.unshift(cur);
  $('#pp-list').innerHTML = names.map(n => '<button data-proj="' + esc(n) + '" class="' + (n === cur ? 'on' : '') + '" style="--pc:' + projColor(n) + '"><i></i>' + esc(n) + '</button>').join('')
    + '<button data-proj="" class="' + (cur ? '' : 'on') + ' none"><i></i>No project</button>';
  const b = $('#st-proj').getBoundingClientRect();
  box.style.left = Math.max(8, Math.min(innerWidth - 268, b.left)) + 'px';
  box.style.top = (b.bottom + 6) + 'px';
  box.hidden = false;
  SFX.play('open');
}
async function projSet(value) {
  const r = rowBy(CUR);
  projPick(false);
  if (!r) return;
  const j = await act('set_project', { ids: [r.uuid || r.short], value: value || 'No project' });
  if (j.ok) { r.project = value || 'No project'; stageBar(); if (G) board(); toast({ i: 'briefcase', t: value ? 'Moved to ' + value : 'Project cleared', x: titleOf(r), ms: 4000 }); setTimeout(poll, 500); SFX.play('click'); }
}
$('#st-proj').onclick = e => { e.stopPropagation(); projPick($('#projpick').hidden); };
$('#projpick').addEventListener('click', e => {
  e.stopPropagation();
  const b = e.target.closest('[data-proj]');
  if (b) projSet(b.dataset.proj);
});
$('#pp-new').onclick = e => {
  e.stopPropagation();
  const n = (prompt('Name of the new project') || '').trim().slice(0, 60);
  if (n) projSet(n);
};
document.addEventListener('click', () => { if (!$('#projpick').hidden) projPick(false); });

// ------------------------------------------------------------------ iframe messages
window.addEventListener('message', e => {
  if (e.origin !== location.origin) return;
  const d = e.data || {};
  if (!d.rc) return;
  if (d.rc === 'ready') { IFRAME_READY = true; if (d.sid && d.sid !== CUR && CUR !== 'new') { CUR = d.sid; stageBar(); } }
  else if (d.rc === 'open') { IFRAME_READY = true; if (d.sid && d.sid !== CUR && CUR !== 'new') { CUR = d.sid; CARD.forEach((el, s) => el.classList.toggle('sel', s === d.sid)); stageBar(); if (G) board(); } }
  else if (d.rc === 'sending') sendFx(d.sid);
  else if (d.rc === 'sent') { ackFx(d.sid); setTimeout(poll, 500); }
  else if (d.rc === 'fail') { SFX.play('sad'); pulseCard(d.sid, 'bump'); }
  else if (d.rc === 'rec') SFX.play(d.on ? 'comm-open' : 'comm-close');
  else if (d.rc === 'voice') SFX.play(d.ok ? 'heard' : 'sad');
  else if (d.rc === 'net') { const cm = $('#comm'); cm.className = 'comm ' + (d.state || 'ok'); cm.title = d.state === 'ok' ? 'link to the computer: good' : d.state === 'weak' ? 'link to the computer: weak, sends are confirmed and retried' : 'link to the computer: down, messages wait in the outbox'; }
  else if (d.rc === 'flash') { if (String(d.t || '').charAt(0) === '✗') SFX.play('error'); }
  else if (d.rc === 'esc') goHQ();
  else if (d.rc === 'nav') nextAgent(d.dir || 1);
  else if (d.rc === 'created') { if (d.sid) { CUR = d.sid; SEEN[d.sid] = NOW(); } setTimeout(poll, 1500); }
  else if (d.rc === 'drag') {
    // a file is dragged over the chat: the drop targets are ours to draw
    const r = $('#chat').getBoundingClientRect();
    dropOpen(); dropKeep(); dropMove((d.x || 0) + r.left, (d.y || 0) + r.top);
  }
  else if (d.rc === 'upload') trayUpdate(d);
  else if (d.rc === 'more') $('#st-more').classList.toggle('on', !!d.on);
  if (d.rc === 'open' || d.rc === 'ready') { setTimeout(dropFlush, 60); try { hookKeys($('#chat').contentDocument); } catch (e) {} if (d.sid) setTimeout(() => firstShow(d.sid), 30); }
});

// ------------------------------------------------------------------ controls
function toggleSide(force) {
  const was = document.body.classList.contains('side');
  const on = force == null ? !was : force;
  document.body.classList.toggle('side', on);
  if (on !== was) SFX.play(on ? 'open' : 'close');
}
function showTro(on) { $('#tro').hidden = !on; if (on) $('#trook').focus(); SFX.play(on ? 'open' : 'close'); }
function toggleSound() {
  SND = !SND; LS.set('snd', SND);
  $('#snd').innerHTML = ico(SND ? 'volume-3' : 'volume-x');
  $('#snd').classList.toggle('off', !SND);
  if (SND) SFX.play('click');
}
async function toggleNotify() {
  if (!('Notification' in window)) { toast({ i: 'notification-off', t: 'No notifications here', x: 'On iPhone they need HQ added to the Home Screen.' }); return; }
  if (Notification.permission !== 'granted') {
    const p = await Notification.requestPermission();
    NTF = p === 'granted';
  } else NTF = !NTF;
  LS.set('ntf', NTF);
  ntfBtn();
  if (NTF) toast({ i: 'notification', t: 'Notifications on', x: 'You get a ping when an agent finishes while HQ is in the background.' });
}
function ntfBtn() {
  const on = NTF && 'Notification' in window && Notification.permission === 'granted';
  $('#ntf').innerHTML = ico(on ? 'notification' : 'notification-off');
  $('#ntf').classList.toggle('off', !on);
}
function focusComposer() {
  try { const fr = $('#chat'); fr.contentWindow.focus(); fr.contentWindow.document.getElementById('drtext').focus(); } catch (e) {}
}
function showHelp() { $('#help').hidden = false; $('#helpok').focus(); SFX.play('open'); }
function hideHelp() { $('#help').hidden = true; LS.set('helped', true); SFX.play('close'); }

document.addEventListener('keydown', e => {
  if (CINE.on) { e.preventDefault(); CINE.skip(); return; }
  if (!$('#projpick').hidden && e.key === 'Escape') { e.preventDefault(); projPick(false); return; }
  if (!$('#help').hidden) { if (e.key === 'Escape' || e.key === '?' || e.key === 'Enter') { e.preventDefault(); hideHelp(); } return; }
  if (!$('#modal').hidden) { if (e.key === 'Escape') modalClose(false); else if (e.key === 'Enter') { e.preventDefault(); modalClose(true); } return; }
  if (!$('#tro').hidden) { if (e.key === 'Escape' || e.key === 't' || e.key === 'T') { e.preventDefault(); showTro(false); } return; }
  if (!$('#prof').hidden) { if (e.key === 'Escape' || e.key === 'p' || e.key === 'P') { e.preventDefault(); showProf(false); } return; }
  if (!$('#sett').hidden) { if (e.key === 'Escape') { e.preventDefault(); showSett(false); } return; }
  if (!$('#acc').hidden) { if (e.key === 'Escape') { e.preventDefault(); showAcc(false); } return; }
  if (!$('#devm').hidden) { if (e.key === 'Escape') { e.preventDefault(); showDev(false); } return; }
  if (!$('#sys').hidden) { if (e.key === 'Escape' || e.key === 'v' || e.key === 'V') { e.preventDefault(); showSys(false); } return; }
  const t = e.target;
  if (t && t.closest && t.closest('input,textarea,select,[contenteditable="true"]')) {
    // Esc in a box lets go of it, so the next Esc and the shortcuts work again
    if (e.key === 'Escape' && t.blur) { e.preventDefault(); t.blur(); }
    return;
  }
  if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 'k' || e.key === 'K' || e.code === 'KeyK')) { e.preventDefault(); hqsFocus(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key;
  if (/^[1-9]$/.test(k)) { const sid = ORDER[+k - 1]; if (sid) { e.preventDefault(); openAgent(sid); } return; }
  if (k === 'n' || k === 'j' || k === 'N' || k === 'J') { e.preventDefault(); nextAgent(1); }
  else if (k === 'k' || k === 'K') { e.preventDefault(); nextAgent(-1); }
  else if (k === 'Escape') { if (FLEET.on) showFleet(false); else goHQ(); }
  else if (k === 'g' || k === 'G' || e.code === 'KeyG') { e.preventDefault(); showFleet(); }
  else if (k === 'r' || k === 'R') { e.preventDefault(); openRecruit(); }
  else if (k === 't' || k === 'T') showTro(true);
  else if (k === 'p' || k === 'P') showProf(true);
  else if (k === 'b' || k === 'B') toggleMusic();
  else if (k === 's' || k === 'S') showSett(true);
  else if (k === 'a' || k === 'A') showAcc(true);
  else if (k === 'v' || k === 'V') showSys(true);
  else if (k === 'm' || k === 'M') toggleSound();
  else if (k === '?') showHelp();
  else if (k === '/') { e.preventDefault(); focusComposer(); }
});
// the first gesture unlocks audio on Safari
document.addEventListener('pointerdown', () => { if (SND) SFX.init(); }, { once: true });

$('#recruit').onclick = openRecruit;
$('#back').onclick = goHQ;
$('#st-next').onclick = () => nextAgent(1);
$('#st-more').onclick = () => {
  try { const on = $('#chat').contentWindow.hqMore(); $('#st-more').classList.toggle('on', on); SFX.play('click'); } catch (e) {}
};
$('#st-stop').onclick = async () => {
  if (!CUR || CUR === 'new') return;
  SFX.play('click');
  const r = await act('interrupt', CUR);
  if (r.ok) toast({ i: 'debug-stop', t: 'Stopped', x: r.msg || '' });
  setTimeout(poll, 800);
};
// 05.11: /compact from the stage bar. The session squeezes its context into a
// summary and goes on; the button hides for a few minutes while it runs.
$('#st-compact').onclick = () => {
  if (!CUR || CUR === 'new') return;
  const sid = CUR, r = rowBy(sid);
  const size = r && r.ctx ? ' (' + tok(r.ctx) + ' of ' + tok(r.ctx_max) + ')' : '';
  confirmBox('Compact «' + titleOf(r) + '»' + size + '? Its context becomes a short summary: every next message gets cheaper, old details stay only in the summary.', 'COMPACT', async ok => {
    if (!ok) return;
    const res = await act('say', { id: sid, text: '/compact' });
    if (!res.ok) return;
    COMPACTING[sid] = NOW(); $('#st-compact').hidden = true;
    SFX.play('click'); toast({ i: 'archive', t: 'Compacting', x: 'Takes a minute or three; the agent keeps its work.', ms: 7000 });
  });
};
$('#st-del').onclick = () => { if (CUR && CUR !== 'new') delSession(CUR); };
$('#st-done').onclick = () => {
  if (!CUR || CUR === 'new') return;
  const sid = CUR, r = rowBy(sid);
  confirmBox('Mission complete? «' + titleOf(r) + '» stops and goes to the archive. The classic panel can bring it back.', 'COMPLETE', async ok => {
    if (!ok) return;
    const res = await act('kill', sid);
    if (!res.ok) return;
    const el = CARD.get(sid);
    if (el) { const c = rectCenter(el); FX.burst(c[0], c[1], 40, ['#ffd23f', '#9dff57', '#ffffff'], 7); el.classList.add('gone'); setTimeout(() => { el.remove(); CARD.delete(sid); }, 650); }
    stageClose();
    setTimeout(poll, 800);
  });
};
$('#myes').onclick = () => modalClose(true);
$('#mno').onclick = () => modalClose(false);
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') modalClose(false); });
$('#help').addEventListener('click', e => { if (e.target.id === 'help') hideHelp(); });
$('#helpok').onclick = hideHelp;
$('#helpb').onclick = showHelp;
$('#snd').onclick = toggleSound;
$('#ntf').onclick = toggleNotify;
$('#trobtn').onclick = () => showTro(true);
$('#trook').onclick = () => showTro(false);
$('#tro').addEventListener('click', e => { if (e.target.id === 'tro') showTro(false); });
document.querySelectorAll('#dock button').forEach(b => b.onclick = () => {
  const g = b.dataset.go;
  SFX.play('click');
  if (g === 'hq') { FLEET.ret = false; if (FLEET.on) showFleet(false, true); goHQ(); }
  else if (g === 'fleet') { toggleSide(false); document.body.classList.remove('staged'); showFleet(); }
  else if (g === 'next') { toggleSide(false); nextAgent(1); }
  else if (g === 'recruit') { toggleSide(false); openRecruit(); }
});
$('#chat').addEventListener('load', () => {
  // the classic page posts 'ready' when it boots; a blank or failed frame does not
  try { const cw = $('#chat').contentWindow; IFRAME_READY = !!(cw && cw.rcHas); } catch (e) { IFRAME_READY = false; }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

// ------------------------------------------------------------------ drag & drop: hand a file to an agent
// the owner 10-16: dropping a file «froze the screen». The overlay of the chat could
// stick (unpaired dragenter/dragleave), an upload hung on the lossy link with
// no retry, and a file let go over the board opened in the tab instead of HQ.
// Now the whole page is the drop zone: every live agent becomes a target, the
// one under the cursor lights up and looks up, the file flies into it, and a
// tray follows each transfer to «attached» (or retries, or says why not).
const DROP = { on: false, t: null, targets: [], hot: null };
let DROP_QUEUE = null;              // files waiting for that agent's chat to load
const TRAY = new Map();
function isFiles(ev) { const t = ev.dataTransfer && ev.dataTransfer.types; return !!(t && Array.prototype.indexOf.call(t, 'Files') >= 0); }
function fileIcon(n) {
  n = String(n || '').toLowerCase();
  if (/\.(zip|gz|tgz|tar|rar|7z)$/.test(n)) return 'archive';
  if (/\.pdf$/.test(n)) return 'file';
  if (/\.(mp4|mov|webm|mkv)$/.test(n)) return 'video';
  if (/\.(mp3|m4a|wav|ogg|aac)$/.test(n)) return 'music';
  if (/\.(csv|xlsx?|numbers|tsv)$/.test(n)) return 'table';
  if (/\.(png|jpe?g|gif|webp|heic|svg)$/.test(n)) return 'image';
  return 'file-alt';
}
function shortName(n) { n = String(n || 'file'); return n.length > 34 ? n.slice(0, 20) + '…' + n.slice(-12) : n; }
function kb(n) { n = n || 0; return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB'; }
function dropTargets() {
  const out = [];
  if (CUR && $('#stage').classList.contains('has')) {
    const r = $('.stagewrap').getBoundingClientRect();
    if (r.width > 40 && r.height > 40)
      out.push({ sid: CUR, kind: 'stage', r, name: CUR === 'new' ? 'the new agent' : (titleOf(rowBy(CUR)) || CUR) });
  }
  CARD.forEach((el, sid) => {
    if (!el.isConnected || el.offsetParent === null || el.classList.contains('st-sleep') || el.classList.contains('gone')) return;
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight || r.width < 10) return;
    out.push({ sid, kind: 'card', r, name: titleOf(rowBy(sid)) || sid });
  });
  return out;
}
function dropOpen() {
  if (DROP.on) return;
  DROP.on = true;
  DROP.hot = null;
  DROP.targets = dropTargets();
  $('#dz-targets').innerHTML = DROP.targets.map((t, i) =>
    '<div class="dt ' + t.kind + '" data-i="' + i + '" style="left:' + t.r.left + 'px;top:' + t.r.top + 'px;width:' + t.r.width + 'px;height:' + t.r.height + 'px">'
    + '<span>' + ico(t.kind === 'stage' ? 'inbox' : 'gift') + (t.kind === 'stage' ? 'attach to ' : 'give to ') + esc(t.name) + '</span></div>').join('');
  $('#dz-sub').textContent = DROP.targets.some(t => t.kind === 'stage')
    ? 'the open chat takes it, or let go over any agent' : 'let go over an agent card';
  document.body.classList.add('dropping');
}
function dropKeep() { if (DROP.t) clearTimeout(DROP.t); DROP.t = setTimeout(dropClose, 450); }
function dropClose() {
  if (DROP.t) { clearTimeout(DROP.t); DROP.t = null; }
  if (!DROP.on) return;
  DROP.on = false; DROP.hot = null;
  document.body.classList.remove('dropping');
  $('#dz-targets').innerHTML = '';
  CARD.forEach(el => el.classList.remove('catch'));
  if (G) board();                    // the caught sprites go back to what they do
}
function dropHit(x, y) {
  let hit = null;
  DROP.targets.forEach((t, i) => {
    const r = t.r;
    if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom && (hit === null || t.kind === 'card')) hit = i;
  });
  return hit;
}
function dropMove(x, y) {
  const i = dropHit(x, y);
  if (i === DROP.hot) return;
  DROP.hot = i;
  document.querySelectorAll('#dz-targets .dt').forEach(el => el.classList.toggle('hot', +el.dataset.i === i));
  CARD.forEach(el => el.classList.remove('catch'));
  const t = i === null ? null : DROP.targets[i];
  if (t && t.kind === 'card') {
    const el = CARD.get(t.sid);
    if (el) {
      el.classList.add('catch');
      const v = SPRITES.get(el.querySelector('canvas'));
      if (v) v.st = 'ask';              // looks up, mouth open: «give it here»
    }
  }
  if (t) SFX.play('hover');
}
function flyParcel(f, x, y, dest, delay) {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const el = document.createElement('div');
  el.className = 'parcel';
  const img = f.type && f.type.indexOf('image/') === 0 && f.size < 20e6 ? URL.createObjectURL(f) : '';
  el.innerHTML = (img ? '<img src="' + img + '" alt="">' : '<span>' + ico(fileIcon(f.name)) + '</span>') + '<b>' + esc(shortName(f.name)) + '</b>';
  el.style.left = x + 'px'; el.style.top = y + 'px';
  document.body.appendChild(el);
  const dx = dest[0] - x, dy = dest[1] - y;
  const a = el.animate([
    { transform: 'translate(-50%,-50%) scale(.5) rotate(-10deg)', opacity: 0 },
    { transform: 'translate(-50%,-50%) translate(0px,-26px) scale(1.12) rotate(5deg)', opacity: 1, offset: 0.22 },
    { transform: 'translate(-50%,-50%) translate(' + (dx * 0.5) + 'px,' + (dy * 0.5 - 80) + 'px) scale(.95) rotate(-6deg)', opacity: 1, offset: 0.62 },
    { transform: 'translate(-50%,-50%) translate(' + dx + 'px,' + dy + 'px) scale(.2) rotate(0deg)', opacity: 0.15 }
  ], { duration: 760, delay: delay || 0, easing: 'cubic-bezier(.45,.05,.35,1)', fill: 'forwards' });
  a.onfinish = () => { el.remove(); if (img) setTimeout(() => URL.revokeObjectURL(img), 500); };
}
function deliverFiles(sid, files) {
  try {
    const cw = $('#chat').contentWindow;
    const h = String(cw.location.hash || '').replace('#', '');
    if (IFRAME_READY && cw.uploadMany && h === sid) { cw.uploadMany(files); return true; }
  } catch (e) {}
  return false;
}
function dropFlush() {
  if (!DROP_QUEUE) return;
  if (Date.now() - DROP_QUEUE.at > 120000) { DROP_QUEUE = null; return; }
  if (deliverFiles(DROP_QUEUE.sid, DROP_QUEUE.files)) DROP_QUEUE = null;
}
function handFiles(t, files, x, y) {
  const sid = t.sid;
  if (sid === 'new') { SFX.play('gulp'); bayUpload(files); return; }
  let dest;
  if (t.kind === 'stage') { const r = $('.stagewrap').getBoundingClientRect(); dest = [r.left + r.width / 2, r.bottom - 44]; }
  else { const c = (CARD.get(sid) || document.body).querySelector('.pix') || $('.stagewrap'); const r = c.getBoundingClientRect(); dest = [r.left + r.width / 2, r.top + r.height / 2]; }
  files.slice(0, 6).forEach((f, k) => flyParcel(f, x, y, dest, k * 110));
  setTimeout(() => {
    SFX.play('gulp');
    FX.burst(dest[0], dest[1], 24, ['#ffd23f', '#4de1ff', '#ffffff'], 5, 0.12);
    if (t.kind === 'card') pulseCard(sid, 'bump');
  }, 640);
  if (sid !== CUR) openAgent(sid);
  if (noFilesRemote()) return;
  if (!deliverFiles(sid, files)) DROP_QUEUE = { sid, files, at: Date.now() };
}
function trayUpdate(d) {
  if (!d.key) return;
  const box = $('#uptray');
  let el = TRAY.get(d.key);
  if (!el) {
    el = document.createElement('div');
    el.className = 'up';
    el.innerHTML = '<span class="ui"></span><div class="ut"><b></b><i class="ub"><u></u></i><small></small></div>';
    box.appendChild(el);
    TRAY.set(d.key, el);
    const ui = el.querySelector('.ui');
    if (d.img) { const im = new Image(); im.alt = ''; im.onerror = () => { ui.innerHTML = ico(fileIcon(d.name)); }; im.src = d.img; ui.appendChild(im); }
    else ui.innerHTML = ico(fileIcon(d.name));
    el.querySelector('b').textContent = shortName(d.name);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('in')));
  }
  const who = d.sid ? (titleOf(rowBy(d.sid)) || d.sid) : 'the new agent';
  const pct = d.state === 'done' ? 100 : (d.pct || 0);
  el.querySelector('u').style.width = pct + '%';
  el.className = 'up in ' + d.state;
  el.querySelector('small').textContent = d.state === 'done' ? 'attached to ' + who + ' · ' + kb(d.size)
    : d.state === 'retry' ? 'weak link, retrying ' + (d.retry + 1) + '/4 · ' + (d.msg || '')
    : d.state === 'fail' ? 'not attached: ' + (d.msg || 'failed')
    : (d.state === 'start' ? 'starting' : pct + '%') + ' to ' + who + ' · ' + kb(d.size);
  if (d.state === 'done') { SFX.play('loot'); setTimeout(() => trayGone(d.key), 2600); }
  if (d.state === 'fail') { SFX.play('error'); setTimeout(() => trayGone(d.key), 10000); }
}
function trayGone(key) {
  const el = TRAY.get(key);
  if (!el) return;
  el.classList.add('out');
  setTimeout(() => { el.remove(); TRAY.delete(key); }, 360);
}
window.addEventListener('dragenter', e => { if (!isFiles(e)) return; e.preventDefault(); dropOpen(); dropKeep(); dropMove(e.clientX, e.clientY); });
window.addEventListener('dragover', e => {
  if (!isFiles(e)) return;
  e.preventDefault();
  try { e.dataTransfer.dropEffect = 'copy'; } catch (_) {}
  dropOpen(); dropKeep(); dropMove(e.clientX, e.clientY);
});
window.addEventListener('drop', e => {
  if (!isFiles(e)) return;
  e.preventDefault();                // never let the browser open the file instead of HQ
  const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
  if (!DROP.on) dropOpen();
  const i = dropHit(e.clientX, e.clientY);
  let t = i === null ? null : DROP.targets[i];
  if (!t) t = DROP.targets.find(x => x.kind === 'stage') || null;
  dropClose();
  if (!files.length) return;
  if (!t) { SFX.play('error'); toast({ i: 'gift', t: 'Let go over an agent', x: 'Open a chat, or drop the file right on an agent card.', cls: 'bad' }); return; }
  handFiles(t, files, e.clientX, e.clientY);
});
window.addEventListener('blur', () => setTimeout(() => { if (DROP.on && !document.hasFocus()) dropKeep(); }, 0));

// ------------------------------------------------------------------ send / receive juice
function sendFx(sid) {
  hypeSend();
  const sw = $('.stagewrap').getBoundingClientRect();
  const sp = $('#stagepix').getBoundingClientRect();
  SFX.play('launch');
  FX.bolt(sw.left + sw.width / 2, sw.bottom - 40, sp.left + sp.width / 2, sp.top + sp.height / 2, '#4de1ff');
  const st = $('.stpix'); st.classList.remove('rx'); void st.offsetWidth; st.classList.add('rx');
}
function floatTag(text, x, y, cls) {
  const d = document.createElement('div');
  d.className = 'floattag ' + (cls || '');
  d.textContent = text;
  d.style.left = x + 'px'; d.style.top = y + 'px';
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 1300);
}
function ackFx(sid) {
  SFX.play('ack');
  pulseCard(sid, 'bump');
  const sp = $('#stagepix').getBoundingClientRect();
  if (sp.width) floatTag('RECEIVED', sp.right + 8, sp.top + 2, 'ack');
}
// the radar on the empty stage: one blip per live agent, lit as the sweep passes
function radarDraw(groups) {
  const box = $('#blips');
  if (!box || $('#stage').classList.contains('has')) { if (box) box.innerHTML = ''; return; }
  let h = '';
  [['ask', 0.3], ['fresh', 0.45], ['busy', 0.64], ['idle', 0.85]].forEach(([k, rr]) => {
    groups[k].forEach(r => {
      const deg = hash(r.short) % 360, a = deg * Math.PI / 180;
      const x = 120 + Math.cos(a) * 108 * rr, y = 120 + Math.sin(a) * 108 * rr;
      const delay = (((deg + 90) % 360) / 360) * 3.2 - 3.2;
      h += '<button class="blip ' + k + '" data-sid="' + esc(r.short) + '" title="' + esc(titleOf(r)) + '" style="left:' + x.toFixed(0)
        + 'px;top:' + y.toFixed(0) + 'px;animation-delay:' + delay.toFixed(2) + 's"></button>';
    });
  });
  box.innerHTML = h;
}

// ------------------------------------------------------------------ launch bay: recruiting a new agent
// the owner 10-16: «при запуске новой сессии должна быть увлекательная анимашка и
// лоадер, чтобы я не ушёл на другую страницу и не заблокировал отправку».
// The launch belongs to HQ itself, not to the chat frame: it keeps going while
// you open other agents, carries a nonce (a retry never creates a second
// session), survives a reload (it is asked for by nonce), and shows a launch
// card on the board until the agent lands.
let LAUNCHES = (LS.get('launches', []) || []).filter(L => L && Date.now() - L.at < 15 * 60000);
let BAY_SEQ = null;
const BAY = { files: [] };
function saveLaunches() { LS.set('launches', LAUNCHES.filter(L => L.state === 'flying' || Date.now() - (L.done_at || 0) < 60000)); }
function newNonce() { return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10); }
function sleepMs(ms) { return new Promise(r => setTimeout(r, ms)); }
function bayShow(on) {
  $('#bay').hidden = !on;
  $('#chat').classList.toggle('behind', !!on);
  if (on) {
    bayFillOpts(); bayFormShow(BAY_SEQ ? false : true);
    SPRITES.set($('#bay-ship'), { ship: true });
    if (!BAY_SEQ) { clearArrive(); $('#hangar').className = 'hangar'; SPRITES.set($('#bay-bot'), { sp: specFor('recruit-' + Math.floor(Date.now() / 60000), 1), st: 'happy' }); }
    else { const L = seqL(); if (L && Date.now() - SEQ_T0 > 3700) seqState(L, true); }
  }
}
function bayFormShow(form) { $('#bay-form').hidden = !form; $('#bay-seq').hidden = form; }
function bayFillOpts() {
  if (!G) return;
  const ms = $('#bay-model'), es = $('#bay-effort');
  if (!ms.options.length) {
    ms.innerHTML = (G.models || []).map(m => '<option value="' + esc(m[0]) + '">' + esc(m[1]) + '</option>').join('');
    es.innerHTML = (G.efforts || []).map(e => '<option value="' + esc(e) + '">' + esc(e || 'default effort') + '</option>').join('');
    ms.value = LS.get('bayModel', G.model_default || '');
    es.value = LS.get('bayEffort', G.effort_default || '');
  }
}
function bayFilesDraw() {
  $('#bay-files').innerHTML = BAY.files.map((f, i) => '<span class="bayf' + (f.path ? ' ok' : f.failed ? ' bad' : '') + '" title="' + esc(f.name) + '">'
    + (f.img ? '<img src="' + f.img + '" alt="">' : ico(fileIcon(f.name))) + '<em>' + (f.path ? '' : f.failed ? '!' : (f.pct || 0) + '%') + '</em>'
    + '<button data-i="' + i + '" title="Remove">' + ico('close') + '</button></span>').join('');
}
function noFilesRemote() {
  if (!HQ_DEV) return false;
  SFX.play('error');
  toast({ i: 'warning-box', t: 'Files stay on this computer', x: 'Uploads land on the hub, not on ' + devName() + '. Switch to this computer to attach files.', cls: 'bad', ms: 9000 });
  return true;
}
function bayUpload(files) {
  if (noFilesRemote()) return;
  files.forEach(file => {
    const it = { name: file.name || 'file', size: file.size || 0, pct: 0, path: '', failed: false,
      img: file.type && file.type.indexOf('image/') === 0 ? URL.createObjectURL(file) : '',
      key: newNonce() };
    BAY.files.push(it); bayFilesDraw();
    (async () => {
      for (let t = 1; t <= 4 && !it.path; t++) {
        try {
          const j = await new Promise((resolve, reject) => {
            const x = new XMLHttpRequest();
            let last = Date.now();
            const w = setInterval(() => { if (Date.now() - last > 30000) { try { x.abort(); } catch (e) {} } }, 2000);
            x.open('POST', '/upload?uuid=&name=' + encodeURIComponent(it.name) + '&size=' + it.size + '&key=' + encodeURIComponent(it.key));
            x.upload.onprogress = e => { last = Date.now(); if (e.lengthComputable) { it.pct = Math.min(99, Math.round(e.loaded * 100 / e.total)); bayFilesDraw(); } };
            x.onload = () => { clearInterval(w); try { resolve(JSON.parse(x.responseText)); } catch (e) { reject(e); } };
            x.onerror = () => { clearInterval(w); reject(new Error('connection lost')); };
            x.onabort = () => { clearInterval(w); reject(new Error('stalled')); };
            x.send(file);
          });
          if (!j.ok) throw new Error(j.msg || 'upload failed');
          it.path = j.path; it.pct = 100; SFX.play('loot');
        } catch (e) { if (t === 4) it.failed = true; else await sleepMs(1500 * t); }
        bayFilesDraw();
      }
    })();
  });
}
// Dictating the briefing (09.11, the owner). Same /voice as the chat mic, which lives in the chat iframe behind the bay. Unlike
// the chat it never sends by itself: the text lands in the briefing, because model, thrust and files are still to pick and
// a stray session costs more than a stray message. A recording that got no answer is kept for RETRY.
const BV = { rec: null, stream: null, chunks: [], opening: false, busy: false, blob: null, ac: null, tick: 0 };
function bvPaint(st) {
  const b = $('#bay-mic'), box = $('#bay-rec');
  b.className = 'bay-mic' + (st ? ' ' + st : ''); b.disabled = st === 'busy';
  b.querySelector('span').textContent = st === 'rec' ? 'STOP' : st === 'busy' ? 'WAIT' : st === 'retry' ? 'RETRY' : 'DICTATE';
  box.hidden = st !== 'rec' && st !== 'busy';
  box.classList.toggle('done', st === 'busy');
  box.querySelector('b').textContent = st === 'busy' ? 'TRANSCRIBING' : 'REC';
}
function bvStop() { if (BV.rec && BV.rec.state !== 'inactive') { try { BV.rec.stop(); } catch (e) {} } }
// on a phone «back» only slides the stage away, the bay stays laid out: offsetParent alone says it is still on screen
function bvOnScreen() {
  return CUR === 'new' && !$('#bay').hidden && !!$('#bay-mic').offsetParent && !FLEET.on
    && (innerWidth > 860 || document.body.classList.contains('staged'));
}
function bvMeter(stream) {
  const cv = $('#bay-rec canvas'), g = cv.getContext('2d'), t0 = Date.now(), em = $('#bay-rec em');
  g.clearRect(0, 0, cv.width, cv.height); em.textContent = '0:00';
  let an = null, data = null;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    BV.ac = new AC(); if (BV.ac.resume) BV.ac.resume();
    an = BV.ac.createAnalyser(); an.fftSize = 256; BV.ac.createMediaStreamSource(stream).connect(an);
    data = new Uint8Array(an.frequencyBinCount);
  } catch (e) { an = null; }   // no meter, the recording itself still works
  BV.tick = setInterval(() => {
    // ⛔ the mic must not stay open behind a screen nobody is looking at: leaving the bay ends the take, the text still lands
    if (!bvOnScreen()) { bvStop(); return; }
    const sec = Math.floor((Date.now() - t0) / 1000);
    em.textContent = Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
    if (!an) return;
    an.getByteFrequencyData(data);
    const W = cv.width, H = cv.height, n = 40, bw = W / n;
    g.clearRect(0, 0, W, H);
    for (let i = 0; i < n; i++) {
      const h = Math.max(4, data[Math.floor(i * data.length / n / 1.7)] / 255 * H);
      g.fillStyle = i % 3 === 0 ? '#ff5fa2' : i % 2 ? '#4de1ff' : '#9dff57';
      g.fillRect(Math.round(i * bw) + 2, Math.round((H - h) / 2), Math.max(3, Math.round(bw) - 5), Math.round(h));
    }
  }, 70);
}
async function bvToggle() {
  if (BV.rec) { bvStop(); return; }
  if (BV.busy || BV.opening) return;
  if (BV.blob) { bvSend(BV.blob); return; }
  BV.opening = true;
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } }); }
  catch (e) { BV.opening = false; SFX.play('sad'); toast({ i: 'warning-box', t: 'No microphone', x: (e && e.name) || String(e), cls: 'bad', ms: 7000 }); return; }
  BV.opening = false;
  let rec;
  try { rec = new MediaRecorder(stream); }
  catch (e) { stream.getTracks().forEach(t => t.stop()); SFX.play('sad'); toast({ i: 'warning-box', t: 'Cannot record here', x: String(e), cls: 'bad', ms: 7000 }); return; }
  BV.rec = rec; BV.stream = stream; BV.chunks = [];
  rec.ondataavailable = ev => { if (ev.data && ev.data.size) BV.chunks.push(ev.data); };
  rec.onstop = () => {
    clearInterval(BV.tick);
    try { if (BV.ac) BV.ac.close(); } catch (e) {}
    BV.ac = null;
    BV.stream.getTracks().forEach(t => t.stop());
    const blob = new Blob(BV.chunks, { type: rec.mimeType || 'audio/webm' });
    BV.rec = null; BV.stream = null; BV.chunks = [];
    SFX.play('comm-close');
    if (!blob.size) { bvPaint(''); SFX.play('sad'); toast({ i: 'warning-box', t: 'Nothing recorded', cls: 'bad' }); return; }
    bvSend(blob);
  };
  rec.start();
  bvPaint('rec'); SFX.play('comm-open'); bvMeter(stream);
}
async function bvSend(blob) {
  BV.busy = true; BV.blob = null; bvPaint('busy');
  let j = null;
  for (let t = 1; t <= 3 && !j; t++) {
    try { j = await (await fetchT('/voice?name=voice.webm', { method: 'POST', body: blob }, 240000)).json(); }
    catch (e) { if (t < 3) await sleepMs(2000 * t); }
  }
  BV.busy = false;
  if (!j) {
    BV.blob = blob; bvPaint('retry'); SFX.play('sad');
    toast({ i: 'warning-box', t: 'Not transcribed', x: 'No answer from the computer. The recording is kept: press RETRY.', cls: 'bad', ms: 9000 });
    return;
  }
  bvPaint('');
  const text = j.ok ? (j.text || '').trim() : '';
  if (!text) { SFX.play('sad'); toast({ i: 'warning-box', t: 'Nothing recognised', x: j.msg || '', cls: 'bad', ms: 9000 }); return; }
  const ta = $('#bay-text'), was = ta.value.replace(/\s+$/, '');
  ta.value = (was ? was + ' ' : '') + text;
  ta.scrollTop = ta.scrollHeight;
  SFX.play('heard');
  if (bvOnScreen()) ta.focus();
  else toast({ i: 'mic', t: 'Dictation is in the launch bay', x: text.slice(0, 80), ms: 7000 });
}
function launchPhase(L) {
  const s = (Date.now() - L.at) / 1000;
  if (L.state === 'online') return [5, L.picked ? 'Delivered' : 'Online'];
  if (L.state === 'failed') return [-1, 'Launch failed'];
  if (L.mode === 'continue') return s < 1.5 ? [0, 'Order received'] : s < 5 ? [1, 'Searching every session'] : s < 9 ? [2, 'Picking the match'] : s < 25 ? [3, 'Delivering the order'] : [4, 'Waiting for it to land'];
  return s < 1.5 ? [0, 'Order received'] : s < 4 ? [1, 'Booting Claude'] : s < 7 ? [2, 'Opening the channel'] : s < 12 ? [3, 'Typing your order'] : [4, 'Waiting for the first words'];
}
const PHASES = ['Order received', 'Booting Claude', 'Opening the channel', 'Typing your order', 'First words'];
function launchCardsDraw() {
  const box = $('#launches');
  if (!LAUNCHES.length) { box.innerHTML = ''; box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = '<h2><i class="led"></i>' + ico('rocket') + '<span class="nm">LAUNCH BAY</span><span class="n">' + LAUNCHES.length + '</span></h2>'
    + LAUNCHES.map(L => {
      const [i, word] = launchPhase(L);
      const pct = L.state === 'online' ? 100 : L.state === 'failed' ? 100 : Math.min(92, 8 + i * 20 + ((Date.now() - L.at) / 1000) % 4 * 2);
      return '<div class="launchc ' + L.state + '" data-n="' + esc(L.nonce) + '"><div class="pix"><canvas width="16" height="16" data-n="' + esc(L.nonce) + '"></canvas></div>'
        + '<div class="ag-main"><b class="ag-name">' + esc(L.title || 'new agent') + '</b>'
        + '<div class="lc-st">' + ico(L.state === 'failed' ? 'warning-box' : L.state === 'online' ? 'check' : 'loader', L.state === 'flying' ? 'spin' : '')
        + '<span>' + esc(L.state === 'failed' ? 'Launch failed: ' + (L.msg || '') : word) + '</span><em>' + Math.round((Date.now() - L.at) / 1000) + 's</em></div>'
        + '<div class="lc-bar"><i style="width:' + pct + '%"></i></div></div>'
        + (L.state === 'failed' ? '<button class="wake" data-retry="' + esc(L.nonce) + '">' + ico('reload') + 'RETRY</button>' : '')
        + (L.state === 'online' && L.sid ? '<button class="wake" data-open="' + esc(L.sid) + '">' + ico('chevron-right') + 'OPEN</button>' : '') + '</div>';
    }).join('');
  box.querySelectorAll('canvas[data-n]').forEach(cv => SPRITES.set(cv, { sp: specFor(cv.dataset.n, 1), st: 'busy' }));
}
setInterval(() => { if (LAUNCHES.some(L => L.state === 'flying')) { launchCardsDraw(); if (BAY_SEQ) seqDraw(); } }, 1000);
// the owner 10-16: «весь флоу запуска сессии пока ужасно выглядит». One viewscreen
// tells the whole story: the briefing in the hangar, the countdown and the
// jump, jump while the mini boots Claude, then the arrival: the new
// agent on screen and a big OPEN that fires by itself unless you move on.
const PATH = ['ORDER', 'BOOT', 'CHANNEL', 'BRIEFING', 'FIRST WORDS'];
const PATH_C = ['ORDER', 'SEARCH', 'MATCH', 'DELIVER', 'LANDED'];
let SEQ_T0 = 0, ARRIVE_T = null, ARRIVE_N = 0;
function seqL() { return LAUNCHES.find(x => x.nonce === BAY_SEQ); }
function modelLabel(m) { const x = ((G && G.models) || []).find(o => o[0] === m); return x ? x[1] : (m || 'CLI default'); }
function seqDraw() {
  const L = seqL();
  if (!L) return;
  const [i, word] = launchPhase(L), st = L.state;
  const secs = Math.max(0, Math.round(((L.done_at || Date.now()) - L.at) / 1000));
  const at = Math.max(0, i);
  const cont = L.mode === 'continue', P = cont ? PATH_C : PATH;
  $('#bay-path').innerHTML = P.map((w, k) => '<li class="' + (st === 'online' || k < at ? 'ok' : st === 'failed' && k === at ? 'bad' : st === 'flying' && k === at ? 'now' : '') + '"><i></i><span>' + w + '</span></li>').join('');
  $('#bay-path').style.setProperty('--prog', String(st === 'online' ? 1 : at / (PATH.length - 1)));
  const pk = L.picked;
  $('#bay-state').textContent = st === 'online' ? (pk ? 'SENT TO ' + L.sid + ' · ' + secs + 's' : 'AGENT ONLINE · ' + secs + 's')
    : st === 'failed' ? 'LAUNCH FAILED' : (cont ? 'SEARCHING · ' : 'IN JUMP · ') + word.toUpperCase() + ' · ' + secs + 's';
  $('#bay-state').className = 'bay-kicker bay-state ' + st;
  $('#bay-title').textContent = L.title || 'new agent';
  $('#bay-chips').innerHTML = (pk ? ['→ ' + (pk.title || L.sid), pk.state === 'archived' ? 'from the archive' : pk.state === 'idle' ? 'woken up' : 'live']
    : cont && st === 'online' ? ['no match: new agent', modelLabel(L.model)]
    : cont ? ['continue the match'] : [modelLabel(L.model), L.effort || 'default effort']).map(c => '<span>' + esc(c) + '</span>').join('');
  $('#bay-note').textContent = st === 'online' ? (pk ? (pk.why ? 'Why this one: ' + pk.why : 'Delivered.') : cont ? (L.msg || 'On the board now.') : 'On the board now.')
    : st === 'failed' ? (L.msg || 'no answer from the computer')
    : cont ? 'The mini reads every session, archived ones too, and sends the order to the one doing this work. Nothing fits = a new agent.'
    : 'It flies on its own: open other agents meanwhile, it lands on the board and pings you.';
  const hb = $('#hangar .harrive b'); if (hb) hb.textContent = pk ? 'ORDER DELIVERED' : 'AGENT ONLINE';
  $('#bay-note').className = 'bay-hint' + (st === 'failed' ? ' bad' : '');
  $('#bay-open').hidden = st !== 'online'; $('#bay-retry').hidden = st !== 'failed'; $('#bay-edit').hidden = st !== 'failed';
  $('#bay-board').hidden = st !== 'flying';
  $('#bay-hud-l').textContent = st === 'failed' ? 'ALERT' : st === 'online' ? 'ARRIVED' : 'JUMP ' + (Math.min(at, PATH.length - 1) + 1) + '/' + PATH.length;
  $('#bay-hud-r').textContent = secs + 's';
  if (CUR === 'new') stageBar();
}
// the viewscreen after the show: still flying, arrived, or failed
function seqState(L, quiet) {
  const hg = $('#hangar');
  seqDraw();
  if (L.state === 'flying') { hg.className = 'hangar fly'; return; }
  if (L.state === 'failed') { hg.className = 'hangar fail'; return; }
  SPRITES.set($('#bay-agent'), { sp: specFor(L.sid, 1), st: 'happy' });
  $('#bay-agent-name').textContent = L.title || L.sid;
  hg.className = 'hangar fly arrive';
  if (quiet) return;
  if (CINE.on && CINE.L === L) return;      // the full-screen scene docks into the chat itself
  hype('launch', 'mid', $('#bay-agent'));
  if (CUR === 'new') startArrive(L);
}
function clearArrive() { clearInterval(ARRIVE_T); ARRIVE_T = null; const e = $('#bay-open em'); if (e) e.textContent = ''; }
// OPEN fires by itself after a short count, unless you click something else
function startArrive(L) {
  clearArrive();
  ARRIVE_N = 3;
  const tick = () => {
    if (BAY_SEQ !== L.nonce || CUR !== 'new' || $('#bay').hidden) { clearArrive(); return; }
    if (ARRIVE_N <= 0) { clearArrive(); BAY_SEQ = null; openAgent(L.sid); return; }
    $('#bay-open em').textContent = ' · ' + ARRIVE_N;
    ARRIVE_N--;
  };
  tick(); ARRIVE_T = setInterval(tick, 1000);
}
function launchSeq(L, quiet) {
  BAY_SEQ = L.nonce; SEQ_T0 = Date.now(); clearArrive();
  const snd = n => { if (!quiet) SFX.play(n); };
  bayFormShow(false);
  const hg = $('#hangar');
  SPRITES.set($('#bay-bot'), { sp: specFor(L.nonce, 1), st: 'happy' });
  hg.className = 'hangar board';            // the bot climbs in
  seqDraw();
  const mine = () => BAY_SEQ === L.nonce;
  // T-3 … lift-off: the request is already on its way, this is only the show
  [300, 900, 1500].forEach((ms, k) => setTimeout(() => { if (!mine()) return; $('#bay-count').textContent = String(3 - k); snd('count'); }, ms));
  setTimeout(() => { if (!mine()) return; snd('spool'); hg.className = 'hangar board spool'; }, 350);
  setTimeout(() => {
    if (!mine()) return;
    $('#bay-count').textContent = '';
    hg.className = 'hangar board spool go';  // out of the door and into jump
    snd('jump');
    if (!quiet) { const r = $('#bay-ship').getBoundingClientRect(); FX.burst(r.left + 10, r.top + r.height / 2, 34, ['#ffd23f', '#ff8a3d', '#ff5fa2', '#ffffff'], 5, 0.05); }
  }, 2100);
  setTimeout(() => { if (mine()) seqState(L); }, 3700);
}
function launchRetry(L) {
  const shown = BAY_SEQ === L.nonce;
  L.state = 'flying'; L.nonce = newNonce(); L.posted = false; L.at = Date.now(); L.done_at = 0; L.msg = ''; L.sid = '';
  saveLaunches(); launchCardsDraw();
  if (shown) launchSeq(L, CINE.play(L));
  launchRun(L);
}
function launch() {
  const ta = $('#bay-text'), text = ta.value.trim();
  if (BV.rec || BV.busy) { SFX.play('click'); toast({ i: 'mic', t: BV.rec ? 'Still recording' : 'Still transcribing', x: BV.rec ? 'Press STOP first, the text lands in the briefing.' : 'The text lands in the briefing in a moment.' }); return; }
  if (BAY.files.some(f => !f.path && !f.failed)) { SFX.play('click'); toast({ i: 'hourglass', t: 'Files are still uploading', x: 'Launch when the tiles turn green.' }); return; }
  const paths = BAY.files.filter(f => f.path).map(f => '"' + f.path + '"');
  if (!text && !paths.length) { SFX.play('sad'); ta.classList.remove('shake'); void ta.offsetWidth; ta.classList.add('shake'); return; }
  const full = text && paths.length ? text + ' ' + paths.join(' ') : (text || paths.join(' '));
  const model = $('#bay-model').value, effort = $('#bay-effort').value;
  LS.set('bayModel', model); LS.set('bayEffort', effort);
  const L = { nonce: newNonce(), text: full, title: (text || BAY.files.map(f => f.name).join(', ')).slice(0, 90), at: Date.now(), state: 'flying', model, effort, mode: bayModeOf() };
  LAUNCHES.push(L); saveLaunches();
  ta.value = ''; BAY.files = []; bayFilesDraw();
  if (BV.blob) { BV.blob = null; bvPaint(''); }
  // the jump film is about a new ship; a continuation stays in the hangar
  const cine = L.mode === 'continue' ? false : CINE.play(L);
  launchSeq(L, cine);
  if (!cine) setTimeout(() => hype('launch', 'mid', $('#hangar')), 2200);
  launchCardsDraw();
  launchRun(L);
}
// ⛔ 10-16: the POST waited up to 150 s for its answer. On the lossy link the
// answer got lost while the session was already working, and the launch screen
// kept flying. Now the POST goes out once, and the result is asked for by nonce
// every 1.5 s from the third second; whichever arrives first lands the launch.
async function launchRun(L) {
  const body = JSON.stringify(Object.assign({ action: L.mode === 'continue' && !HQ_DEV ? 'continue' : 'new', target: { text: L.text, model: L.model, effort: L.effort, nolink: true }, nonce: L.nonce }, HQ_DEV ? { dev: HQ_DEV } : {}));
  let finished = false;
  const finish = j => { if (finished || L.state !== 'flying') return; finished = true; launchDone(L, j || {}); };
  if (HQ_DEV) {
    // ⛔ 17.11: the peers' older panel has no /act_result, and a 404 there means «never arrived, post again»:
    // one POST, no retries, and a page reloaded mid-flight never re-posts
    if (L.posted) { finish({ ok: true, msg: 'sent to ' + devName() + ' before the reload; it shows up on the board' }); return; }
    L.posted = true; saveLaunches();
    try { finish(await (await fetchT('/act', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body }, 150000)).json()); }
    catch (e) { finish({ ok: false, msg: 'no answer from ' + devName() + ' in 150 s; look at the board before launching again' }); }
    return;
  }
  const post = () => fetchT('/act', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body }, 40000).then(r => r.json()).then(finish).catch(() => {});
  if (!L.posted) { L.posted = true; saveLaunches(); post(); await sleepMs(2500); }
  let unknown = 0;
  for (let n = 1; n <= 240 && !finished && L.state === 'flying'; n++) {
    try {
      const r = await fetchT('/act_result?nonce=' + encodeURIComponent(L.nonce), { cache: 'no-store' }, 6000);
      const st = r.status === 404 ? { state: 'unknown' } : await r.json();
      if (st.state === 'done') { finish(st.out); break; }
      // the order never reached the mini: send it again, the nonce keeps it single
      if (st.state === 'unknown' && ++unknown % 4 === 0) post();
    } catch (e) {}
    await sleepMs(1500);
  }
  if (!finished && L.state === 'flying') finish({ ok: false, msg: 'no answer from the computer' });
}
const LANDED = {};
function launchDone(L, j) {
  const m = /(?:new|continued) session ([0-9a-f]{8})/.exec(String(j.msg || ''));
  // an older peer answers «new session new-17949… launched»: no short id yet, but it is up
  L.state = j.ok && (m || HQ_DEV) ? 'online' : 'failed';
  L.sid = m ? m[1] : '';
  L.picked = j.ok && j.picked ? j.picked : null;
  L.msg = String(j.msg || '').slice(0, 200);
  L.done_at = Date.now();
  saveLaunches(); launchCardsDraw();
  if (BAY_SEQ === L.nonce) { seqDraw(); setTimeout(() => { if (BAY_SEQ === L.nonce) seqState(L); }, Math.max(0, SEQ_T0 + 3800 - Date.now())); }
  if (CINE.on && CINE.L === L) CINE.done(L);
  if (L.state === 'online') { cleanupLanded(L); if (L.sid && !L.picked) FIRSTORDER[L.sid] = L.text; }
  if (L.state === 'online') {
    SFX.play('land');
    LANDED[L.sid] = Date.now();
    SEEN[L.sid] = NOW(); LS.set('seen', SEEN);
    // the viewscreen already says it when you are watching this launch
    if (!(CUR === 'new' && BAY_SEQ === L.nonce && !$('#bay').hidden)) {
      if (L.picked) toast({ i: 'repeat', t: 'Sent to ' + (L.picked.title || L.sid), x: L.picked.why || L.title, sid: L.sid, cls: 'turn', ms: 9000 });
      else toast({ i: 'rocket', t: L.mode === 'continue' ? 'No match, new agent online' : 'New agent online', x: L.title, sid: L.sid, cls: 'turn' });
    }
    setTimeout(poll, 400);
    setTimeout(() => { LAUNCHES = LAUNCHES.filter(x => x !== L); saveLaunches(); launchCardsDraw(); }, 9000);
  } else {
    SFX.play('sad');
    toast({ i: 'warning-box', t: 'Launch failed', x: L.msg, cls: 'bad', ms: 12000 });
  }
}
$('#bay-go').onclick = launch;
// 19.11 the owner: the order either starts a fresh session, or goes to the live or
// archived session already doing this work (the mini picks it, act «continue»;
// nothing fits = a new one). Peers run an older panel: there it is new only.
let BAY_MODE = LS.get('bayMode', 'new') === 'continue' ? 'continue' : 'new';
function bayModeOf() { return HQ_DEV ? 'new' : BAY_MODE; }
function bayModeDraw() {
  const m = bayModeOf();
  document.querySelectorAll('#bay-mode button').forEach(b => {
    const on = b.dataset.m === m;
    b.classList.toggle('on', on); b.setAttribute('aria-checked', on ? 'true' : 'false');
    b.disabled = b.dataset.m === 'continue' && !!HQ_DEV;
    if (b.disabled) b.title = 'Only on this computer for now';
  });
  $('#bay-kick').textContent = m === 'continue' ? 'LAUNCH BAY · CONTINUE THE MATCH' : 'LAUNCH BAY · NEW AGENT';
  $('#bay-go-t').textContent = m === 'continue' ? 'FIND & SEND' : 'LAUNCH';
  $('#bay-text').placeholder = m === 'continue' ? 'What comes next? It goes to the session already doing this work' : 'What should the new agent do?';
  try { if (CUR === 'new') stageBar(); } catch (e) {}
}
$('#bay-mode').addEventListener('click', e => {
  const b = e.target.closest('button[data-m]');
  if (!b || b.disabled) return;
  BAY_MODE = b.dataset.m; LS.set('bayMode', BAY_MODE); SFX.play('click'); bayModeDraw();
});
bayModeDraw();
// the browser gives a microphone only on https or localhost, as in the chat: on plain http there is no button
if (window.isSecureContext && navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === 'function' && typeof window.MediaRecorder === 'function') $('#bay-mic').hidden = false;
$('#bay-mic').onclick = bvToggle;
$('#bay-text').addEventListener('keydown', e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); launch(); } });
$('#bay-files').addEventListener('click', e => { const b = e.target.closest('button[data-i]'); if (b) { BAY.files.splice(+b.dataset.i, 1); bayFilesDraw(); } });
$('#bay-again').onclick = () => { clearArrive(); BAY_SEQ = null; bayShow(true); $('#bay-text').focus(); SFX.play('open'); };
$('#bay-open').onclick = () => { const L = seqL(); clearArrive(); if (L && L.sid) { BAY_SEQ = null; openAgent(L.sid); } };
$('#bay-retry').onclick = () => { const L = seqL(); if (L) { SFX.play('click'); launchRetry(L); } };
$('#bay-edit').onclick = () => {
  const L = seqL(); clearArrive(); BAY_SEQ = null;
  if (L) { $('#bay-text').value = L.text || ''; LAUNCHES = LAUNCHES.filter(x => x !== L); saveLaunches(); launchCardsDraw(); }
  bayShow(true); $('#bay-text').focus(); SFX.play('open');
};
// the launch keeps flying on the board; the stage goes back to the radar
$('#bay-board').onclick = () => { clearArrive(); CUR = null; bayShow(false); $('#stage').classList.remove('has'); goHQ(); stageBar(); SFX.play('close'); };
$('#launches').addEventListener('click', e => {
  const o = e.target.closest('[data-open]'); if (o) { openAgent(o.dataset.open); return; }
  const r = e.target.closest('[data-retry]');
  if (r) { const L = LAUNCHES.find(x => x.nonce === r.dataset.retry); if (L) launchRetry(L); return; }
  const c = e.target.closest('.launchc'); if (c) { BAY_SEQ = c.dataset.n; openRecruit(); bayFormShow(false); seqDraw(); }
});
$('#blips').addEventListener('click', e => { const b = e.target.closest('.blip'); if (b) openAgent(b.dataset.sid); });
document.querySelectorAll('[data-crew]').forEach(b => b.onclick = () => {
  CREW = b.dataset.crew; LS.set('crew', CREW);
  document.querySelectorAll('[data-crew]').forEach(x => x.classList.toggle('on', x.dataset.crew === CREW));
  SFX.play(CREW === 'bots' ? 'ack' : 'click');
});
document.querySelectorAll('[data-crew]').forEach(x => x.classList.toggle('on', x.dataset.crew === CREW));
// launches still in the air when the page was reloaded: ask the mini by nonce
LAUNCHES.filter(L => L.state === 'flying').forEach(L => { L.posted = true; launchRun(L); });
launchCardsDraw();

// ------------------------------------------------------------------ pilot profile
// the owner 10-16: «я хочу увидеть свой профиль персонажа и ачивки, кликая на аватарку»
const RANK_NAMES = ['Desert Kid', 'Crew Mechanic', 'Crew Pilot', 'Smuggler', 'Padawan', 'Master Knight', 'Crew General', 'Master Master', 'Grand Master'];
function showProf(on) {
  $('#prof').hidden = !on;
  SFX.play(on ? 'open-big' : 'close');
  if (on) { profDraw(); $('#prof-ok').focus({ preventScroll: true }); $('#prof .hbox').scrollTop = 0; }
}
function profDraw() {
  if (!G) return;
  accShow();
  SPRITES.set($('#prof-pix'), { cmdr: true });
  $('#prof-rank').textContent = G.rank;
  $('#prof-lv').textContent = 'LEVEL ' + G.lv + (G.next_rank ? ' · next: ' + G.next_rank : ' · top rank');
  const span = Math.max(1, G.hi - G.lo), have = G.xp - G.lo;
  $('#prof-xp').style.width = clamp(have * 100 / span, 0, 100) + '%';
  $('#prof-xpt').textContent = have + ' / ' + span + ' XP to level ' + (G.lv + 1) + ' · ' + G.xp + ' XP in total';
  const t = G.totals || {}, d = G.today || {}, got = G.ach.filter(a => a.ts).length;
  const cells = [['arrow-right', t.cmds || 0, 'orders sent'], ['radio-handheld', t.voice || 0, 'by voice'], ['check', d.turns || 0, 'results today'],
    ['git-commit', d.commits || 0, 'commits today'], ['rocket', (d.deploys || 0) + (d.publishes || 0), 'shipped today'],
    ['flame', G.streak || 0, 'day streak'], ['trophy', got + '/' + G.ach.length, 'trophies']];
  $('#prof-stats').innerHTML = cells.map(c => '<div class="pst">' + ico(c[0]) + '<b>' + c[1] + '</b><span>' + c[2] + '</span></div>').join('');
  $('#prof-path').innerHTML = CMDR.map(([at, k], i) => '<div class="pp' + (G.lv >= at ? ' got' : '') + (cmdrKind(G.lv) === k ? ' cur' : '') + '">'
    + '<canvas width="16" height="16" data-lv="' + at + '"></canvas><b>LV ' + at + '</b><span>' + esc(RANK_NAMES[i]) + '</span></div>').join('');
  $('#prof-path').querySelectorAll('canvas').forEach(cv => SPRITES.set(cv, { cmdr: true, lv: +cv.dataset.lv }));
  $('#prof-achn').textContent = got + '/' + G.ach.length;
  $('#prof-ach').innerHTML = G.ach.slice().sort((x, y) => (y.ts ? 1 : 0) - (x.ts ? 1 : 0)).map(a => '<div class="pa' + (a.ts ? ' got' : '') + '">'
    + ico(a.ts ? (ACH_ICON[a.k] || 'trophy') : 'lock') + '<div><b>' + esc(a.t) + '</b><span>' + esc(a.d)
    + (a.ts ? ' · ' + new Date(a.ts * 1000).toLocaleDateString() : '') + '</span></div></div>').join('');
}
$('#cmdr').addEventListener('click', () => showProf(true));
// 11-17 phone: the HUD folds to one row; this unfolds it (play.css #hudmore)
(() => {
  const b = document.createElement('button');
  b.id = 'hudmore'; b.className = 'hb'; b.title = 'Show the full HUD'; b.textContent = '\u22ef';
  if (LS.get('hudopen', false)) document.body.classList.add('hudopen');
  b.onclick = () => { const on = document.body.classList.toggle('hudopen'); LS.set('hudopen', on); };
  $('#hud').appendChild(b);
})();
// no pinch zoom (the owner 11-17): iOS Safari ignores user-scalable=no, gesture events it honours
['gesturestart', 'gesturechange'].forEach(n => document.addEventListener(n, e => e.preventDefault(), { passive: false }));
$('#prof-ok').onclick = () => showProf(false);
$('#prof').addEventListener('click', e => { if (e.target.id === 'prof') showProf(false); });

// ------------------------------------------------------------------ key clicks
// the owner 10-16: «попробовать добавить звук на ввод текста» — console keys in the
// chat box, the launch bay and the goal box. One switch in the help screen.
let KEYS = LS.get('keys', true);
function keySound(e) {
  if (!SND || SOUNDS.keys === false || e.metaKey || e.ctrlKey || e.altKey || e.repeat) return;
  if (e.key === 'Enter') SFX.play('key-enter');
  else if (e.key === 'Backspace' || e.key === 'Delete') SFX.play('key-del');
  else if (e.key && e.key.length === 1) SFX.play('key');
}
function hookKeys(doc) {
  if (!doc || doc.__hqkeys) return;
  doc.__hqkeys = 1;
  doc.addEventListener('keydown', e => {
    const t = e.target;
    if (t && (t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && /^(text|search|)$/.test(t.type || '')))) keySound(e);
  }, true);
}
hookKeys(document);
// ------------------------------------------------------------------ launch score
// the owner 10-16: «должна быть музыка корабля». The scene's own cue, original and
// synthesised here (nobody's theme): a pulse that tightens through the countdown,
// a brass stab and a rising call at lift-off, a driving ostinato in jump
// (Dm – B♭ – F – C), and a major resolution on arrival. Its own sound switch.
const NOTE = { A1: 55, Bb1: 58.27, C2: 65.41, D2: 73.42, Eb2: 77.78, F2: 87.31, A2: 110, Bb2: 116.54, C3: 130.81, D3: 146.83, E3: 164.81, F3: 174.61, Fs3: 185, G3: 196,
  A3: 220, Bb3: 233.08, C4: 261.63, D4: 293.66, E4: 329.63, F4: 349.23, Fs4: 369.99, G4: 392, A4: 440, Bb4: 466.16, C5: 523.25, D5: 587.33, Fs5: 739.99, A5: 880, D6: 1174.66 };
const SCORE = {
  on: false, timer: null, loopAt: 0, bar: 0, ducked: false,
  BEAT: 60 / 112,
  ok() { return SND && SOUNDS.score !== false && SFX.init(); },
  start() {
    if (this.on || !this.ok()) return;
    const c = SFX.ctx, t = c.currentTime + 0.05;
    this.on = true; this.mode = 'launch'; this.t0 = t;
    this.bus = c.createGain(); this.bus.gain.setValueAtTime(0.0001, t); this.bus.gain.exponentialRampToValueAtTime(0.9, t + 0.35);
    this.bus.connect(SFX.out());
    if (MUSIC.on) { MUSIC.setVol(LS.get('mvol', 0.5) * 0.12); this.ducked = true; }
    const B = this.BEAT / 2;
    // the hangar: a low drone and 8ths that brighten towards lift-off
    this.pad(t, [NOTE.D2, NOTE.A2], 1.9, 0.05, 220);
    for (let i = 0; i * B < 1.85; i++) this.bass(t + i * B, NOTE.D2, B * 0.8, 300 + i * 110, 0.10 + i * 0.004);
    for (let i = 3; i * B < 1.85; i += 2) this.hat(t + i * B, 0.035);
    // lift-off at 1.85 s
    const L = t + 1.85;
    this.timp(L, NOTE.D2 * 2, 0.55); this.crash(L, 1.6);
    this.brass(L, [NOTE.D3, NOTE.A3, NOTE.D4, NOTE.F4], 0.55, 0.05);
    [[NOTE.D4, 0.1, 0.25], [NOTE.A4, 0.37, 0.25], [NOTE.D5, 0.64, 0.75]].forEach(([f, at, d]) => this.brass(L + at, [f, f * 1.5], d, 0.055));
    for (let i = 0; i * B < 1.75; i++) this.bass(L + i * B, NOTE.D2, B * 0.8, 1600, 0.12);
    for (let i = 0; i * B < 1.75; i++) this.hat(L + i * B + B / 2, 0.04);
    this.timp(t + 3.05, NOTE.D2 * 2, 0.6); this.crash(t + 3.05, 2.2);
    // jump from 3.6 s: the loop, scheduled a little ahead of time
    this.loopAt = t + 3.6; this.bar = 0;
    clearInterval(this.timer); this.timer = setInterval(() => this.tick(), 250);
  },
  tick() {
    if (!this.on || this.mode !== 'launch' && this.mode !== 'travel') return;
    const c = SFX.ctx, BAR = this.BEAT * 4;
    // Dm – B♭ – F – C: root for the bass, a pad chord, an arpeggio an octave up
    const CH = [[NOTE.D2, [NOTE.D3, NOTE.F3, NOTE.A3], [NOTE.D4, NOTE.F4, NOTE.A4]],
      [NOTE.Bb1, [NOTE.Bb2, NOTE.D3, NOTE.F3], [NOTE.Bb3, NOTE.D4, NOTE.F4]],
      [NOTE.F2, [NOTE.F3, NOTE.A3, NOTE.C4], [NOTE.F4, NOTE.A4, NOTE.C5]],
      [NOTE.C2, [NOTE.C3, NOTE.E3, NOTE.G3], [NOTE.C4, NOTE.E4, NOTE.G4]]];
    while (this.loopAt < c.currentTime + 1.2) {
      const t = this.loopAt, [root, pad, arp] = CH[this.bar % 4], B = this.BEAT / 2;
      this.pad(t, pad, BAR * 1.02, 0.03, 1300);
      for (let i = 0; i < 8; i++) this.bass(t + i * B, root, B * 0.78, 1200 + (i % 2) * 400, 0.11);
      for (let i = 0; i < 8; i++) this.hat(t + i * B + B / 2, i % 2 ? 0.03 : 0.045);
      for (let i = 0; i < 8; i++) this.arp(t + i * B, arp[[0, 1, 2, 1, 0, 2, 1, 2][i]], B * 0.6);
      this.timp(t, root * 2, 0.22); this.timp(t + BAR / 2, root * 2, 0.16);
      if (this.bar % 4 === 3) this.crash(t + BAR - 0.05, 1.2);
      this.loopAt += BAR; this.bar++;
    }
  },
  travel() { this.mode = 'travel'; },
  arrive() {
    if (!this.on) return;
    this.mode = 'arrive'; clearInterval(this.timer);
    const c = SFX.ctx, t = Math.max(c.currentTime + 0.05, Math.min(this.loopAt, c.currentTime + 0.4));
    [0, 0.12, 0.24, 0.36].forEach((d, i) => this.timp(t + d, NOTE.D2 * 2, 0.25 + i * 0.08));
    this.pad(t + 0.4, [NOTE.D3, NOTE.Fs3, NOTE.A3, NOTE.D4], 3.4, 0.05, 2200);
    this.brass(t + 0.4, [NOTE.D4, NOTE.A4], 1.4, 0.05);
    [NOTE.D5, NOTE.Fs5, NOTE.A5, NOTE.D6].forEach((f, i) => this.bell(t + 0.5 + i * 0.16, f));
    this.crash(t + 0.4, 2.6);
    this.fade(t + 2.6, 1.8);
  },
  fail() {
    if (!this.on) return;
    this.mode = 'fail'; clearInterval(this.timer);
    const c = SFX.ctx, t = c.currentTime + 0.05;
    this.pad(t, [NOTE.A1, NOTE.D2, NOTE.Eb2], 2.2, 0.06, 500);
    this.timp(t, NOTE.D2, 0.5);
    this.fade(t + 0.8, 1.6);
  },
  stop(fast) {
    if (!this.on) return;
    clearInterval(this.timer);
    this.fade(SFX.ctx.currentTime, fast ? 0.3 : 0.8);
  },
  fade(at, dur) {
    const bus = this.bus, c = SFX.ctx;
    this.on = false;
    try { bus.gain.cancelScheduledValues(at); bus.gain.setValueAtTime(Math.max(0.0001, bus.gain.value), at); bus.gain.exponentialRampToValueAtTime(0.0001, at + dur); } catch (e) {}
    setTimeout(() => { try { bus.disconnect(); } catch (e) {} }, (at - c.currentTime + dur + 0.3) * 1000);
    if (this.ducked) { this.ducked = false; setTimeout(() => { if (MUSIC.on) MUSIC.setVol(LS.get('mvol', 0.5)); }, (at - c.currentTime + dur) * 1000); }
  },
  // voices
  osc(type, f, t, d, g0, cut, q) {
    const c = SFX.ctx, o = c.createOscillator(), g = c.createGain(), lp = c.createBiquadFilter();
    o.type = type; o.frequency.setValueAtTime(f, t);
    lp.type = 'lowpass'; lp.frequency.setValueAtTime(cut || 2000, t); lp.Q.value = q || 0.7;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(g0, t + 0.012); g.gain.exponentialRampToValueAtTime(0.0001, t + d);
    o.connect(lp); lp.connect(g); g.connect(this.bus); o.start(t); o.stop(t + d + 0.05);
    return { o, lp, g };
  },
  bass(t, f, d, cut, v) { this.osc('sawtooth', f, t, d, v || 0.1, cut, 2); this.osc('square', f / 2, t, d, (v || 0.1) * 0.6, 400); },
  arp(t, f, d) { this.osc('square', f, t, d, 0.018, 3000); },
  hat(t, v) {
    const c = SFX.ctx, src = c.createBufferSource(), hp = c.createBiquadFilter(), g = c.createGain();
    src.buffer = SFX.noise(); hp.type = 'highpass'; hp.frequency.value = 7000;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(v || 0.04, t + 0.004); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.05);
    src.connect(hp); hp.connect(g); g.connect(this.bus); src.start(t, Math.random() * 0.5); src.stop(t + 0.08);
  },
  crash(t, d) {
    const c = SFX.ctx, src = c.createBufferSource(), hp = c.createBiquadFilter(), g = c.createGain();
    src.buffer = SFX.noise(); hp.type = 'highpass'; hp.frequency.value = 3500;
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.09, t + 0.01); g.gain.exponentialRampToValueAtTime(0.0001, t + d);
    src.connect(hp); hp.connect(g); g.connect(this.bus); src.start(t); src.stop(t + d + 0.05);
  },
  timp(t, f, v) {
    const c = SFX.ctx, o = c.createOscillator(), g = c.createGain();
    o.type = 'sine'; o.frequency.setValueAtTime(f, t); o.frequency.exponentialRampToValueAtTime(f * 0.55, t + 0.45);
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(v || 0.4, t + 0.008); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.7);
    o.connect(g); g.connect(this.bus); o.start(t); o.stop(t + 0.75);
  },
  brass(t, fs, d, v) {
    fs.forEach(f => [1, 1.006].forEach(k => {
      const n = this.osc('sawtooth', f * k, t, d + 0.15, v || 0.05, 700, 1.2);
      n.lp.frequency.setValueAtTime(700, t); n.lp.frequency.linearRampToValueAtTime(2800, t + 0.07); n.lp.frequency.linearRampToValueAtTime(1600, t + d);
    }));
  },
  pad(t, fs, d, v, cut) {
    const c = SFX.ctx;
    fs.forEach(f => [1, 1.004, 0.996].forEach(k => {
      const o = c.createOscillator(), g = c.createGain(), lp = c.createBiquadFilter();
      o.type = 'sawtooth'; o.frequency.value = f * k; lp.type = 'lowpass'; lp.frequency.value = cut || 1200;
      g.gain.setValueAtTime(0.0001, t); g.gain.linearRampToValueAtTime((v || 0.03) / 3, t + Math.min(0.35, d / 3));
      g.gain.setValueAtTime((v || 0.03) / 3, t + d - 0.25); g.gain.linearRampToValueAtTime(0.0001, t + d);
      o.connect(lp); lp.connect(g); g.connect(this.bus); o.start(t); o.stop(t + d + 0.05);
    }));
  },
  bell(t, f) { this.osc('sine', f, t, 1.2, 0.05, 6000); this.osc('sine', f * 2.76, t, 0.5, 0.015, 9000); },
};

// ------------------------------------------------------------------ launch scene
// the owner 10-16: «хочу сделать анимацию запуска на весь экран, с кораблём,
// улетающим в космос, и классными звуками». The whole screen: the hangar of a
// space station opens, a countdown, the engines spool, lift-off, the ship
// races off towards the jump point and goes to jump in a white flash.
// About 4 s; a click or any key skips it, the launch itself never waits on it.
const CINE = {
  on: false, raf: 0, t0: 0, timers: [], stars: [], parts: [], ship: null,
  enabled() { return LS.get('cine', true) !== false && !(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches); },
  // the owner 10-16: «корабль должен быть виден сзади и лететь вперёд». The camera
  // rides behind the ship: an X of four wings, four engines glowing at us, the
  // canopy and the astromech dome on top.
  makeRear() {
    const W0 = 41, H0 = 25, cx = 20, cy = 12;
    const c = document.createElement('canvas'); c.width = W0; c.height = H0;
    const g = c.getContext('2d');
    const P = (x, y, col) => { if (x < 0 || y < 0 || x >= W0 || y >= H0) return; g.fillStyle = col; g.fillRect(x, y, 1, 1); };
    const L = '#ece8ff', M = '#c9c2ee', D = '#8a83b9', K = '#2a2260', R = '#ff4d6d';
    for (const sx of [-1, 1]) for (const sy of [-1, 1]) {
      for (let i = 3; i <= 19; i++) {
        const x = cx + sx * i, y = cy + sy * Math.round(i * 0.5);
        P(x, y - 1, L); P(x, y, M); P(x, y + 1, D);
        if (i >= 12 && i <= 13) { P(x, y, R); P(x, y - 1, R); }
      }
      const tx = cx + sx * 20, ty = cy + sy * 10;
      P(tx, ty - 1, K); P(tx, ty, K); P(tx, ty + 1, K); P(tx + sx, ty, R);
      const ex = cx + sx * 7, ey = cy + sy * 4;
      for (let a = -2; a <= 2; a++) for (let b = -2; b <= 2; b++) { const r = Math.abs(a) + Math.abs(b); if (r >= 2 && r <= 3 && !(Math.abs(a) === 2 && Math.abs(b) === 2)) P(ex + a, ey + b, K); }
    }
    for (let y = -3; y <= 3; y++) for (let x = -3; x <= 3; x++) {
      if (Math.abs(x) === 3 && Math.abs(y) === 3) continue;
      const edge = Math.abs(x) === 3 || Math.abs(y) === 3;
      P(cx + x, cy + y, edge ? K : y < 0 ? L : M);
    }
    for (let x = -2; x <= 2; x++) P(cx + x, cy - 1, R);
    P(cx - 1, cy + 1, K); P(cx, cy + 1, '#ff8a3d'); P(cx + 1, cy + 1, K);
    for (let x = -1; x <= 1; x++) { P(cx + x, cy - 4, '#4de1ff'); P(cx + x, cy - 5, '#1f86a3'); }
    P(cx - 1, cy - 6, L); P(cx, cy - 6, '#7aa8ff'); P(cx + 1, cy - 6, L); P(cx, cy - 7, L);
    this.rearShip = c;
    this.engines = [[cx - 7, cy - 4], [cx + 7, cy - 4], [cx - 7, cy + 4], [cx + 7, cy + 4]];
  },
  rearS() { return Math.max(4, Math.round(Math.min(this.W, this.H) / 74)); },
  // the ship seen from behind, its four engines blazing at the camera
  rear(g, x, y, s, power) {
    const sp = this.rearShip, w = sp.width * s, h = sp.height * s, x0 = x - w / 2, y0 = y - h / 2;
    g.drawImage(sp, x0, y0, w, h);
    for (const [ex, ey] of this.engines) {
      const px = x0 + (ex + 0.5) * s, py = y0 + (ey + 0.5) * s;
      const fl = 0.78 + Math.random() * 0.22, R = s * (1 + power * 2.4) * fl;
      const grd = g.createRadialGradient(px, py, 0, px, py, R * 2.4);
      grd.addColorStop(0, 'rgba(255,255,255,' + (0.55 + 0.45 * power).toFixed(2) + ')');
      grd.addColorStop(0.22, 'rgba(255,196,110,' + (0.45 + 0.45 * power).toFixed(2) + ')');
      grd.addColorStop(0.6, 'rgba(255,95,162,' + (0.28 * power).toFixed(2) + ')');
      grd.addColorStop(1, 'rgba(255,95,162,0)');
      g.fillStyle = grd; g.fillRect(px - R * 2.4, py - R * 2.4, R * 4.8, R * 4.8);
      g.fillStyle = '#ffffff'; g.fillRect(px - s * 0.7, py - s * 0.7, s * 1.4, s * 1.4);
      // exhaust left behind comes at the camera
      if (power > 0.4 && Math.random() < 0.8) this.parts.push({ x: px, y: py, vx: (px - x) * 0.02 + (Math.random() - 0.5), vy: (py - y) * 0.03 + Math.random() * 1.2, life: 1, c: ['#ffd23f', '#ff8a3d', '#ff5fa2', '#4de1ff'][Math.floor(Math.random() * 4)], s: s * (0.5 + Math.random() * 0.6), grow: 1.04 });
    }
  },
  sparks(g) {
    for (const q of this.parts) { q.x += q.vx; q.y += q.vy; q.life -= 0.03; if (q.grow) q.s *= q.grow; }
    this.parts = this.parts.filter(q => q.life > 0);
    for (const q of this.parts) { g.globalAlpha = Math.max(0, q.life); g.fillStyle = q.c; g.fillRect(q.x - q.s / 2, q.y - q.s / 2, q.s, q.s); }
    g.globalAlpha = 1;
  },
  // star streaks coming at the camera from the vanishing point
  streaks(g, cx, cy, sp, lenK, tint) {
    const far = Math.hypot(this.W, this.H) * 0.62;
    if (!this.tun) this.tun = Array.from({ length: Math.round(220 + this.W * this.H / 6000) }, () => this.tunP(true));
    for (const p of this.tun) {
      p.r *= 1 + sp * (0.6 + p.z * 0.8);
      if (p.r > far) Object.assign(p, this.tunP(false));
      const len = lenK * (0.16 + p.z * 0.4) + 0.004;
      const ca = Math.cos(p.a), sa = Math.sin(p.a), al = Math.min(1, p.r / 120) * (0.4 + 0.6 * p.z);
      g.strokeStyle = 'rgba(' + (tint || p.c) + ',' + al.toFixed(2) + ')';
      g.lineWidth = p.z > 0.72 ? 2 : 1;
      g.beginPath(); g.moveTo(cx + ca * p.r, cy + sa * p.r); g.lineTo(cx + ca * p.r * (1 + len), cy + sa * p.r * (1 + len)); g.stroke();
    }
  },
  // the hangar seen from the cockpit's back: frames running to the open door
  corridor(g, cx, cy, zc, t) {
    const W = this.W, H = this.H, F = Math.max(W, H) * 0.5, N = 8, door = N + 0.6;
    if (zc > door - 0.05) return;
    const dz = door - zc, dw = F / dz, dh = F * 0.6 / dz;
    g.save(); g.beginPath(); g.rect(0, 0, W, H); g.rect(cx - dw, cy - dh, dw * 2, dh * 2);
    g.fillStyle = '#120f30'; g.fill('evenodd'); g.restore();
    g.strokeStyle = 'rgba(74,63,163,.55)'; g.lineWidth = 2;
    [[0, 0, -1, -1], [W, 0, 1, -1], [0, H, -1, 1], [W, H, 1, 1]].forEach(([x, y, sx, sy]) => { g.beginPath(); g.moveTo(x, y); g.lineTo(cx + sx * dw, cy + sy * dh); g.stroke(); });
    for (let k = N; k >= 1; k--) {
      const z = k - zc;
      if (z < 0.06) continue;
      const w = F / z, h = F * 0.6 / z, a = Math.min(1, 0.3 + 1.1 / z), lw = Math.max(1, 7 / z);
      g.globalAlpha = a;
      g.strokeStyle = '#4a3fa3'; g.lineWidth = lw; g.strokeRect(cx - w, cy - h, w * 2, h * 2);
      const n = 12, sw = w * 2 / n, sh = Math.max(2, 12 / z);
      for (let i = 0; i < n; i++) { g.fillStyle = i % 2 ? '#ffd23f' : '#1d1a36'; g.fillRect(cx - w + i * sw, cy + h - sh, sw, sh); }
      const on = (Math.floor(t / 120) + k) % N < 2, Lz = Math.max(2, 11 / z);
      g.fillStyle = on ? '#ffb547' : '#5a3a20';
      g.fillRect(cx - w - Lz / 2, cy - h - Lz / 2, Lz, Lz); g.fillRect(cx + w - Lz / 2, cy - h - Lz / 2, Lz, Lz);
      g.fillStyle = on ? '#ff4d6d' : '#4a1d2b';
      g.fillRect(cx - w - Lz / 2, cy + h - Lz / 2, Lz, Lz); g.fillRect(cx + w - Lz / 2, cy + h - Lz / 2, Lz, Lz);
    }
    g.globalAlpha = 1;
    // the door frame itself, with hazard lips
    g.strokeStyle = '#ffd23f'; g.lineWidth = Math.max(2, 5 / dz); g.strokeRect(cx - dw, cy - dh, dw * 2, dh * 2);
  },
  prep() {
    if (!this.rearShip) this.makeRear();
    if (this.ship) return;
    const c = document.createElement('canvas'); c.width = 24; c.height = 13;
    const g = c.getContext('2d');
    for (let y = 0; y < SHIP.length; y++) for (let x = 0; x < SHIP[y].length; x++) {
      const ch = SHIP[y][x];
      if (ch === '.' || ch === 'O') continue;
      g.fillStyle = SHIPC[ch]; g.fillRect(x, y, 1, 1);
    }
    this.ship = c;
  },
  play(L) {
    if (this.on || !this.enabled()) return false;
    this.prep();
    const box = $('#cine'), cv = $('#cine-cv'), r = window.devicePixelRatio || 1;
    this.W = innerWidth; this.H = innerHeight;
    cv.width = Math.round(this.W * r); cv.height = Math.round(this.H * r);
    this.g = cv.getContext('2d'); this.g.setTransform(r, 0, 0, r, 0, 0); this.g.imageSmoothingEnabled = false;
    this.stars = Array.from({ length: Math.round(this.W * this.H / 4200) }, () => ({ x: Math.random() * this.W, y: Math.random() * this.H, z: 0.15 + Math.random() * 0.85 }));
    this.parts = []; this.bg = null; this.tun = null; this.bgp = null;
    this.L = L; this.mode = 'launch'; this.pending = null;
    ['#cine-hud', '#cine-arrive', '#cine-fail'].forEach(q => $(q).className = '');
    $('#cine-skip').textContent = 'click or Esc to skip · the launch keeps going';
    this.on = true; this.t0 = performance.now(); this.timers = [];
    $('#cine-count').textContent = ''; $('#cine-cap').textContent = ''; $('#cine-cap').className = '';
    box.hidden = false; box.className = ''; void box.offsetWidth; box.className = 'in';
    SCORE.start();
    this.cue(0, () => SFX.play('hangar'));
    [350, 850, 1350].forEach((ms, k) => this.cue(ms, () => { const c = $('#cine-count'); c.textContent = String(3 - k); c.className = ''; void c.offsetWidth; c.className = 'pop'; SFX.play('count'); }));
    this.cue(420, () => SFX.play('spool'));
    this.cue(1850, () => {
      $('#cine-count').textContent = '';
      const cap = $('#cine-cap'); cap.textContent = HYPE_MODE === 'off' ? 'LIFT OFF' : hypeWord('liftoff'); cap.className = 'pop';
      SFX.play('liftoff');
    });
    this.cue(3050, () => { SFX.play('warp'); $('#cine-cap').className = 'pop gone'; });
    // the owner 10-16: «полный процесс запуска должен быть полноэкранно: после скорости
    // света я должен видеть летящий корабль и статусы, а потом переход в рабочий экран»
    this.cue(3600, () => this.toTravel());
    const loop = now => {
      if (!this.on) return;
      if (this.mode === 'launch') this.frame(now - this.t0);
      else this.travel(now - this.tt0);
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
    return true;
  },
  cue(ms, fn) { this.timers.push(setTimeout(() => { if (this.on) fn(); }, ms)); },
  skip() {
    if (!this.on) return;
    if (this.mode === 'arrive') { this.dock(true); return; }
    this.timers.forEach(clearTimeout); this.timers = [];
    $('#cine').className = 'in out fast';
    setTimeout(() => this.end(), 230);
  },
  end() {
    if (SCORE.on) SCORE.stop(true);
    this.on = false; cancelAnimationFrame(this.raf); clearInterval(this.hudT);
    this.timers.forEach(clearTimeout); this.timers = [];
    $('#cine').hidden = true; $('#cine').className = '';
  },
  // after the jump: the ship in the tunnel and the launch's own status, live
  toTravel() {
    this.mode = 'travel'; this.tt0 = performance.now(); this.slow = 0; this.red = 0;
    SCORE.travel();
    $('#cine-hud').className = 'on';
    $('#cine-skip').textContent = 'Esc: back to the board · it keeps flying';
    this.hud(); clearInterval(this.hudT); this.hudT = setInterval(() => this.hud(), 500);
    if (this.pending) { const L = this.pending; this.pending = null; this.cue(1400, () => this.done(L)); }
  },
  hud() {
    const L = this.L;
    if (!L) return;
    const [i, word] = launchPhase(L), st = L.state, at = Math.max(0, Math.min(i, PATH.length - 1));
    const secs = Math.max(0, Math.round(((L.done_at || Date.now()) - L.at) / 1000));
    $('#cine-path').innerHTML = PATH.map((w, k) => '<li class="' + (st === 'online' || k < at ? 'ok' : st === 'failed' && k === at ? 'bad' : st === 'flying' && k === at ? 'now' : '') + '"><i></i><span>' + w + '</span></li>').join('');
    $('#cine-path').style.setProperty('--prog', String(st === 'online' ? 1 : at / (PATH.length - 1)));
    $('#cine-phase').textContent = st === 'online' ? 'OUT OF JUMP' : st === 'failed' ? 'ENGINE FAILURE' : 'IN JUMP · JUMP ' + (at + 1) + '/' + PATH.length;
    $('#cine-secs').textContent = secs + 's';
    $('#cine-st').textContent = st === 'online' ? 'Agent online' : st === 'failed' ? 'Launch failed' : word + '…';
    $('#cine-title').textContent = L.title || 'new agent';
    $('#cine-chips').innerHTML = [modelLabel(L.model), L.effort || 'default effort'].map(c => '<span>' + esc(c) + '</span>').join('');
  },
  // the launch is back from the mini: arrive, or the red alert
  done(L) {
    if (!this.on || L !== this.L) return;
    if (this.mode === 'launch') { this.pending = L; return; }
    if (this.mode !== 'travel') return;
    this.hud();
    if (L.state === 'online') return this.arrive(L);
    if (L.state === 'failed') {
      this.mode = 'fail'; this.ft0 = performance.now();
      $('#cine-why').textContent = L.msg || 'no answer from the computer';
      $('#cine-fail').className = 'on'; $('#cine-hud').className = 'on bad';
      $('#cine-skip').textContent = 'Esc: close';
      SFX.play('error'); SFX.play('sad'); SCORE.fail();
    }
  },
  arrive(L) {
    this.mode = 'arrive'; this.at0 = performance.now();
    SCORE.arrive();
    SPRITES.set($('#cine-agent'), { sp: specFor(L.sid, 1), st: 'happy' });
    $('#cine-aname').textContent = L.title || L.sid;
    $('#cine-hud').className = 'on done';
    $('#cine-skip').textContent = 'click or Esc: open it now';
    this.cue(700, () => {
      $('#cine-arrive').className = 'on'; SFX.play('land'); SFX.play('done');
      const r = $('#cine-agent').getBoundingClientRect();
      FX.burst(r.left + r.width / 2, r.top + r.height / 2, 80, ['#9dff57', '#ffd23f', '#4de1ff', '#ffffff', '#ff5fa2'], 11, 0.08);
    });
    this.cue(2900, () => this.dock(false));
  },
  // into the working screen: the new agent's chat opens under the scene, the scene zooms away
  dock(fast) {
    const L = this.L;
    this.timers.forEach(clearTimeout); this.timers = [];
    if (L && L.sid) { clearArrive(); BAY_SEQ = null; openAgent(L.sid); }
    $('#cine').className = 'in out dock' + (fast ? ' fast' : '');
    setTimeout(() => this.end(), fast ? 260 : 820);
  },
  tunP(init) {
    return { a: Math.random() * Math.PI * 2, r: init ? 8 + Math.random() * Math.max(this.W, this.H) * 0.6 : 4 + Math.random() * 30,
      z: Math.random(), c: Math.random() < 0.72 ? '225,240,255' : '150,200,255' };
  },
  travel(tt) {
    const g = this.g, W = this.W, H = this.H, cx = W / 2, cy = H * 0.4, S = this.rearS();
    const now = performance.now();
    const ta = this.mode === 'arrive' ? now - this.at0 : 0;
    const slow = this.mode === 'arrive' ? clamp(ta / 1300, 0, 1) : this.mode === 'fail' ? clamp((now - this.ft0) / 1500, 0, 0.85) : 0;
    g.fillStyle = '#05040f'; g.fillRect(0, 0, W, H);
    const glow = g.createRadialGradient(cx, cy, 0, cx, cy, Math.max(W, H) * 0.55);
    glow.addColorStop(0, this.mode === 'fail' ? 'rgba(255,77,109,.35)' : 'rgba(150,200,255,.30)');
    glow.addColorStop(0.3, this.mode === 'fail' ? 'rgba(120,20,40,.18)' : 'rgba(60,40,140,.18)');
    glow.addColorStop(1, 'rgba(5,4,15,0)');
    g.fillStyle = glow; g.fillRect(0, 0, W, H);
    this.streaks(g, cx, cy, (1 - slow) * 0.045 + 0.0015, 1 - slow, this.mode === 'fail' ? '255,170,180' : null);
    // arrival: the planet ahead grows out of the vanishing point and the ship flies into it
    if (this.mode === 'arrive') {
      const k = clamp(ta / 1800, 0, 1), e = 1 - Math.pow(1 - k, 3), R = 12 + e * Math.min(W, H) * 0.42;
      const pg = g.createRadialGradient(cx - R * 0.35, cy - R * 0.35, R * 0.1, cx, cy, R);
      pg.addColorStop(0, '#ffd9b0'); pg.addColorStop(0.35, '#ff8a3d'); pg.addColorStop(0.75, '#a83a6a'); pg.addColorStop(1, '#2a1050');
      g.save(); g.globalAlpha = Math.min(1, k * 2); g.fillStyle = pg; g.beginPath(); g.arc(cx, cy, R, 0, Math.PI * 2); g.fill();
      g.globalAlpha = Math.min(1, k * 2) * 0.35; g.strokeStyle = '#ffd9b0'; g.lineWidth = 3; g.beginPath(); g.arc(cx, cy, R * 1.04, 0, Math.PI * 2); g.stroke(); g.restore();
    }
    const fade = this.mode === 'arrive' ? 1 - clamp((ta - 700) / 600, 0, 1) : this.mode === 'fail' ? 1 - clamp((now - this.ft0) / 600, 0, 1) : 1;
    if (fade > 0) {
      const sc = (innerWidth <= 860 ? 0.95 : 0.78) * (this.mode === 'arrive' ? 1 - clamp(ta / 1300, 0, 1) * 0.75 : 1);
      const y = H * 0.5 - (this.mode === 'arrive' ? clamp(ta / 1300, 0, 1) * (H * 0.5 - cy) : 0);
      const bob = this.mode === 'arrive' ? 0 : Math.sin(tt / 380) * S * 1.2;
      g.globalAlpha = fade;
      this.rear(g, cx + Math.sin(tt / 520) * S * 1.4, y + bob + (this.mode === 'fail' ? Math.random() * 4 - 2 : 0), S * sc, this.mode === 'fail' ? 0.15 : 1);
      g.globalAlpha = 1;
    }
    this.sparks(g);
    if (this.mode === 'fail' && Math.floor(now / 350) % 2 === 0) { g.fillStyle = 'rgba(255,40,70,.10)'; g.fillRect(0, 0, W, H); }
  },
  frame(t) {
    const g = this.g, W = this.W, H = this.H;
    const LIFT = 1850, JUMP = 3050, cx = W / 2, cy = H * 0.42, S = this.rearS();
    const lt = Math.max(0, t - LIFT) / 1000;
    const zc = lt * lt * 6.2;                         // how far the camera has flown
    g.fillStyle = '#05040f'; g.fillRect(0, 0, W, H);
    // space beyond the door: still at first, streaming once we move, stretched by the jump
    const jk = t >= JUMP ? Math.min(1, (t - JUMP) / 420) : 0;
    const sp = t < LIFT ? 0.0015 : Math.min(0.05, 0.004 + lt * 0.028) + jk * 0.03;
    this.streaks(g, cx, cy, sp, Math.min(1, lt * 0.5) + jk * 1.6);
    this.corridor(g, cx, cy, zc, t);
    // the ship: idling low in the frame, then forward and up, then gone in the jump
    let y = H * 0.66, sc = 1;
    if (t >= LIFT && t < JUMP) { const p = (t - LIFT) / (JUMP - LIFT); y = H * (0.66 - 0.1 * p); sc = 1 - 0.2 * p; }
    if (t >= JUMP) { const p = Math.min(1, (t - JUMP) / 380); y = H * 0.56 + (cy - H * 0.56) * p; sc = 0.8 * (1 - p * 0.94); }
    const shake = t > 420 && t < JUMP ? (Math.random() - 0.5) * (t < LIFT ? 2 : 5) : 0;
    const power = t < 420 ? 0.12 : t < LIFT ? 0.2 + (t - 420) / (LIFT - 420) * 0.55 : 1;
    if (sc > 0.06) this.rear(g, cx + shake, y + shake * 0.5, S * sc, power);
    this.sparks(g);
    if (t > JUMP + 120 && t < JUMP + 700) { const a = 1 - Math.abs((t - JUMP - 380) / 300); g.fillStyle = 'rgba(255,255,255,' + clamp(a, 0, 1).toFixed(2) + ')'; g.fillRect(0, 0, W, H); }
  },
  // a blocky station: hull, panel lines, lit windows, and the hangar mouth on its right side
  station(g, mx, my, S, t, W, H) {
    const right = mx + 2 * S, top = my - H * 0.42, bot = my + H * 0.42, left = right - Math.max(W * 0.34, 46 * S);
    g.fillStyle = '#17142f'; g.fillRect(left, top, right - left, bot - top);
    g.fillStyle = '#221d47';
    for (let yy = top + 3 * S; yy < bot; yy += 7 * S) g.fillRect(left, yy, right - left, Math.max(1, S / 3));
    for (let xx = left + 5 * S; xx < right; xx += 11 * S) g.fillRect(xx, top, Math.max(1, S / 3), bot - top);
    for (let i = 0; i < 26; i++) {
      const wx = left + ((i * 73) % 97) / 97 * (right - left - 4 * S), wy = top + ((i * 41) % 89) / 89 * (bot - top);
      if (Math.abs(wy - my) < 12 * S) continue;
      g.fillStyle = (Math.floor(t / 400) + i) % 7 === 0 ? '#ff8a3d' : '#ffd23f';
      g.globalAlpha = 0.55 + ((i * 13) % 5) / 10; g.fillRect(wx, wy, S, S); g.globalAlpha = 1;
    }
    // the mouth: open shutters, warm light inside, hazard stripes on the lips
    const mh = 22 * S, open = clamp(t / 420, 0, 1), mw = 36 * S;
    g.fillStyle = '#2b1640'; g.fillRect(right - mw, my - mh / 2, mw, mh);
    const glow = g.createLinearGradient(right - mw, 0, right, 0); glow.addColorStop(0, 'rgba(255,138,61,.55)'); glow.addColorStop(1, 'rgba(255,138,61,.05)');
    g.fillStyle = glow; g.fillRect(right - mw, my - mh / 2, mw, mh);
    for (let i = 0; i < 8; i++) { g.fillStyle = (Math.floor(t / 160) + i) % 8 < 2 ? '#ffb547' : '#5a3a20'; g.fillRect(right - mw + i * mw / 8 + S, my + mh / 2 - S * 1.5, S, S * 0.6); }
    const sh = (mh / 2) * (1 - open);
    g.fillStyle = '#3a335f'; g.fillRect(right - mw, my - mh / 2, mw, sh); g.fillRect(right - mw, my + mh / 2 - sh, mw, sh);
    for (let i = 0; i < mw / (2 * S); i++) {
      g.fillStyle = i % 2 ? '#ffd23f' : '#1d1a36';
      g.fillRect(right - mw + i * 2 * S, my - mh / 2 - 2 * S, 2 * S, 2 * S); g.fillRect(right - mw + i * 2 * S, my + mh / 2, 2 * S, 2 * S);
    }
    // beacon lights blinking on the edge
    if (Math.floor(t / 300) % 2 === 0) { g.fillStyle = '#ff4d6d'; g.fillRect(right - S, my - mh / 2 - 4 * S, S * 1.5, S * 1.5); g.fillRect(right - S, my + mh / 2 + 3 * S, S * 1.5, S * 1.5); }
  },
};
$('#cine').addEventListener('click', e => { if (e.target.closest('#cine-fail .cf-btns')) return; CINE.skip(); });
$('#cine-retry').onclick = () => { const L = CINE.L; CINE.end(); if (L) launchRetry(L); };
$('#cine-edit').onclick = () => { CINE.end(); $('#bay-edit').click(); };
$('#cine-close').onclick = () => CINE.end();

// ------------------------------------------------------------------ ship systems
// the owner 10-16: «вот тут хочу по клику открывать попап, и по клику на перегрузку
// будет запускаться сессия по очистке RAM/памяти». The vitals open this window;
// a system running high gets a button that launches a cleanup agent with the
// live numbers and the rules of what it may and may not touch.
const PRESS = { 1: 'normal', 2: 'raised', 4: 'critical' };
function sysLevels(m) {
  m = m || {};
  const pr = m.pressure || 1, used = (m.used_mb || 0) / 1024, tot = (m.mem_mb || 0) / 1024;
  const load = m.load1 || 0, gate = m.load_gate || 20, live = m.live_total || 0, cap = m.cap || 20;
  const free = m.disk_free_gb, dt = m.disk_total_gb;
  return {
    mem: { lvl: pr >= 4 ? 'hot' : pr >= 2 ? 'warm' : 'ok', pct: tot ? used * 100 / tot : 0, used, tot, pr, swap: (m.swap_mb || 0) / 1024 },
    cpu: { lvl: load > gate ? 'hot' : load > gate * 0.6 ? 'warm' : 'ok', pct: load * 100 / gate, load, gate },
    crew: { lvl: live > cap ? 'hot' : live >= cap * 0.85 ? 'warm' : 'ok', pct: live * 100 / cap, live, cap, busy: m.busy || 0 },
    disk: free == null ? null : { lvl: free < 10 ? 'hot' : free < 25 ? 'warm' : 'ok', pct: dt ? (dt - free) * 100 / dt : 0, free, dt },
  };
}
const CLEAN_TITLE = { mem: 'RAM cleanup', disk: 'Disk cleanup', cpu: 'CPU check' };
function cleanupText(k, v, m) {
  const head = 'Task from HQ: the owner pressed "clean up" on an overloaded system of this machine. ';
  const tail = '\n\nReport briefly, 3-5 lines: before -> after, what was done, what is left and what waits for the owner\'s yes.';
  if (k === 'mem') return head + 'MEMORY: ' + v.used.toFixed(1) + ' of ' + Math.round(v.tot) + ' GB used, pressure ' + (PRESS[v.pr] || v.pr)
    + ', swap ' + v.swap.toFixed(1) + ' GB, live sessions ' + (m.live_total || 0) + ' of ' + (m.cap || 0) + '.\n\n'
    + 'Find what holds the memory and free it safely.\n'
    + '1. Facts first: memory_pressure, vm_stat, ps -axo pid,ppid,rss,etime,command sorted by RSS. Split into Claude sessions, Chrome/Playwright, apps, orphans.\n'
    + '2. Remove only orphans and hung processes: dev servers with no clients for over a day, headless Chrome/Playwright without a live parent, hung claude -p, old scripts without a terminal. Check parent, age and command before any kill; kill saved pids only, never by pattern.\n'
    + '3. Do not touch live Claude sessions, the panel, its guardian, launchd services or the owner\'s apps: only report how much they hold.' + tail;
  if (k === 'disk') return head + 'DISK: ' + v.free + ' GB free of ' + v.dt + '. Target: at least 25 GB free.\n\n'
    + '1. Look only at specific folders, not the whole home: du -sh ~/.claude/projects ~/Library/Caches/* ~/.cache ~/.npm /private/tmp.\n'
    + '2. Start with build caches (npm, pip, playwright), logs and temp files older than 3 days.\n'
    + '3. Delete nothing unique without a copy: working repositories, session transcripts and unpublished output stay. Ask before anything else.' + tail;
  return head + 'CPU: load ' + v.load.toFixed(1) + ' against a gate of ' + v.gate + ' (above the gate new sessions wait in a queue).\n\n'
    + '1. top -o cpu and ps: what loads the CPU and how long it has been running.\n'
    + '2. Stop only hung and orphaned work: renders, ffmpeg, headless Chrome, node/python without a live parent or stuck in a loop. Do not touch live Claude sessions or the panel services.' + tail;
}
// one cleanup agent per system at a time: a second click opens the first one
function cleanupOf(k) {
  const c = (LS.get('cleanups', {}) || {})[k];
  if (!c || Date.now() - c.at > 30 * 60000) return null;
  const L = LAUNCHES.find(x => x.nonce === c.nonce);
  if (L && L.state === 'failed') return null;
  return { sid: (L && L.sid) || c.sid || '', flying: !!(L && L.state === 'flying') };
}
function cleanupLanded(L) {
  const all = LS.get('cleanups', {}) || {};
  Object.keys(all).forEach(k => { if (all[k].nonce === L.nonce) all[k].sid = L.sid; });
  LS.set('cleanups', all);
}
function launchTask(text, title) {
  const L = { nonce: newNonce(), text, title, at: Date.now(), state: 'flying', model: LS.get('bayModel', (G && G.model_default) || ''), effort: LS.get('bayEffort', (G && G.effort_default) || '') };
  LAUNCHES.push(L); saveLaunches(); launchCardsDraw(); launchRun(L);
  return L;
}
function cleanupLaunch(k) {
  const m = (G && G.meta) || {}, v = sysLevels(m)[k];
  if (!v || cleanupOf(k)) return;
  const L = launchTask(cleanupText(k, v, m), CLEAN_TITLE[k]);
  const all = LS.get('cleanups', {}) || {}; all[k] = { nonce: L.nonce, at: Date.now() }; LS.set('cleanups', all);
  SFX.play('jump'); FX.rocket();
  hype('launch', 'mid', $('#sys .sysc[data-k="' + k + '"]'));
  toast({ i: 'zap', t: CLEAN_TITLE[k] + ' agent launched', x: 'It lands on the board with a report in a few minutes.', ms: 7000 });
  SYS_HTML = ''; sysDraw();
}
let SYS_K = '', SYS_PROCS = null, SYS_PT = null, SYS_HTML = '';
function leds(pct, n) {
  const on = Math.round(clamp(pct, 0, 100) / 100 * (n || 24));
  let h = '<i class="leds">';
  for (let i = 0; i < (n || 24); i++) h += '<u' + (i < on ? ' class="on"' : '') + '></u>';
  return h + '</i>';
}
// «Google Chrome.app/Contents/Frameworks/…/Helper (Renderer)» reads as «Google Chrome · helper»
function procName(cmd) {
  cmd = String(cmd || '');
  const m = /([^/]+)[.]app[/]Contents[/]/.exec(cmd);
  if (!m) return cmd;
  return m[1] + (/Helper|Framework|--type=/.test(cmd) ? ' · helper' : '');
}
function sysDraw() {
  if (!G || $('#sys').hidden) return;
  const m = G.meta || {}, S = sysLevels(m), P = SYS_PROCS;
  const word = { ok: 'NOMINAL', warm: 'HIGH', hot: 'OVERLOAD' };
  const gb = mb => (mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : Math.round(mb) + ' MB');
  const btn = (k, v, label) => {
    const c = cleanupOf(k);
    if (c) return '<button class="hb wide sysbtn busy" data-open="' + esc(c.sid) + '">' + ico(c.sid ? 'chevron-right' : 'loader', c.sid ? '' : 'spin')
      + (c.sid ? 'CLEANUP AGENT ON IT · OPEN' : 'CLEANUP AGENT LAUNCHING…') + '</button>';
    return '<button class="' + (v.lvl === 'ok' ? 'hb wide' : 'go') + ' sysbtn" data-clean="' + k + '">' + ico('zap') + label + '</button>';
  };
  const card = (k, icon, name, sub, v, val, body, action) => '<div class="sysc ' + v.lvl + (SYS_K === k ? ' focus' : '') + '" data-k="' + k + '">'
    + '<div class="sysh">' + ico(icon) + '<b>' + name + '</b><span>' + sub + '</span><em>' + word[v.lvl] + '</em></div>'
    + '<div class="sysg">' + leds(v.pct) + '<strong>' + val + '</strong></div>' + body + (action || '') + '</div>';
  let top = '';
  if (P && P.procs) {
    const t = P.totals || {};
    top = '<p class="sysp">Sessions ' + gb(t.sessions_mb || 0) + ' · Chrome ' + gb(t.chrome_mb || 0) + ' · other ' + gb(t.other_mb || 0) + ' · ' + (t.count || 0) + ' processes</p>'
      + '<ol class="systop">' + P.procs.slice(0, 6).map(p => '<li><b>' + gb(p.rss_mb || 0) + '</b><span>' + esc(p.uuid ? (p.title || p.uuid.slice(0, 8)) : procName(p.cmd)) + '</span>'
        + (p.uuid ? '<em>agent</em>' : '') + '</li>').join('') + '</ol>';
  } else top = '<p class="sysp">reading the processes…</p>';
  const mem = S.mem, cpu = S.cpu, crew = S.crew, disk = S.disk;
  let h = card('mem', 'zap', 'LIFE SUPPORT', 'memory', mem, mem.used.toFixed(1) + ' / ' + Math.round(mem.tot) + ' GB',
    '<p class="sysp">Pressure <b class="pr' + mem.pr + '">' + (PRESS[mem.pr] || mem.pr).toUpperCase() + '</b> · swap ' + mem.swap.toFixed(1) + ' GB. The kernel pressure is what matters: the file cache fills the rest and gives it back.</p>' + top,
    btn('mem', mem, 'LAUNCH A RAM CLEANUP AGENT'));
  if (disk) h += card('disk', 'archive', 'CARGO HOLD', 'disk', disk, disk.free + ' GB free of ' + disk.dt,
    '<p class="sysp">' + (disk.lvl === 'ok' ? 'Plenty of room.' : disk.lvl === 'warm' ? 'Getting full: renders and builds fill it fast.' : 'Almost full: under 10 GB sessions start failing to save.') + '</p>',
    btn('disk', disk, 'LAUNCH A DISK CLEANUP AGENT'));
  h += card('cpu', 'speed-fast', 'REACTOR', 'CPU load', cpu, cpu.load.toFixed(1) + ' / ' + cpu.gate,
    '<p class="sysp">Above ' + cpu.gate + ' the queue holds new sessions until the load drops.</p>',
    cpu.lvl === 'ok' ? '' : btn('cpu', cpu, 'LAUNCH A CPU CHECK AGENT'));
  h += card('crew', 'users', 'CREW QUARTERS', 'live sessions', crew, crew.live + ' / ' + crew.cap,
    '<p class="sysp">' + crew.busy + ' working right now · ' + ((G.sleeping_n || 0)) + ' asleep in the hangar. Each live agent holds a slot and some memory.</p>',
    '<button class="hb wide sysbtn" data-retire="6">' + ico('moon-stars') + 'RETIRE AGENTS IDLE OVER 6 H</button>');
  if (h === SYS_HTML) return;
  SYS_HTML = h;
  $('#sys-list').innerHTML = h;
  $('#sys-host').textContent = (m.host || '').split('-').slice(0, 3).join('-');
}
async function sysProcs() {
  try { const j = await (await fetchT('/api/procs' + devq('?'), { cache: 'no-store' }, 12000)).json(); if (j.ok) { SYS_PROCS = j; sysDraw(); } } catch (e) {}
}
function showSys(on, k) {
  $('#sys').hidden = !on; SFX.play(on ? 'open-big' : 'close');
  clearInterval(SYS_PT); SYS_PT = null;
  if (!on) return;
  SYS_K = k || ''; SYS_HTML = '';
  sysDraw(); sysProcs(); SYS_PT = setInterval(sysProcs, 6000);
  $('#sys .hbox').scrollTop = 0; $('#sysok').focus({ preventScroll: true });
  if (SYS_K) { const c = $('#sys .sysc[data-k="' + SYS_K + '"]'); if (c) c.scrollIntoView({ block: 'nearest' }); }
}
$('#vitals').addEventListener('click', e => { const vt = e.target.closest('.vt'); showSys(true, vt ? vt.id.slice(3) : ''); });
$('#vitals').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showSys(true); } });
$('#sysok').onclick = () => showSys(false);
$('#sys').addEventListener('click', e => {
  if (e.target.id === 'sys') { showSys(false); return; }
  const c = e.target.closest('[data-clean]');
  if (c) { cleanupLaunch(c.dataset.clean); return; }
  const o = e.target.closest('[data-open]');
  if (o) { if (o.dataset.open) { showSys(false); openAgent(o.dataset.open); } return; }
  const r = e.target.closest('[data-retire]');
  if (r) confirmBox('Retire every agent untouched for over ' + r.dataset.retire + ' hours? The guardian stops restoring them; nothing is deleted and any of them comes back from the classic panel.', 'RETIRE', async ok => {
    if (!ok) return;
    const j = await act('retire_older', +r.dataset.retire);
    if (j.ok) { toast({ i: 'moon-stars', t: 'Crew quarters tidied', x: j.msg || '', ms: 7000 }); setTimeout(poll, 600); }
  });
});

// ------------------------------------------------------------------ hype
// the owner 10-16: «мотивирующие подписи и всплывашки при разных действиях
// прогресса — LET'S FUUCKIN GOOO или просто LFG и другие прикольные фразы».
// Three sizes: a small word by the composer on a send, a medium one over the
// agent when its work lands, the full screen one for the big wins. Settings:
// LOUD (default), CLEAN (no swearing), OFF.
const HYPE = {
  send: ['LFG', 'SEND IT', 'PUNCH IT', 'GO GO GO', "LET'S GO", 'ENGAGE', 'FULL THROTTLE', 'COOK', 'ON IT', 'MAKE IT SO', 'NEVER TELL ME THE ODDS', 'ALL GAS'],
  turn: ['W', 'NICE', 'CLEAN', 'BOOM', 'GG', 'EZ', 'SHEEESH', 'COOKED', 'NAILED IT', 'SMOOTH', 'GREAT SHOT, KID', 'THIS IS THE WAY'],
  loot: ['SHIPPED', 'LOCKED IN', 'BIG W', 'NO CAP', 'CRUSHED IT', 'GOATED', 'BUILT DIFFERENT', 'THIS IS THE WAY', 'STACKING WINS'],
  deploy: ["IT'S LIVE", 'SHIP IT', 'JUMP', "LET'S FUCKING GOOO", 'TO THE STARS', "WE'RE SO BACK", 'LFG'],
  goal: ['ONE DOWN', 'TARGET DOWN', 'BOOM', 'LFG', "GOT 'EM", 'CHECK', 'NEXT'],
  won: ["LET'S FUCKING GOOO", 'DAY WON', 'ABSOLUTE CINEMA', 'UNSTOPPABLE', 'LEGENDARY'],
  level: ['THE FORCE IS STRONG', 'POWER UP', 'GLOW UP', 'UNLIMITED POWER'],
  flow: ['ON FIRE', 'IN THE ZONE', 'RAMPAGE', 'UNSTOPPABLE', 'FLOW STATE', 'COMBO ×2'],
  launch: ['WHEELS UP', 'PUNCH IT, CHEWIE', 'JUMP', 'CREW ASSEMBLE', 'LFG', 'NEVER TELL ME THE ODDS'],
  liftoff: ['PUNCH IT', 'WHEELS UP', 'LFG', 'NEVER TELL ME THE ODDS', "LET'S GOOO", 'FULL THROTTLE', 'PUNCH IT, CHEWIE', "LET'S FUCKING GOOO"],
  quick: ['QUICK DRAW', 'FASTEST GUN', 'SPEEDRUN', 'ZOOM'],
  ach: ['ACHIEVEMENT UNLOCKED', 'LEGEND', 'GOATED', 'HALL OF FAME'],
};
const HYPE_CLEAN = { "LET'S FUCKING GOOO": "LET'S GOOO", 'LFG': "LET'S GO" };
const HYPE_LAST = {};
let HYPE_MODE = LS.get('hype', 'loud');
function hypeWord(kind) {
  const list = HYPE[kind] || HYPE.turn;
  let w = list[Math.floor(Math.random() * list.length)];
  if (w === HYPE_LAST[kind] && list.length > 1) w = list[(list.indexOf(w) + 1) % list.length];
  HYPE_LAST[kind] = w;
  return HYPE_MODE === 'clean' ? (HYPE_CLEAN[w] || w) : w;
}
// size: 'small' by the composer, 'mid' over an element, 'big' across the screen
function hype(kind, size, el, word) {
  if (HYPE_MODE === 'off' || document.hidden) return;
  const w = word || hypeWord(kind);
  // the owner 10-16: «мотивашки слишком быстро, хочу их дольше видеть, анимацию жирнее»
  // each word stays whole (a line may break between words, never inside one)
  const letters = t => { let i = 0; return t.split(' ').map(wd => '<em class="hw">' + [...wd].map(ch => '<span style="--i:' + (i++) + '">' + esc(ch) + '</span>').join('') + '</em>').join(' '); };
  const COLS = ['#ffd23f', '#ff8a3d', '#ff5fa2', '#4de1ff', '#9dff57', '#ffffff'];
  if (size === 'big') {
    const d = document.createElement('div');
    d.className = 'hypebig'; d.innerHTML = '<b>' + letters(w) + '</b>';
    $('#hypebox').appendChild(d);
    SFX.play('hype');
    FX.burst(innerWidth / 2, innerHeight * 0.45, 120, COLS, 14, 0.12);
    setTimeout(() => FX.burst(innerWidth / 2, innerHeight * 0.45, 70, COLS, 10, 0.1), Math.min(1400, [...w].length * 60 + 250));
    document.body.classList.remove('shake'); void document.body.offsetWidth; document.body.classList.add('shake');
    setTimeout(() => d.remove(), 3700);
    return;
  }
  let x = innerWidth / 2, y = innerHeight / 2;
  const r = el && el.getBoundingClientRect ? el.getBoundingClientRect() : null;
  if (r && r.width && r.bottom > 0 && r.top < innerHeight) { x = r.left + r.width / 2; y = r.top + Math.min(r.height / 2, 40); }
  const d = document.createElement('div');
  d.className = 'hype ' + (size || 'mid') + ' h-' + kind;
  d.innerHTML = letters(w);
  d.style.setProperty('--rot', ((Math.random() * 10) - 5).toFixed(1) + 'deg');
  $('#hypebox').appendChild(d);
  // long words near an edge would leave the screen: keep the whole word in view
  const half = d.offsetWidth / 2 + 10;
  d.style.left = (half * 2 >= innerWidth ? innerWidth / 2 : clamp(x, half, innerWidth - half)) + 'px';
  d.style.top = clamp(y, 70, innerHeight - 40) + 'px';
  const bx = parseFloat(d.style.left), by = parseFloat(d.style.top);
  setTimeout(() => FX.burst(bx, by, size === 'small' ? 14 : 30, COLS, size === 'small' ? 4 : 7, 0.1), 160);
  if (size !== 'small') SFX.play('hype');
  setTimeout(() => d.remove(), size === 'small' ? 2100 : 2900);
}
// a send gets a word about half the time, and never twice in 12 s
let HYPE_SEND_AT = 0;
function hypeSend() {
  const t = Date.now();
  if (t - HYPE_SEND_AT < 12000 || Math.random() > 0.55) return;
  HYPE_SEND_AT = t;
  const fr = $('#chat').getBoundingClientRect();
  if (!fr.width) return;
  const d = { getBoundingClientRect: () => ({ left: fr.right - 260, top: fr.bottom - 120, width: 160, height: 40, bottom: fr.bottom - 80 }) };
  hype('send', 'small', d);
}

// ------------------------------------------------------------------ account
// the owner 10-16: «в интерфейсе не хватает переключения на другой аккаунт, как в
// основном». The same flow as the classic Account box: the mini runs
// `claude auth login`, HQ shows its link, passes the code back and watches
// /api/auth until the account changes; the guardian then moves the agents.
let ACC_T = null, ACC_RUN = false, ACC_STEP = 0, ACC_NEW = null, ACC_KEEP = false;
// 20.11: a «keep» sign-in only saves the account for auto-switch, this Mac stays put
function accFlowText(keep) {
  ACC_KEEP = !!keep;
  $('#acc-f3 span').textContent = keep ? 'Saved: the account goes on the auto-switch list, this computer stays on ' + (accName() || 'its account') + ' and nothing restarts'
    : 'Switch: the guardian relaunches the live agents on the new account by itself, it takes a few minutes';
}
// right after a switch the board snapshot can lag a few seconds behind /api/auth:
// hold the new name until the snapshot agrees, so the label does not flip back
function accName() {
  const srv = (G && G.meta && G.meta.account) || '';
  if (ACC_NEW && (srv === ACC_NEW.n || NOW() - ACC_NEW.at > 60)) ACC_NEW = null;
  return ACC_NEW ? ACC_NEW.n : srv;
}
function accShow() {
  const a = accName() || '…';
  $('#acc-now').textContent = a; $('#prof-accn').textContent = a;
  $('#accbtn').title = 'Account: ' + a + ' · switch to another one (A)';
}
function accMsg(t, cls) { const m = $('#acc-msg'); m.hidden = !t; m.textContent = t || ''; m.className = 'accmsg' + (cls ? ' ' + cls : ''); }
function accStep(n) {
  // 0 = no flow, 1 = waiting for the link, 2 = waiting for the code, 3 = switching, 4 = done
  ACC_STEP = n;
  $('#acc-flow').hidden = !n; $('#acc-start').hidden = n > 0 && n < 4; $('#acc-known').hidden = n > 0 && n < 4;
  $('#acc-cancel').hidden = !(n && n < 4);
  [1, 2, 3].forEach(i => {
    const li = $('#acc-f' + i), on = n === i || (n === 2 && i === 1);
    li.classList.toggle('on', on); li.classList.toggle('ok', !on && n > i);
  });
  $('#acc-code').disabled = n !== 2; $('#acc-ok').disabled = n !== 2;
}
// accounts seen on this Mac, so switching back is one tap
function accKnown(...names) {
  let k = LS.get('accs', []);
  names.forEach(n => { if (n && n !== 'unknown' && /@/.test(n) && k.indexOf(n) < 0) k.unshift(n); });
  k = k.slice(0, 6); LS.set('accs', k);
  const cur = accName();
  $('#acc-known').innerHTML = k.filter(n => n !== cur).map(n => '<button class="hb accpick" data-mail="' + esc(n) + '">' + ico('user') + esc(n) + '</button>').join('');
}
function accStop() { clearInterval(ACC_T); ACC_T = null; }
async function accPoll() {
  let s;
  try { s = await (await fetchT('/api/auth' + devq('?'), { cache: 'no-store' }, 8000)).json(); } catch (e) { return; }
  accKnown(s.account, s.before);
  // a flow started earlier, here or on the classic page
  if (s.running && !ACC_RUN) { ACC_RUN = true; accFlowText(s.keep); accStep(s.url ? 2 : 1); }
  // otherwise /api/auth still holds the last finished flow: history, not news
  if (!ACC_RUN) return;
  const a = $('#acc-url');
  if (s.url && a.getAttribute('href') !== s.url) {
    a.setAttribute('href', s.url); a.querySelector('em').textContent = 'OPEN THE SIGN-IN LINK';
    if (ACC_STEP === 1) { accStep(2); SFX.play('comm-open'); }
  }
  if (ACC_KEEP && s.added) {
    accStop(); ACC_RUN = false; accStep(4); LIMS_HTML = '';
    accMsg('Saved: ' + s.added + '. When ' + (s.account || 'the active account') + ' runs out, this computer can move there by itself. Nothing restarted.', 'good');
    SFX.play('level'); toast({ i: 'shield', t: 'Sign-in saved', x: s.added, cls: 'turn', ms: 9000 });
    accFlowText(false);
    return;
  }
  if (s.switched) {
    accStop(); ACC_RUN = false; ACC_NEW = { n: s.account, at: NOW() }; accShow(); accStep(4);
    accMsg('Signed in: ' + s.account + '. The guardian relaunches the live agents on it, give it a few minutes.', 'good');
    SFX.play('level'); toast({ i: 'card-id', t: 'Account switched', x: s.account, cls: 'turn', ms: 9000 });
    accKnown(s.account);
    return;
  }
  if (!s.running) {
    const kept = ACC_KEEP;
    accStop(); ACC_RUN = false; accStep(0); accFlowText(false);
    accMsg((kept ? 'The sign-in ended and nothing was saved.' : 'The sign-in ended and the account did not change.') + (s.tail ? ' Last words: ' + s.tail.slice(-160) : ''), 'bad');
    SFX.play('sad');
    return;
  }
  if (!ACC_T) ACC_T = setInterval(accPoll, 2000);
}
// 05.11 the owner: limits of every account, opened from the power bars. /usage only
// sees the account signed in now; the others keep their last reading.
let LIMS_HTML = '';
function accLims() {
  const box = $('#acc-lims');
  if (!box || !G) return;
  const list = (G.meta && G.meta.accounts) || [];
  const day = { weekday: 'short', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' };
  const resTxt = s => String(s || '').replace(/\s*\([^)]*\)\s*$/, '').replace(' at ', ' ');
  const h = list.length ? list.map(a => {
    const wk = a.week, cur = !!a.current, mail = esc(a.account || '');
    let lvl = 'ok', tag = cur ? 'ACTIVE' : 'STANDBY', tcls = cur ? 'cur' : '', val, note;
    if (wk == null) { val = '—'; tag = 'NO DATA'; tcls = 'na'; note = 'No reading yet: it shows up after this account is signed in once.'; }
    else if (a.week_reset_passed) { val = '0%'; tag = 'RECHARGED'; note = 'Its week has reset since the last reading (' + wk + '% before), so it is full again.'; }
    else {
      val = wk + '%'; lvl = wk >= 85 ? 'hot' : wk >= 65 ? 'warm' : 'ok';
      if (wk >= 100 && !cur) tag = 'EMPTY';
      note = 'Resets ' + resTxt(a.week_resets) + '.';
    }
    if (!cur && a.ts) note += ' Last reading ' + new Date(a.ts * 1000).toLocaleString([], day) + '.';
    if (cur) note += ' Every agent on this computer runs on it now.';
    if (a.empty_until) { tag = 'EMPTY'; lvl = 'hot'; note = 'Out of its limit until ' + new Date(a.empty_until * 1000).toLocaleString([], day) + '. ' + note; }
    const pct = wk == null ? 0 : (a.week_reset_passed ? 0 : wk);
    // 20.11 auto-switch (rc_accounts.py): a peer's older panel sends no `saved`, so nothing shows there
    let save = '', btns = '';
    const sv = a.saved !== undefined, ready = !!(a.saved && !a.saved_bad);
    // an empty account is no place to move to: one tap would restart every agent onto a dead limit
    const empty = !cur && (!!a.empty_until || (wk != null && wk >= 100 && !a.week_reset_passed));
    if (sv) {
      const exp = a.saved_exp ? new Date(a.saved_exp * 1000) : null;
      const left = exp ? (exp - Date.now()) / 864e5 : null;
      const until = exp ? exp.toLocaleDateString([], { day: '2-digit', month: 'short' }) : '';
      if (ready && left != null && left < 4) save = '<p class="syss warn">' + ico('warning-box') + 'Saved sign-in ends ' + until + ': sign in again to keep auto-switch</p>';
      else if (ready) save = '<p class="syss ' + (empty ? 'na' : 'ok') + '">' + ico('shield') + (cur ? 'Sign-in saved' : empty ? 'Sign-in saved, auto-switch waits for its reset' : 'Auto-switch ready') + (until ? ' · valid until ' + until : '') + '</p>';
      else if (a.saved_bad) save = '<p class="syss bad">' + ico('warning-box') + 'Saved sign-in was refused, save it again: ' + esc(a.saved_bad.slice(0, 90)) + '</p>';
      else save = '<p class="syss na">' + ico('shield') + (cur ? 'Sign-in not saved yet: it is, within a minute' : 'No saved sign-in: auto-switch cannot move here') + '</p>';
      if (!cur) {
        if (ready && !empty) btns += '<button class="hb accsw" data-sw="' + mail + '">' + ico('repeat') + 'SWITCH NOW</button>';
        btns += '<button class="hb acckeep" data-keep="' + mail + '">' + ico('shield') + (ready ? 'RENEW SIGN-IN' : 'SAVE SIGN-IN') + '</button>';
      } else if (left != null && left < 4) btns += '<button class="hb accpick" data-mail="' + mail + '">' + ico('login') + 'SIGN IN AGAIN</button>';
    }
    if (!cur && !ready) btns += '<button class="hb accpick" data-mail="' + mail + '">' + ico('login') + 'SWITCH TO IT</button>';
    return '<div class="sysc ' + lvl + (cur ? ' cur' : '') + '"><div class="sysh">' + ico('user') + '<span>' + esc(a.account || '?') + '</span><em class="' + tcls + '">' + tag + '</em></div>'
      + '<div class="sysg">' + leds(pct) + '<strong>WEEK ' + val + '</strong></div><p class="sysp">' + esc(note) + '</p>'
      + save + (btns ? '<div class="sysb">' + btns + '</div>' : '') + '</div>';
  }).join('') : '<p class="sysp">No readings yet.</p>';
  const au = list.find(a => a.auto !== undefined);
  const ah = au ? '<div class="accauto' + (au.auto ? ' on' : '') + '"><div><b>AUTO-SWITCH ' + (au.auto ? 'ON' : 'OFF') + '</b><span>'
      + (au.auto ? 'When the active account runs out, this computer moves to the saved account with the most room, and agents the limit stopped carry on. With none saved, Telegram pings you every 10 min until you sign in.'
        : 'A used-up limit only pings you in Telegram every 10 min; nothing switches by itself.')
      + '</span></div><button class="hb" data-auto="' + (au.auto ? '0' : '1') + '">' + (au.auto ? 'TURN OFF' : 'TURN ON') + '</button></div>' : '';
  if (h + ah === LIMS_HTML) return;
  LIMS_HTML = h + ah; box.innerHTML = h; $('#acc-auto').innerHTML = ah; $('#acc-auto').hidden = !ah;
}
// one tap to a saved account: no browser, no code
function accSwitchNow(mail) {
  confirmBox('Switch this computer to ' + mail + ' now? Its sign-in is saved, so no browser is needed. Live agents are relaunched on it within a few minutes, and the ones the limit stopped are told to carry on.', 'SWITCH', async ok => {
    if (!ok) return;
    accMsg('Switching to ' + mail + '…'); SFX.play('spool');
    const j = await act('acct_switch', { email: mail });
    if (!j.ok) { accMsg(j.msg || 'could not switch', 'bad'); LIMS_HTML = ''; return; }
    ACC_NEW = { n: mail, at: NOW() }; accShow(); LIMS_HTML = '';
    accMsg('Signed in: ' + mail + '. The guardian relaunches the live agents on it, give it a few minutes.', 'good');
    SFX.play('level'); toast({ i: 'card-id', t: 'Account switched', x: mail, cls: 'turn', ms: 9000 });
  });
}
function accKeep(mail) {
  confirmBox('Sign in to ' + mail + ' only to save it for auto-switch? This Mac stays on ' + (accName() || 'its account') + ' and no agent restarts. You open the link and paste the code, as for a normal sign-in.', 'SAVE SIGN-IN', async ok => {
    if (!ok) return;
    $('#acc-url').removeAttribute('href'); $('#acc-url').querySelector('em').textContent = 'waiting for the link…'; $('#acc-code').value = '';
    accMsg('Starting the sign-in for ' + mail + '…');
    const j = await act('auth_start', { email: mail, keep: true });
    if (!j.ok) { accMsg(j.msg || 'could not start the sign-in', 'bad'); return; }
    ACC_RUN = true; accFlowText(true); accStep(1); accMsg('');
    SFX.play('spool');
    accPoll();
  });
}
async function accAuto(on) {
  const j = await act('acct_auto', { on });
  if (j.ok) { toast({ i: 'repeat', t: 'Auto-switch ' + (on ? 'on' : 'off'), x: j.msg || '', ms: 6000 }); }
}
function showAcc(on) {
  $('#acc').hidden = !on; SFX.play(on ? 'open-big' : 'close');
  if (on) {
    const tr = devAuthTrouble(); $('#acc-warn').hidden = !tr; $('#acc-warn').textContent = tr;
    // 17.11: on another machine the box is about THAT machine's account
    $('#acc h2').textContent = HQ_DEV ? 'ACCOUNT · ' + devName().toUpperCase() : 'PILOT ACCOUNT';
    $('#acc .acccur em').textContent = 'Every agent on ' + (HQ_DEV ? devName() : 'this computer') + ' runs on it, and the 5H and WEEK power bars are its limits.';
  }
  if (on) { accShow(); LIMS_HTML = ''; accLims(); $('#acc .hbox').scrollTop = 0; $('#accok').focus({ preventScroll: true }); if (!ACC_RUN) { accStep(0); accMsg(''); } accPoll(); }
  else if (!ACC_RUN) accStop();
}
$('#accbtn').onclick = () => showAcc(true);
$('#power').addEventListener('click', () => showAcc(true));
$('#power').addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showAcc(true); } });
$('#prof-accb').onclick = () => { showProf(false); showAcc(true); };
$('#accok').onclick = () => showAcc(false);
$('#acc').addEventListener('click', e => {
  if (e.target.id === 'acc') { showAcc(false); return; }
  const sw = e.target.closest('[data-sw]');
  if (sw) { accSwitchNow(sw.dataset.sw); return; }
  const kp = e.target.closest('[data-keep]');
  if (kp) { accKeep(kp.dataset.keep); return; }
  const au = e.target.closest('[data-auto]');
  if (au) { accAuto(au.dataset.auto === '1'); return; }
  const pk = e.target.closest('[data-mail]');
  if (pk) { $('#acc-mail').value = pk.dataset.mail; SFX.play('click'); $('#acc-go').focus(); }
});
$('#acc-go').onclick = () => {
  const mail = $('#acc-mail').value.trim();
  confirmBox('Sign in to a different account' + (mail ? ' (' + mail + ')' : '') + '? Once it completes, live agents lose their relay and are relaunched under the new account. This takes a few minutes.', 'SIGN IN', async ok => {
    if (!ok) return;
    $('#acc-url').removeAttribute('href'); $('#acc-url').querySelector('em').textContent = 'waiting for the link…'; $('#acc-code').value = '';
    accMsg('Starting the sign-in on ' + devName() + '…');
    const j = await act('auth_start', { email: mail });
    if (!j.ok) { accMsg(j.msg || 'could not start the sign-in', 'bad'); return; }
    ACC_RUN = true; accFlowText(false); accStep(1); accMsg('');
    SFX.play('spool');
    accPoll();
  });
};
async function accCode() {
  const code = $('#acc-code').value.trim();
  if (!code) { accMsg('Paste the code from the browser first.', 'bad'); $('#acc-code').focus(); return; }
  $('#acc-ok').disabled = true;
  const j = await act('auth_code', { code });
  if (!j.ok) { $('#acc-ok').disabled = false; accMsg(j.msg || 'the code was not taken', 'bad'); return; }
  accStep(3); accMsg(ACC_KEEP ? 'Code sent. Saving the sign-in…' : 'Code sent. Watching for the account to change…');
  SFX.play('send');
}
$('#acc-ok').onclick = accCode;
$('#acc-code').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); accCode(); } });
$('#acc-mail').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); $('#acc-go').click(); } });
$('#acc-cancel').onclick = async () => {
  accStop(); ACC_RUN = false;
  const j = await act('auth_cancel', null);
  accStep(0); accMsg(j.ok ? 'Sign-in cancelled, the account stays as it was.' : '');
};
$('#acc-check').onclick = async () => {
  const pre = $('#acc-rep'); pre.hidden = false; pre.textContent = 'building the report…';
  const j = await act('preflight', null);
  pre.textContent = j.msg || (j.ok ? 'ready' : 'no answer');
};
$('#acc-mig').onclick = () => confirmBox('Migration stops ALL restored sessions, restarts the daemon and relaunches them under the current account. Cards get new links. Continue?', 'MIGRATE', async ok => {
  if (!ok) return;
  const j = await act('migrate', null);
  if (j.ok) toast({ i: 'repeat', t: 'Migrating the sessions', x: j.msg || 'this takes minutes', ms: 9000 });
});

// ------------------------------------------------------------------ settings
function hypeDraw() { $('#hyperow').querySelectorAll('[data-hype]').forEach(b => b.classList.toggle('on', b.dataset.hype === HYPE_MODE)); }
function settDraw() {
  hypeDraw();
  $('#vol').value = Math.round(LS.get('vol', 0.8) * 100); $('#volv').textContent = $('#vol').value + '%';
  $('#mvol').value = Math.round(LS.get('mvol', 0.5) * 100); $('#mvolv').textContent = $('#mvol').value + '%';
  $('#sgrid').innerHTML = SOUND_GROUPS.map(([k, label, ic]) => '<div class="sg' + (SOUNDS[k] === false ? '' : ' on') + '" data-k="' + k + '">'
    + ico(ic) + '<span>' + esc(label) + '</span><button class="hb sgt" data-test="' + k + '" title="Play it">' + ico('play') + '</button>'
    + '<button class="sw" data-sw="' + k + '" aria-label="on or off"><i></i></button></div>').join('');
  $('#sett-ntf').classList.toggle('on', !!NTF);
  $('#sett-cine').classList.toggle('on', LS.get('cine', true) !== false);
  $('#mus2').classList.toggle('on', MUSIC.on); $('#mus2').innerHTML = ico(MUSIC.on ? 'pause' : 'play') + (MUSIC.on ? 'PAUSE' : 'PLAY');
}
function showSett(on) { $('#sett').hidden = !on; SFX.play(on ? 'open-big' : 'close'); if (on) { settDraw(); $('#settok').focus({ preventScroll: true }); $('#sett .hbox').scrollTop = 0; } }
$('#settbtn').onclick = () => showSett(true);
$('#sett-ntf').onclick = () => { $('#ntf').click(); setTimeout(settDraw, 400); };
$('#sett-help').onclick = () => { showSett(false); showHelp(); };
$('#sett-cine').onclick = () => { LS.set('cine', LS.get('cine', true) === false); settDraw(); SFX.play('click'); };
$('#settok').onclick = () => showSett(false);
$('#sett').addEventListener('click', e => {
  if (e.target.id === 'sett') { showSett(false); return; }
  const hy = e.target.closest('[data-hype]');
  if (hy) { HYPE_MODE = hy.dataset.hype; LS.set('hype', HYPE_MODE); hypeDraw(); SFX.play('click'); if (HYPE_MODE !== 'off') hype('send', 'mid', hy, HYPE_MODE === 'clean' ? "LET'S GO" : 'LFG'); return; }
  const sw = e.target.closest('[data-sw]');
  if (sw) { const k = sw.dataset.sw; SOUNDS[k] = SOUNDS[k] === false; LS.set('sounds', SOUNDS); settDraw(); if (SOUNDS[k] !== false) SFX.play((SOUND_GROUPS.find(g => g[0] === k) || [])[3] || 'click'); return; }
  const t = e.target.closest('[data-test]');
  if (t && t.dataset.test === 'score') { const was = SOUNDS.score; SOUNDS.score = true; SCORE.start(); SOUNDS.score = was; setTimeout(() => SCORE.stop(), 5200); return; }
  if (t) { const g = SOUND_GROUPS.find(x => x[0] === t.dataset.test); if (g) { const was = SOUNDS[g[0]]; SOUNDS[g[0]] = true; SFX.play(g[3]); SOUNDS[g[0]] = was; } }
});
$('#vol').addEventListener('input', () => { const v = $('#vol').value / 100; LS.set('vol', v); $('#volv').textContent = $('#vol').value + '%'; if (SFX.mg) SFX.mg.gain.value = v; });
$('#vol').addEventListener('change', () => SFX.play('click'));
$('#mvol').addEventListener('input', () => { const v = $('#mvol').value / 100; LS.set('mvol', v); $('#mvolv').textContent = $('#mvol').value + '%'; MUSIC.setVol(v); });
$('#mus2').onclick = () => { toggleMusic(); settDraw(); };

// ------------------------------------------------------------------ background music
// the owner 10-16: «музыку из space на фон для вайба, спокойную и медитативную».
// John Williams' recordings cannot be shipped in the page, so HQ plays its own
// slow «temple at night» ambient (a drone, modal pads, far bells, space wind),
// all synthesised here; or, if a YouTube link is set in the settings, that
// track instead (an official soundtrack upload, say), looped and quiet.
const MUSIC = {
  on: false, nodes: [], t: null, k: 0, yt: null,
  url() { return LS.get('musicUrl', ''); },
  ytId(u) { const m = /(?:youtu\.be\/|v=|embed\/|shorts\/)([A-Za-z0-9_-]{11})/.exec(u || ''); return m ? m[1] : ''; },
  start() {
    if (this.on) return;
    this.on = true;
    const id = this.ytId(this.url());
    if (id) { this.startYT(id); return; }
    if (!SFX.init()) return;
    const c = SFX.ctx;
    const master = c.createGain(); master.gain.setValueAtTime(0.0001, c.currentTime); master.gain.exponentialRampToValueAtTime(Math.max(0.0002, LS.get('mvol', 0.5)), c.currentTime + 4);
    // a long, dark room: a noise impulse that dies over five seconds
    const len = c.sampleRate * 5, ir = c.createBuffer(2, len, c.sampleRate);
    for (let ch = 0; ch < 2; ch++) { const d = ir.getChannelData(ch); for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3.2); }
    const verb = c.createConvolver(); verb.buffer = ir;
    const wet = c.createGain(); wet.gain.value = 0.7; const dry = c.createGain(); dry.gain.value = 0.35;
    const bus = c.createGain(); bus.connect(verb); bus.connect(dry); verb.connect(wet); wet.connect(master); dry.connect(master); master.connect(c.destination);
    this.master = master; this.bus = bus;
    // the drone: D and A, slowly breathing
    [[73.42, 'triangle', 0.05], [110, 'sine', 0.035], [146.83, 'sine', 0.012]].forEach(([f, ty, v], i) => {
      const o = c.createOscillator(), g = c.createGain(), lfo = c.createOscillator(), lg = c.createGain();
      o.type = ty; o.frequency.value = f; g.gain.value = v;
      lfo.frequency.value = 0.05 + i * 0.03; lg.gain.value = v * 0.6; lfo.connect(lg); lg.connect(g.gain);
      o.connect(g); g.connect(bus); o.start(); lfo.start(); this.nodes.push(o, lfo);
    });
    // space wind
    const src = c.createBufferSource(); src.buffer = SFX.noise(); src.loop = true;
    const bp = c.createBiquadFilter(); bp.type = 'bandpass'; bp.Q.value = 0.7; bp.frequency.value = 500;
    const wl = c.createOscillator(), wlg = c.createGain(); wl.frequency.value = 0.04; wlg.gain.value = 350; wl.connect(wlg); wlg.connect(bp.frequency);
    const wg = c.createGain(); wg.gain.value = 0.018;
    src.connect(bp); bp.connect(wg); wg.connect(bus); src.start(); wl.start(); this.nodes.push(src, wl);
    this.k = 0; this.chord(); this.t = setInterval(() => this.chord(), 9500);
    this.bt = setInterval(() => { if (Math.random() < 0.6) this.bell(); }, 3800);
  },
  // D dorian with a lydian lift: Dm9, Bbmaj7, Gm9, Asus
  CH: [[146.83, 174.61, 220, 261.63, 329.63], [116.54, 146.83, 174.61, 220, 349.23], [98, 116.54, 146.83, 174.61, 220], [110, 146.83, 164.81, 220, 293.66]],
  chord() {
    if (!this.on || !SFX.ctx) return;
    const c = SFX.ctx, t = c.currentTime, notes = this.CH[this.k++ % this.CH.length];
    notes.forEach((f, i) => {
      [1, 1.004].forEach(det => {
        const o = c.createOscillator(), lp = c.createBiquadFilter(), g = c.createGain(), pan = c.createStereoPanner ? c.createStereoPanner() : null;
        o.type = 'sawtooth'; o.frequency.value = f * det;
        lp.type = 'lowpass'; lp.frequency.setValueAtTime(380, t); lp.frequency.linearRampToValueAtTime(900, t + 4); lp.frequency.linearRampToValueAtTime(420, t + 10);
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.008, t + 3.5); g.gain.exponentialRampToValueAtTime(0.0001, t + 11.5);
        o.connect(lp); lp.connect(g);
        if (pan) { pan.pan.value = (i - 2) * 0.25; g.connect(pan); pan.connect(this.bus); } else g.connect(this.bus);
        o.start(t); o.stop(t + 12);
      });
    });
  },
  bell() {
    if (!this.on || !SFX.ctx) return;
    const c = SFX.ctx, t = c.currentTime, scale = [587.33, 659.25, 698.46, 880, 987.77, 1174.66];
    const f = scale[Math.floor(Math.random() * scale.length)];
    [[f, 0.022], [f * 2.01, 0.006]].forEach(([fq, v]) => {
      const o = c.createOscillator(), g = c.createGain(); o.type = 'sine'; o.frequency.value = fq;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(v, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 4.5);
      o.connect(g); g.connect(this.bus); o.start(t); o.stop(t + 4.6);
    });
  },
  startYT(id) {
    const f = document.createElement('iframe');
    f.id = 'ytmusic'; f.allow = 'autoplay'; f.title = 'soundtrack';
    f.src = 'https://www.youtube.com/embed/' + id + '?autoplay=1&loop=1&playlist=' + id + '&controls=0&enablejsapi=1';
    f.style.cssText = 'position:fixed;width:1px;height:1px;left:-10px;bottom:0;opacity:0;pointer-events:none';
    f.onload = () => { try { f.contentWindow.postMessage(JSON.stringify({ event: 'command', func: 'setVolume', args: [Math.round(LS.get('mvol', 0.5) * 40)] }), '*'); } catch (e) {} };
    document.body.appendChild(f); this.yt = f;
  },
  setVol(v) {
    if (this.master && SFX.ctx) { const c = SFX.ctx; this.master.gain.cancelScheduledValues(c.currentTime); this.master.gain.setTargetAtTime(Math.max(0.0002, v), c.currentTime, 0.2); }
    if (this.yt) { try { this.yt.contentWindow.postMessage(JSON.stringify({ event: 'command', func: 'setVolume', args: [Math.round(v * 40)] }), '*'); } catch (e) {} }
  },
  stop() {
    if (!this.on) return;
    this.on = false;
    clearInterval(this.t); clearInterval(this.bt);
    if (this.yt) { this.yt.remove(); this.yt = null; }
    if (this.master && SFX.ctx) {
      const c = SFX.ctx, m = this.master, ns = this.nodes;
      m.gain.cancelScheduledValues(c.currentTime); m.gain.setValueAtTime(m.gain.value, c.currentTime); m.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + 1.5);
      setTimeout(() => { ns.forEach(n => { try { n.stop(); } catch (e) {} }); try { m.disconnect(); } catch (e) {} }, 1700);
    }
    this.nodes = []; this.master = null;
  },
};
function musicBtn() { const b = $('#mus'); b.classList.toggle('on', MUSIC.on); b.title = MUSIC.on ? 'Music on (B)' : 'Background music (B)'; }
function toggleMusic() { if (MUSIC.on) { MUSIC.stop(); LS.set('music', false); } else { MUSIC.start(); LS.set('music', true); } musicBtn(); }
$('#mus').onclick = toggleMusic;
// browsers start sound only after a gesture: resume the music on the first one
if (LS.get('music', false)) document.addEventListener('pointerdown', () => { if (!MUSIC.on) { MUSIC.start(); musicBtn(); } }, { once: true });
$('#musurl').value = LS.get('musicUrl', '');
$('#musurl').addEventListener('change', () => {
  LS.set('musicUrl', $('#musurl').value.trim());
  if (MUSIC.on) { MUSIC.stop(); setTimeout(() => { MUSIC.start(); musicBtn(); }, 1800); }
});

// ------------------------------------------------------------------ fleet
// the owner 10-18: «экран со всеми видами сессий, чтобы на одном экране они
// помещались и я видел процесс по каждой сессии». Every session at once: a
// tile per live agent with the prompt it works on, its last steps and a
// 30-minute activity strip, sized so the whole fleet fits the screen, and the
// hangar along the bottom. G opens it, a tile opens that chat, Esc comes back.
const STEP_ICO = { run: 'command', edit: 'edit', read: 'eye', find: 'eye', web: 'radio-tower', agent: 'users',
  skill: 'lightbulb', ask: 'question', art: 'rocket', plan: 'table', mcp: 'server', tool: 'sliders',
  a: 'chat', u: 'message-arrow-right', wake: 'notification', sys: 'sync', stop: 'debug-stop' };
const LANE_TAG = { ask: 'ASKING YOU', fresh: 'YOUR MOVE', busy: 'ON A MISSION', idle: 'STANDBY' };
function clk(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
}
function showFleet(on, quiet) {
  on = on === undefined ? !FLEET.on : !!on;
  if (on === FLEET.on) return;
  FLEET.on = on;
  LS.set('fleet', on);
  $('#fleet').hidden = !on;
  document.body.classList.toggle('fleet', on);
  $('#fleetbtn').classList.toggle('on', on);
  if (!quiet) SFX.play(on ? 'open-big' : 'close');
  clearInterval(FLEET.tick);
  if (!on) return;
  FLEET.ret = false;
  FLEET.v = 0;              // the next poll brings every trail
  FLEET.n = -1;
  if (G) flDraw();
  FLEET.tick = setInterval(flTick, 1000);
  poll();
}
// a chat or the launch bay opened from the fleet takes the screen; Esc brings
// the fleet back, and a reload lands on it again
function flAway() {
  if (!FLEET.on) return;
  showFleet(false, true);
  LS.set('fleet', true);
  FLEET.ret = true;
}
function flMerge(f) {
  if (!f.since) { FLEET.tr = {}; FLEET.v = f.v || 0; }
  else FLEET.v = Math.max(FLEET.v, f.v || 0);
  Object.keys(f.s || {}).forEach(k => { FLEET.tr[k] = f.s[k]; });
}
function flDraw() {
  if (!FLEET.on || !G) return;
  const counts = { ask: 0, fresh: 0, busy: 0, idle: 0 };
  ORDER.forEach(s => { if (STATE[s] in counts) counts[STATE[s]]++; });
  if (FLEET.filter && !counts[FLEET.filter]) FLEET.filter = '';
  flSum(counts);
  const list = ORDER.filter(s => !FLEET.filter || STATE[s] === FLEET.filter);
  const grid = $('#fl-grid'), used = new Set();
  list.forEach((sid, i) => {
    let el = FLEET.tiles.get(sid);
    if (!el) { el = flMake(sid); FLEET.tiles.set(sid, el); }
    used.add(sid);
    flFill(el, sid, ORDER.indexOf(sid) + 1);
    if (grid.children[i] !== el) grid.insertBefore(el, grid.children[i] || null);
  });
  FLEET.tiles.forEach((el, sid) => { if (!used.has(sid)) { el.remove(); FLEET.tiles.delete(sid); } });
  $('#fl-empty').hidden = list.length > 0;
  grid.hidden = !list.length;
  if (FLEET.n !== list.length) { FLEET.n = list.length; flFit(); }
  flHangar();
}
function flSum(counts) {
  const h = ['ask', 'fresh', 'busy', 'idle'].map(k => '<button class="fs k-' + k + (FLEET.filter === k ? ' on' : '') + (counts[k] ? '' : ' zero')
    + '" data-k="' + k + '" title="' + (FLEET.filter === k ? 'Show every session again' : 'Show only these') + '"><i class="led"></i>'
    + LANE_TAG[k] + '<b>' + counts[k] + '</b></button>').join('');
  const box = $('#fl-sum');
  if (box.dataset.h !== h) { box.innerHTML = h; box.dataset.h = h; }
}
// as many tiles as there are agents, as big as the screen allows: the column
// count that gives the largest tile near a 1.9 aspect (a step is a line of
// text: width buys more than height). Tiles stay 250 px wide where they can,
// 220 on a small laptop; below 100 px of height the grid scrolls instead.
function flFit() {
  const grid = $('#fl-grid'), n = FLEET.n;
  if (n <= 0) return;
  const cs = getComputedStyle(grid), gap = parseFloat(cs.columnGap) || 10;
  const W = grid.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
  const H = grid.clientHeight - (parseFloat(cs.paddingTop) || 0) - (parseFloat(cs.paddingBottom) || 0);
  let best = null;
  for (const minW of [250, 220]) {
    if (best || innerWidth <= 860 || W <= 0 || H <= 0) break;
    for (let c = 1; c <= n; c++) {
      const r = Math.ceil(n / c), w = (W - gap * (c - 1)) / c, h = (H - gap * (r - 1)) / r;
      if (c > 1 && w < minW) break;
      if (h < 100) continue;
      const score = Math.min(w, 640) * Math.min(h, 420) * Math.exp(-Math.abs(Math.log(w / h / 1.9)) * 0.8);
      if (!best || score > best.score) best = { c, r, score };
    }
  }
  grid.classList.toggle('scroll', !best);
  if (best) { grid.style.setProperty('--c', best.c); grid.style.setProperty('--r', best.r); }
  else grid.style.setProperty('--c', innerWidth > 860 ? Math.max(1, Math.floor((W + gap) / (300 + gap))) : 1);
}
function flMake(sid) {
  const el = document.createElement('article');
  el.className = 'ft';
  el.tabIndex = 0;
  el.dataset.sid = sid;
  el.innerHTML = '<header class="ft-h"><div class="ft-pix"><canvas width="16" height="16"></canvas></div>'
    + '<div class="ft-id"><div class="ft-proj"><i></i><span></span></div><b class="ft-name"></b></div>'
    + '<div class="ft-badge"><span class="ft-st"></span><b class="ft-clock"></b></div><span class="ft-key"></span></header>'
    + '<div class="ft-task"></div><div class="ft-body"><div class="ft-note" hidden></div><ol class="ft-steps"></ol></div>'
    + '<footer class="ft-f"><div class="ft-spark"></div><span class="ft-n"></span><span class="ft-ctx"><i></i></span></footer>';
  el.addEventListener('click', () => openAgent(sid));
  el.addEventListener('keydown', ev => { if (ev.key === 'Enter') openAgent(sid); });
  return el;
}
function flClock(r, a, lane) {
  if (lane === 'busy') { const t0 = (a && (a.turn_start || a.busy_since)) || 0; return t0 ? clk(NOW() - t0) : ''; }
  const ts = lane === 'fresh' ? doneTs(r, a) : r.la_ts;
  return ts ? ago(ts) + ' ago' : '';
}
function flTask(r, tr) {
  const st = (tr && tr.st) || [];
  for (let i = st.length - 1; i >= 0; i--) if (st[i][1] === 'u') return st[i][2];
  // a digest from before DIGEST_V 4 can still hold a compaction's summary
  return /^This session is being continued/.test(r.last_u || '') ? '' : r.last_u || '';
}
function flFill(el, sid, key) {
  const r = rowBy(sid);
  if (!r) return;
  const a = agentBy(sid), lane = STATE[sid] || laneOf(r, a), tr = FLEET.tr[sid] || null;
  el.className = 'ft st-' + lane + (sid === CUR ? ' sel' : '') + (lane === 'fresh' && doneTs(r, a) > (SEEN[sid] || 0) ? ' new' : '');
  const set = (sel, txt) => { const n = el.querySelector(sel); if (n.textContent !== txt) n.textContent = txt; };
  set('.ft-name', titleOf(r));
  const pj = projOf(r), pe = el.querySelector('.ft-proj');
  pe.hidden = !pj;
  if (pj) { set('.ft-proj span', pj); pe.style.setProperty('--pc', projColor(pj)); }
  set('.ft-key', key <= 9 ? String(key) : '');
  const stn = el.querySelector('.ft-st');
  const sth = ico(statusIcon(r, lane), lane === 'busy' ? 'spin' : '') + '<span>' + LANE_TAG[lane] + '</span>';
  if (stn.dataset.h !== sth) { stn.innerHTML = sth; stn.dataset.h = sth; }
  set('.ft-clock', flClock(r, a, lane));
  const task = flTask(r, tr);
  set('.ft-task', task);
  el.querySelector('.ft-task').title = task;
  const note = el.querySelector('.ft-note');
  const nh = lane === 'ask' ? '<b>' + ico('question') + 'QUESTION FOR YOU</b>' + esc(r.waiting || 'A question menu is open in the chat')
    : lane === 'fresh' && r.last_a ? '<b>' + ico('check') + 'RESULT</b>' + esc(r.last_a) : '';
  if (note.dataset.h !== nh) { note.innerHTML = nh; note.dataset.h = nh; note.hidden = !nh; }
  flSteps(el.querySelector('.ft-steps'), tr, lane);
  flSpark(el.querySelector('.ft-spark'), tr);
  set('.ft-n', tr && tr.n ? tr.n + (tr.n === 1 ? ' STEP' : ' STEPS') : '');
  const pc = r.ctx_max ? Math.round(r.ctx * 100 / r.ctx_max) : 0, cx = el.querySelector('.ft-ctx');
  cx.hidden = !r.ctx;
  cx.className = 'ft-ctx' + (pc >= 80 ? ' hot' : pc >= 60 ? ' warm' : '');
  cx.firstChild.style.width = pc + '%';
  cx.title = 'context ' + tok(r.ctx) + ' / ' + tok(r.ctx_max) + ' (' + pc + '%)';
  el.title = titleOf(r) + ' · ' + modelLabel(r) + (pj ? ' · ' + pj : '') + ' · click to open the chat';
  SPRITES.set(el.querySelector('canvas'), { sp: specFor(sid, a ? a.lv : 1), st: spriteState(lane) });
}
// the trail: what came after the prompt, newest at the bottom like a terminal;
// steps from before it stay dim above
function flSteps(ol, tr, lane) {
  const st = (tr && tr.st) || [];
  const sig = lane + '|' + JSON.stringify(st);
  if (ol.dataset.sig === sig) return;
  ol.dataset.sig = sig;
  let cut = -1;
  for (let i = st.length - 1; i >= 0; i--) if (st[i][1] === 'u') { cut = i; break; }
  const last = st.length - 1;
  ol.innerHTML = st.map((x, i) => i === cut ? '' : '<li class="k-' + x[1] + (i < cut ? ' old' : '') + (i === last && lane === 'busy' ? ' now' : '') + '">'
    + ico(STEP_ICO[x[1]] || 'sliders') + '<span>' + esc(x[2]) + '</span><time data-ts="' + x[0] + '">' + ago(x[0]) + '</time></li>').join('')
    || '<li class="none"><span>' + (lane === 'busy' ? 'warming up…' : 'no steps seen yet') + '</span></li>';
}
// 30 bars, a minute each: how hard the agent worked (6+ steps a minute is a full bar)
function flSpark(div, tr) {
  const cm = Math.floor(NOW() / 60), h = tr && tr.h;
  const key = cm + '|' + (h ? h[0] + h[1] : '');
  if (div.dataset.k === key) return;
  div.dataset.k = key;
  let out = '';
  for (let i = 0; i < 30; i++) {
    const m = cm - 29 + i, idx = h ? 29 - (h[0] - m) : -1;
    const c = idx >= 0 && idx < 30 ? parseInt(h[1].charAt(idx), 36) || 0 : 0;
    out += c ? '<i style="--v:' + Math.min(1, c / 6).toFixed(2) + '"></i>' : '<i class="z"></i>';
  }
  div.innerHTML = out;
  div.title = 'Activity over the last 30 minutes, a bar a minute';
}
function flTick() {
  if (!FLEET.on || !G) return;
  FLEET.tiles.forEach((el, sid) => {
    const r = rowBy(sid);
    if (!r) return;
    const c = el.querySelector('.ft-clock'), t = flClock(r, agentBy(sid), STATE[sid]);
    if (c.textContent !== t) c.textContent = t;
  });
  $('#fl-grid').querySelectorAll('time[data-ts]').forEach(t => { const v = ago(+t.dataset.ts); if (t.textContent !== v) t.textContent = v; });
  const m = Math.floor(NOW() / 60);
  if (m !== FLEET.min) { FLEET.min = m; FLEET.tiles.forEach((el, sid) => flSpark(el.querySelector('.ft-spark'), FLEET.tr[sid])); flHangar(); }
}
function flHangar() {
  const sl = G.sleeping || [];
  const chips = sl.map(r => '<button class="fh" data-sid="' + esc(r.short) + '" title="' + esc(titleOf(r)) + ' · last seen ' + esc(ago(r.la_ts)) + ' ago · click to open">'
    + ico('moon') + '<span>' + esc(titleOf(r)) + '</span><em>' + esc(ago(r.la_ts)) + '</em></button>').join('')
    + (G.sleeping_n > sl.length ? '<span class="fh-more">+' + (G.sleeping_n - sl.length) + '</span>' : '');
  const h = '<span class="fh-l">' + ico('moon') + 'HANGAR <b>' + (G.sleeping_n || 0) + '</b></span><div class="fh-list">' + chips + '</div>'
    + (G.app_n ? '<span class="fh-x" title="Chats started in the Claude app: they live on the app side">' + ico('chat') + G.app_n + ' app chat' + (G.app_n === 1 ? '' : 's') + '</span>' : '')
    + '<a class="fh-x" href="/" title="The archive lives in the classic panel (Retired): bring any of them back from there">' + ico('archive') + (G.retired_n || 0) + ' archived</a>';
  const box = $('#fl-hangar');
  if (box.dataset.h !== h) { box.innerHTML = h; box.dataset.h = h; }
}
$('#fleetbtn').onclick = () => showFleet();
$('#fl-close').onclick = () => showFleet(false);
$('#fl-recruit').onclick = () => openRecruit();
$('#fl-sum').onclick = e => {
  const b = e.target.closest('.fs');
  if (!b) return;
  FLEET.filter = FLEET.filter === b.dataset.k ? '' : b.dataset.k;
  SFX.play('click');
  flDraw();
};
$('#fl-hangar').onclick = e => { const b = e.target.closest('.fh'); if (b) openAgent(b.dataset.sid); };
// the fleet sits under the HUD, which wraps to three rows on a phone
function hudReal() { document.documentElement.style.setProperty('--hud-real', $('#hud').offsetHeight + 'px'); }
hudReal();
if (window.ResizeObserver) {
  new ResizeObserver(() => { if (FLEET.on) flFit(); }).observe($('#fl-grid'));
  new ResizeObserver(hudReal).observe($('#hud'));
} else window.addEventListener('resize', () => { hudReal(); if (FLEET.on) flFit(); });

// ------------------------------------------------------------------ boot
FX.init();
SPRITES.set($('#me'), { cmdr: true });
SPRITES.set($('#emptypix'), { sp: specFor('pilot', 1), st: 'idle' });
fillIcons();
$('#snd').innerHTML = ico(SND ? 'volume-3' : 'volume-x');
$('#snd').classList.toggle('off', !SND);
ntfBtn();
(function () {
  const h = (location.hash || '').replace(/^#/, '');
  poll().then(() => {
    if (h === 'new') openRecruit();
    // 20.11: the Telegram «out of limit» alarm links to /play#acc, one tap to the account box
    else if (h === 'acc') { history.replaceState(null, '', location.pathname + location.search); showAcc(true); }
    else if (h === 'fleet' || (!h && LS.get('fleet', false))) showFleet(true, true);
    else if (h) openAgent(h);
    // the help is a keyboard-shortcut sheet: useless on a phone (11-17)
    if (!LS.get('helped', false) && !matchMedia('(hover:none) and (pointer:coarse)').matches) showHelp();
  });
})();
window.addEventListener('hashchange', () => {
  const h = (location.hash || '').replace(/^#/, '');
  if (h === 'fleet') showFleet(true);
  else if (h && h !== CUR) openAgent(h);
});
