//! Browser → dedicated Horizon server over a local WebSocket↔TCP proxy.
//!
//! Browsers have no TCP. The page opens `ws://127.0.0.1:8768/?target=HOST:PORT`
//! (dev_server / minecraft_ws_proxy.py) and moves binary frames through the same
//! style of in-memory [`Pipe`] as [`crate::peer`]. The game joins `ws:<id>`.

use horizon_protocol::connection::Pipe;
use std::{
    collections::HashMap,
    sync::{
        Arc, Mutex, MutexGuard,
        atomic::{AtomicBool, AtomicU32, Ordering},
    },
};

/// Address prefix: `ws:<id>`.
pub const PREFIX: &str = "ws:";

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    loop {
        match mutex.try_lock() {
            Ok(guard) => return guard,
            Err(std::sync::TryLockError::Poisoned(error)) => return error.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => std::hint::spin_loop(),
        }
    }
}

struct Pending {
    address: String,
    open: AtomicBool,
}

static BRIDGES: Mutex<Option<HashMap<u32, Pipe>>> = Mutex::new(None);
static DIALED: Mutex<Option<HashMap<u32, Pipe>>> = Mutex::new(None);
static PENDING: Mutex<Option<HashMap<u32, Arc<Pending>>>> = Mutex::new(None);
static NEXT: AtomicU32 = AtomicU32::new(1);

/// Start a bridge for `address` (`host` or `host:port`). Returns the connection id.
pub fn begin(address: &str) -> u32 {
    let (page, game) = Pipe::remote_pair();
    let id = NEXT.fetch_add(1, Ordering::Relaxed);
    lock(&BRIDGES)
        .get_or_insert_with(HashMap::new)
        .insert(id, page);
    lock(&DIALED)
        .get_or_insert_with(HashMap::new)
        .insert(id, game);
    lock(&PENDING).get_or_insert_with(HashMap::new).insert(
        id,
        Arc::new(Pending {
            address: address.to_owned(),
            open: AtomicBool::new(false),
        }),
    );
    log::info!("Horizon: WebSocket bridge {id} requested for {address}");
    id
}

/// Oldest bridge that is not open yet, for the page to dial.
pub fn pending() -> Option<(u32, String)> {
    let g = lock(&PENDING);
    let map = g.as_ref()?;
    map.iter()
        .find(|(_, p)| !p.open.load(Ordering::Relaxed))
        .map(|(id, p)| (*id, p.address.clone()))
}

/// The page's WebSocket reached OPEN.
pub fn mark_open(id: u32) {
    if let Some(p) = lock(&PENDING).as_ref().and_then(|m| m.get(&id)) {
        p.open.store(true, Ordering::Relaxed);
        log::info!("Horizon: WebSocket bridge {id} open");
    }
}

/// Whether the page has marked this bridge open.
pub fn is_open(id: u32) -> bool {
    lock(&PENDING)
        .as_ref()
        .and_then(|m| m.get(&id))
        .is_some_and(|p| p.open.load(Ordering::Relaxed))
}

/// Game end of the pipe plus handshake host/port, once (after [`mark_open`]).
pub fn take_dialed(id: u32) -> Option<(Pipe, String, u16)> {
    let pending = lock(&PENDING).as_mut()?.remove(&id)?;
    let pipe = lock(&DIALED).as_mut()?.remove(&id)?;
    let (host, port) = parse_host_port(&pending.address)?;
    Some((pipe, host, port))
}

fn parse_host_port(address: &str) -> Option<(String, u16)> {
    let address = address.trim();
    if address.is_empty() {
        return None;
    }
    if let Some(rest) = address.strip_prefix('[') {
        let (host, rest) = rest.split_once(']')?;
        let port = rest
            .strip_prefix(':')
            .and_then(|p| p.parse().ok())
            .unwrap_or(25565);
        return Some((host.to_owned(), port));
    }
    match address.rsplit_once(':') {
        Some((host, port)) if !host.is_empty() && port.parse::<u16>().is_ok() && !host.contains(':') => {
            Some((host.to_owned(), port.parse().ok()?))
        }
        _ => Some((address.to_owned(), 25565)),
    }
}

pub fn push(id: u32, data: &[u8]) -> bool {
    lock(&BRIDGES)
        .as_ref()
        .and_then(|b| b.get(&id))
        .is_some_and(|pipe| pipe.push(data))
}

pub fn take(id: u32, max: usize) -> (Vec<u8>, bool) {
    match lock(&BRIDGES).as_ref().and_then(|b| b.get(&id)) {
        Some(pipe) => pipe.take_available(max),
        None => (Vec::new(), true),
    }
}

pub fn close(id: u32) {
    if let Some(pipe) = lock(&BRIDGES).as_mut().and_then(|b| b.remove(&id)) {
        pipe.shutdown();
    }
    lock(&DIALED).as_mut().map(|d| d.remove(&id));
    lock(&PENDING).as_mut().map(|p| p.remove(&id));
}
