//! Process-local bounded jobs. Fast inventory and slow Marketplace have separate capacity.
use crate::{catalogs::Catalog, digest, now};
use anyhow::Result;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::Semaphore;
#[derive(Clone, Eq, PartialEq, Hash)]
pub struct Key {
    pub owner: String,
    pub thread: String,
    pub catalog: Catalog,
}
enum State {
    Loading { ticket: u64 },
    Ready(Arc<Value>),
    Failed,
}
struct Entry {
    state: State,
    updated: Instant,
    last_ready: Option<Arc<Value>>,
}
#[derive(Clone)]
pub struct CatalogCache {
    entries: Arc<Mutex<HashMap<Key, Entry>>>,
    fast: Arc<Semaphore>,
    slow: Arc<Semaphore>,
    serial: Arc<std::sync::atomic::AtomicU64>,
}
impl Default for CatalogCache {
    fn default() -> Self {
        Self {
            entries: Arc::new(Mutex::new(HashMap::new())),
            fast: Arc::new(Semaphore::new(2)),
            slow: Arc::new(Semaphore::new(1)),
            serial: Arc::new(std::sync::atomic::AtomicU64::new(0)),
        }
    }
}
impl CatalogCache {
    fn status(key: &Key, status: &str) -> Value {
        json!({"kind":key.catalog.kind(),"thread_id":digest(&key.thread),"observed_at":now(),"state":status})
    }
    pub fn read<F>(&self, key: Key, refresh: bool, load: F) -> Value
    where
        F: FnOnce() -> Result<Value> + Send + 'static,
    {
        let mut entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(entry) = entries.get(&key) {
            match &entry.state {
                State::Loading { .. } => return Self::status(&key, "loading"),
                State::Ready(v)
                    if !refresh
                        && v["observed_at"].as_f64().is_some_and(|t| {
                            let ttl = match key.catalog {
                                Catalog::Tools => 10.0,
                                Catalog::AccountLimits => 60.0,
                                _ => 240.0,
                            };
                            (0.0..ttl).contains(&(now() - t))
                        }) =>
                {
                    return (**v).clone();
                }
                State::Failed if !refresh && entry.updated.elapsed() < Duration::from_secs(3) => {
                    return Self::status(&key, "failed");
                }
                _ => {}
            }
        }
        let pool = if key.catalog.slow() {
            self.slow.clone()
        } else {
            self.fast.clone()
        };
        let Ok(permit) = pool.try_acquire_owned() else {
            return Self::status(&key, "loading");
        };
        if entries.len() >= 16 && !entries.contains_key(&key) {
            let old = entries
                .iter()
                .filter(|(_, e)| !matches!(e.state, State::Loading { .. }))
                .min_by_key(|(_, e)| e.updated)
                .map(|(k, _)| k.clone());
            if let Some(old) = old {
                entries.remove(&old);
            } else {
                return Self::status(&key, "loading");
            }
        }
        let ticket = self
            .serial
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let last_ready = entries.get(&key).and_then(|e| match &e.state {
            State::Ready(v) => Some(v.clone()),
            _ => e.last_ready.clone(),
        });
        entries.insert(
            key.clone(),
            Entry {
                state: State::Loading { ticket },
                updated: Instant::now(),
                last_ready: last_ready.clone(),
            },
        );
        let storage = self.entries.clone();
        let job_key = key.clone();
        tokio::spawn(async move {
            let result = tokio::task::spawn_blocking(load).await;
            let mut map = storage.lock().unwrap_or_else(|e| e.into_inner());
            if map
                .get(&job_key)
                .is_some_and(|e| matches!(e.state,State::Loading{ticket:t}if t==ticket))
            {
                let state = match result {
                    Ok(Ok(v)) if v["thread_id"] == digest(&job_key.thread) => {
                        State::Ready(Arc::new(v))
                    }
                    _ => State::Failed,
                };
                let retained = if matches!(state, State::Failed) {
                    last_ready
                } else {
                    None
                };
                map.insert(
                    job_key,
                    Entry {
                        state,
                        updated: Instant::now(),
                        last_ready: retained,
                    },
                );
            }
            drop(permit);
        });
        Self::status(&key, "loading")
    }
    pub fn ready(&self, key: &Key, max_age: f64) -> Option<Value> {
        let entries = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        let entry = entries.get(key)?;
        let value = match &entry.state {
            State::Ready(v) => Some(v),
            _ => entry.last_ready.as_ref(),
        }?;
        value["observed_at"]
            .as_f64()
            .filter(|t| (0.0..max_age).contains(&(now() - t)))
            .map(|_| (**value).clone())
    }
}
