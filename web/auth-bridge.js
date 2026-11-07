// Microsoft account for Astris: mint a Horizon session via the local dev server
// (Prism / Minenite / device-code), hand it to WASM, and proxy session/join
// (browsers have no Minecraft HTTPS sockets from Rust ureq).

let log = () => {};

/** Sync XHR so Rust's login thread can block on join like native ureq. */
export function installJoinProxy() {
  globalThis.horizonAuthJoin = body => {
    try {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/auth/join', false);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.send(typeof body === 'string' ? body : JSON.stringify(body));
      if (xhr.status >= 200 && xhr.status < 300) return '';
      return `${xhr.status}:${xhr.responseText || ''}`;
    } catch (error) {
      return `0:${error.message || 'join proxy failed'}`;
    }
  };
}

async function fetchJson(url, init) {
  const res = await fetch(url, init);
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text }; }
  return { ok: res.ok, status: res.status, data };
}

async function deviceSignIn() {
  const start = await fetchJson('/auth/device/start');
  if (!start.ok) throw new Error(start.data.error || 'device start failed');
  const { user_code: code, verify_url: url, interval } = start.data;
  log('HOST', `Microsoft sign-in: open ${url} and enter ${code}`);
  notice(`Sign in: ${url} → code ${code}`);
  const deadline = Date.now() + 15 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, Math.max(3000, (interval || 5) * 1000)));
    const poll = await fetchJson('/auth/device/poll');
    if (!poll.ok) throw new Error(poll.data.error || 'device poll failed');
    if (poll.data.status === 'ready') return poll.data.session;
    if (poll.data.status === 'pending') continue;
    if (poll.data.status === 'expired') throw new Error('Sign-in code expired');
    if (poll.data.status === 'error') throw new Error(poll.data.message || 'sign-in failed');
  }
  throw new Error('Sign-in timed out');
}

function notice(message) {
  try { window.horizonNotice?.(message); } catch { /* boot */ }
  const toast = document.querySelector('#toast');
  if (toast) {
    toast.textContent = message;
    toast.hidden = false;
  }
}

/** Load MSA into the WASM client. Returns the player name or null. */
export async function attachAccount(module, logger) {
  log = logger || log;
  installJoinProxy();
  if (!module?.horizon_auth_set) {
    log('WARN', 'horizon_auth_set missing (rebuild wasm)');
    return null;
  }
  let session;
  const minted = await fetchJson('/auth/session');
  if (minted.ok && minted.data.access_token) {
    session = minted.data;
  } else {
    log('HOST', minted.data.error || 'No saved MSA; starting device sign-in');
    notice(minted.data.error || 'Microsoft sign-in required for online servers');
    session = await deviceSignIn();
  }
  const payload = JSON.stringify({
    name: session.name,
    uuid: session.uuid,
    access_token: session.access_token,
    user_type: 'msa',
    xuid: session.xuid || undefined,
  });
  const err = module.horizon_auth_set(payload);
  if (err) {
    log('WARN', `auth set failed: ${err}`);
    return null;
  }
  log('HOST', `Signed in as ${session.name} (${session.source || 'msa'})`);
  notice(`Signed in as ${session.name}`);
  return session.name;
}
