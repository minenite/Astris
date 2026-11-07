// Browser → dedicated Minecraft/Horizon over same-origin WebSocket↔TCP
// (Astris /minecraft-tcp). Avoids a second port (CSP + flaky sidecar).

const HIGH_WATER = 4 << 20;
const CHUNK = 64 << 10;

function proxyBase() {
  if (typeof globalThis.ASTRIS_WS_PROXY === 'string' && globalThis.ASTRIS_WS_PROXY) {
    return globalThis.ASTRIS_WS_PROXY.replace(/\/$/, '');
  }
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/minecraft-tcp`;
}

let engine = null;
let log = () => {};
const active = new Map();
const dialing = new Set();
/** id → last failed attempt ms, so we retry without thrashing */
const backoffUntil = new Map();

function carry(ws, id) {
  ws.binaryType = 'arraybuffer';
  active.set(id, ws);
  ws.onmessage = event => {
    if (!engine.horizon_ws_push(id, new Uint8Array(event.data))) ws.close();
  };
  const pump = setInterval(() => {
    while (ws.readyState === WebSocket.OPEN && ws.bufferedAmount < HIGH_WATER) {
      const bytes = engine.horizon_ws_take(id, CHUNK);
      if (bytes === undefined) {
        ws.close();
        break;
      }
      if (!bytes.length) break;
      ws.send(bytes);
    }
  }, 4);
  const finish = () => {
    clearInterval(pump);
    active.delete(id);
    dialing.delete(id);
    engine.horizon_ws_close(id);
    log('HOST', `ws bridge ${id} closed`);
  };
  ws.onclose = finish;
  ws.onerror = () => log('WARN', `ws bridge ${id} socket error`);
}

function dial(id, address) {
  if (active.has(id) || dialing.has(id)) return;
  const waitUntil = backoffUntil.get(id) || 0;
  if (Date.now() < waitUntil) return;
  dialing.add(id);
  const url = `${proxyBase()}?target=${encodeURIComponent(address)}`;
  log('HOST', `ws bridge ${id} → ${address} via ${url}`);
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (error) {
    dialing.delete(id);
    backoffUntil.set(id, Date.now() + 500);
    log('WARN', `ws bridge ${id} open failed: ${error.message}`);
    return;
  }
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    dialing.delete(id);
    backoffUntil.delete(id);
    engine.horizon_ws_mark_open(id);
    carry(ws, id);
    log('HOST', `ws bridge ${id} open`);
  };
  ws.onerror = () => {
    log('WARN', `ws bridge ${id} error (Astris /minecraft-tcp)`);
  };
  // Do NOT horizon_ws_close on failed dial — Rust is still waiting; retry.
  ws.onclose = () => {
    if (active.has(id)) return;
    dialing.delete(id);
    backoffUntil.set(id, Date.now() + 400);
    log('HOST', `ws bridge ${id} dial failed; retrying`);
  };
}

export function attach(module, logger) {
  engine = module;
  log = logger;
  setInterval(() => {
    if (!engine?.horizon_ws_pending_id) return;
    const id = engine.horizon_ws_pending_id();
    if (!id) return;
    const address = engine.horizon_ws_pending_addr();
    if (!address) return;
    dial(id, address);
  }, 40);
}
