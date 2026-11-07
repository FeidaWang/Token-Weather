//! Developer-only same-owner diagnostic. Never creates a host or sends writes.
use anyhow::{Result, anyhow};
use token_weather_core::{
    catalogs::{self, Catalog},
    owner::{Owner, OwnerSpec},
};
fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    let root = args.get(1).ok_or_else(|| anyhow!("root required"))?;
    let pid = args
        .get(2)
        .ok_or_else(|| anyhow!("pid required"))?
        .parse()?;
    let thread = args.get(3).ok_or_else(|| anyhow!("thread required"))?;
    let spec = OwnerSpec::new(root.into(), pid)?;
    let mut owner = Owner::connect(&spec)?;
    owner.require_scope(thread)?;
    let kind = if args.get(4).map(String::as_str) == Some("tools") {
        Catalog::Tools
    } else {
        Catalog::ConnectorMarketplace
    };
    let result = catalogs::collect(&mut owner, thread, kind)?;
    owner.require_root(thread)?;
    owner.check_identity()?;
    println!(
        "{}",
        serde_json::json!({"verified":true,"count":result["apps"].as_array().map(|v|v.len()).or_else(||result["count"].as_u64().map(|v|v as usize))})
    );
    Ok(())
}
