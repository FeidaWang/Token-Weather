mod api_cost;
pub mod cache;
pub mod catalogs;
pub mod forecast;
pub mod history;
pub mod owner;
pub mod service;

use anyhow::{Result, ensure};
use sha2::{Digest, Sha256};
use std::time::{SystemTime, UNIX_EPOCH};

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn digest(bytes: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(bytes.as_ref()))
}
pub fn thread_id(value: &str) -> Result<()> {
    ensure!(
        uuid::Uuid::parse_str(value)?.to_string() == value,
        "Invalid thread identity"
    );
    Ok(())
}
pub fn name(value: &serde_json::Value) -> Result<String> {
    let s = value
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("Missing metadata name"))?;
    ensure!(
        !s.trim().is_empty() && s.chars().count() <= 512 && !s.chars().any(|c| c.is_control()),
        "Invalid metadata name"
    );
    Ok(s.to_owned())
}
