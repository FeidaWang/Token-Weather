//! Runtime composition. No Python interpreter or second App Server is launched.
use crate::{
    cache::{CatalogCache, Key},
    catalogs::{self, Catalog},
    digest, forecast,
    history::HistoryStore,
    now,
    owner::{Owner, OwnerSpec},
    thread_id,
};
use anyhow::{Result, anyhow, ensure};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    path::PathBuf,
    sync::{Arc, Mutex},
};
pub const TOOL: &str = "token_weather_context";
pub const RESOURCE: &str = "ui://token-weather/context.html";
pub const MIME: &str = "text/html;profile=mcp-app";
fn native_chat_usage(value: &Value, id: &str) -> Option<Value> {
    let usage = &value["threadUsage"];
    if usage["threadId"] != id {
        return None;
    }
    let micros = |key: &str| usage[key].as_u64().filter(|n| *n <= 9_007_199_254_740_991);
    let credits = micros("estimatedUsageCreditsMicros");
    let estimated_usd = micros("estimatedUsageUsdMicros");
    let native_usd = micros("nativeUsageUsdMicros");
    if credits.is_none() && estimated_usd.is_none() && native_usd.is_none() {
        return None;
    }
    Some(json!({
        "kind":"codex_thread_usage","thread_id":digest(id),"observed_at":now(),
        "estimated_credits_micros":credits,"estimated_usd_micros":estimated_usd,
        "native_usd_micros":native_usd
    }))
}
fn apply_live_usage(reading: &mut Value, usage: &Value, clock: f64) {
    // Snapshot polling refreshes the envelope, never the original usage event.
    // An old owner event must not revive a stale disk reading or its forecast.
    if usage["live_event"] != true
        || !usage["tokens"].as_u64().is_some_and(|n| n > 0)
        || !usage["at"].as_f64().is_some_and(|at| {
            (-1.0..=30.0).contains(&(clock - at))
                && at >= reading["last_request"]["at"].as_f64().unwrap_or(0.0)
        })
    {
        return;
    }
    reading["available"] = json!(true);
    reading["tokens"] = usage["tokens"].clone();
    reading["stale"] = json!(false);
    reading["max_age_seconds"] = json!(30);
    if usage["observed_window"].as_u64().is_some_and(|n| n > 0) {
        reading["observed_window"] = usage["observed_window"].clone();
    }
    let mut last = json!({});
    for key in [
        "at",
        "input",
        "output",
        "cached_input",
        "cache_write_input",
        "reasoning_output",
    ] {
        if let Some(v) = usage.get(key) {
            last[key] = v.clone();
        }
    }
    reading["last_request"] = last;
}
#[derive(Clone)]
pub struct Service {
    pub home: PathBuf,
    pub owner: Option<OwnerSpec>,
    history: Arc<Mutex<HistoryStore>>,
    cache: CatalogCache,
    owner_slots: Arc<tokio::sync::Semaphore>,
    opened_panels: Arc<Mutex<VecDeque<String>>>,
}
impl Service {
    pub fn new(
        home: PathBuf,
        owner: Option<OwnerSpec>,
        state_dir: Option<PathBuf>,
    ) -> Result<Self> {
        let home = std::fs::canonicalize(home)?;
        crate::history::no_links(&home)?;
        if let Some(spec) = &owner {
            ensure!(home == spec.home(), "Owner/profile mismatch");
        }
        Ok(Self {
            home,
            owner,
            history: Arc::new(Mutex::new(HistoryStore::new(state_dir))),
            cache: CatalogCache::default(),
            owner_slots: Arc::new(tokio::sync::Semaphore::new(3)),
            opened_panels: Arc::default(),
        })
    }
    fn read_catalog(&self, spec: OwnerSpec, key: Key, refresh: bool) -> Value {
        let slots = self.owner_slots.clone();
        let id = key.thread.clone();
        let instance = key.owner.clone();
        let kind = key.catalog;
        self.cache.read(key, refresh, move || {
            let _permit = tokio::runtime::Handle::current().block_on(tokio::time::timeout(
                std::time::Duration::from_secs(3),
                slots.acquire_owned(),
            ))??;
            let mut source = Owner::connect(&spec)?;
            ensure!(
                source.instance == instance,
                "Owner changed during catalog read"
            );
            // The isolated bridge checks account reads against its own root metadata.
            if kind != Catalog::AccountLimits {
                source.require_scope(&id)?;
            }
            let value = catalogs::collect(&mut source, &id, kind)?;
            if kind != Catalog::AccountLimits {
                source.require_root(&id)?;
            }
            source.check_identity()?;
            Ok(value)
        })
    }
    pub async fn reading(
        &self,
        id: String,
        section: String,
        catalog: String,
        refresh: bool,
    ) -> Result<Value> {
        thread_id(&id)?;
        ensure!(
            [
                "",
                "runtime",
                "tools",
                "plugins",
                "connectors",
                "skills",
                "usage"
            ]
            .contains(&section.as_str()),
            "Unknown section"
        );
        ensure!(
            ["installed", "marketplace"].contains(&catalog.as_str()),
            "Unknown catalog"
        );
        let home = self.home.clone();
        let history = self.history.clone();
        let tid = id.clone();
        let numeric = tokio::task::spawn_blocking(move || {
            history
                .lock()
                .map_err(|_| anyhow!("History lock unavailable"))?
                .read(&home, &tid, now())
        })
        .await?;
        let binding = digest(&id);
        let mut report = json!({});
        let mut reading = json!({"schema_version":1,"thread_id":binding,"available":false,"implementation":"rust"});
        if let Ok((r, collection)) = numeric {
            report = r;
            for key in [
                "tokens",
                "observed_window",
                "last_request",
                "session_usage",
                "turn_reasoning",
                "weekly_quota",
                "api_cost_estimate",
                "stale",
                "max_age_seconds",
            ] {
                reading[key] = report[key].clone();
            }
            reading["available"] = json!(true);
            reading["collection"] = collection;
            if report["last_request"].is_object() {
                let mut last = json!({});
                for key in [
                    "at",
                    "input",
                    "output",
                    "cached_input",
                    "cache_write_input",
                    "reasoning_output",
                ] {
                    if let Some(v) = report["last_request"].get(key) {
                        last[key] = v.clone();
                    }
                }
                reading["last_request"] = last;
            }
            if reading["tokens"].as_u64().is_some_and(|n| n > 0)
                && reading["last_request"]["input"] == 0
                && reading["last_request"]["output"] == 0
            {
                reading["last_request"] = json!({"at":reading["last_request"]["at"]});
            }
        }
        let enabled = self.owner.is_some();
        for key in [
            "runtime_supported",
            "skill_catalog_supported",
            "plugin_catalog_supported",
        ] {
            reading[key] = json!(enabled);
        }
        // These fixed public destinations do not require owner inventory access.
        reading["native_navigation_supported"] = json!(true);
        let mut capabilities = json!({"mcp_tools":{"available":false,"reason":"same_app_server_inventory_unavailable"},"memory_files":{"available":false,"reason":"memory_source_files_unavailable"},"until_auto_compact":{"available":false,"reason":"effective_boundary_and_usage_unverified"},"compact_session":{"available":false,"reason":"read_only_runtime"},"skills":{"available":false,"reason":"workspace_catalog_unavailable"}});
        if let Some(spec) = self.owner.clone() {
            let tid = id.clone();
            let slots = self.owner_slots.clone();
            let usage_supported = spec.allowed.contains(&id);
            let read_limits = reading["available"] == true && section == "runtime";
            let read_usage = section == "usage" && usage_supported;
            let read_introduced = section == "runtime" && !usage_supported;
            let session_spec = spec.clone();
            let session = tokio::task::spawn_blocking(
                move || -> Result<(Owner, Option<Value>, Option<Value>)> {
                    let _permit =
                        tokio::runtime::Handle::current().block_on(tokio::time::timeout(
                            std::time::Duration::from_secs(2),
                            slots.acquire_owned(),
                        ))??;
                    let mut owner = Owner::connect(&session_spec)?;
                    let runtime = owner
                        .require_scope(&tid)
                        .and_then(|()| owner.read("bridge/runtime", json!({"threadId":tid})))
                        .ok()
                        .or_else(|| {
                            if !read_introduced {
                                return None;
                            }
                            owner
                                .read("bridge/runtime", json!({"threadId":tid}))
                                .ok()
                                .filter(|r| {
                                    r["compaction"]["source"] == "introduced_owner_session"
                                        && r["compaction"]["target_thread_id"] == tid
                                        && r["compaction"]["supported"] == true
                                })
                        });
                    owner.check_identity()?;
                    let usage = if read_usage && runtime.is_some() {
                        let result = owner.read("account/usage/read", json!({"threadId":tid}));
                        owner.require_root(&tid)?;
                        owner.check_identity()?;
                        result.ok().and_then(|v| native_chat_usage(&v, &tid))
                    } else {
                        None
                    };
                    Ok((owner, runtime, usage))
                },
            )
            .await?;
            if let Ok((owner, _, _)) = &session
                && read_limits
            {
                let key = Key {
                    owner: owner.instance.clone(),
                    thread: id.clone(),
                    catalog: Catalog::AccountLimits,
                };
                let limits = self.read_catalog(spec.clone(), key.clone(), refresh);
                reading["account_limits"] = if limits.get("state").is_none() {
                    limits
                } else {
                    self.cache.ready(&key, 75.0).unwrap_or(limits)
                };
            }
            if let Ok((owner, Some(runtime), usage)) = session {
                ensure!(runtime["thread_id"] == binding, "Runtime binding mismatch");
                reading["native_usage_supported"] = json!(usage_supported);
                let introduced = runtime["compaction"]["source"] == "introduced_owner_session";
                reading["tool_catalog_supported"] = json!(!introduced);
                if let Some(usage) = usage {
                    reading["native_usage"] = usage;
                }
                if runtime["compaction"]["supported"] == true {
                    capabilities["compact_session"] =
                        json!({"available":true,"source":runtime["compaction"]["source"]});
                }
                apply_live_usage(&mut reading, &runtime["usage"], now());
                if runtime["awaiting_usage"] == true {
                    reading["available"] = json!(true);
                    reading["tokens"] = Value::Null;
                    reading["last_request"] = Value::Null;
                    reading["stale"] = json!(true);
                }
                if ["effective_runtime_remaining", "sampling_runtime_remaining"]
                    .contains(&runtime["auto_compact"]["kind"].as_str().unwrap_or(""))
                {
                    capabilities["until_auto_compact"] =
                        json!({"available":true,"source":"same_owner_engine_compaction_trace"});
                }
                reading["runtime"] = runtime;
                let kind = match section.as_str() {
                    "plugins" => Some(if catalog == "marketplace" {
                        Catalog::Marketplace
                    } else {
                        Catalog::Plugins
                    }),
                    "connectors" => Some(if catalog == "marketplace" {
                        Catalog::ConnectorMarketplace
                    } else {
                        Catalog::Connectors
                    }),
                    "skills" => Some(Catalog::Skills),
                    "" | "tools" => Some(Catalog::Tools),
                    _ => None,
                };
                if let Some(kind) = kind {
                    let key = Key {
                        owner: owner.instance.clone(),
                        thread: id.clone(),
                        catalog: kind,
                    };
                    let value = self.read_catalog(spec, key, refresh);
                    let field = match kind {
                        Catalog::Plugins | Catalog::Marketplace => "plugins",
                        Catalog::Connectors | Catalog::ConnectorMarketplace => "connectors",
                        Catalog::Skills => "skills",
                        Catalog::Tools => "mcp_tools",
                        Catalog::AccountLimits => "account_limits",
                    };
                    if kind == Catalog::Tools && value.get("state").is_some() {
                        reading["mcp_tools_state"] = value["state"].clone();
                    }
                    if kind != Catalog::Tools || value.get("state").is_none() {
                        reading[field] = value;
                    }
                    if kind == Catalog::Tools && reading.get("mcp_tools").is_some() {
                        capabilities["mcp_tools"] =
                            json!({"available":true,"source":"same_app_server_thread_catalog"});
                    }
                }
                // Runtime polling consumes completed tool jobs without launching
                // discovery. Retain a still-fresh snapshot during a refresh,
                // keeping its original observation time and exact owner/root key.
                if !introduced
                    && ["", "runtime", "tools"].contains(&section.as_str())
                    && reading.get("mcp_tools").is_none()
                    && let Some(value) = self.cache.ready(
                        &Key {
                            owner: owner.instance.clone(),
                            thread: id.clone(),
                            catalog: Catalog::Tools,
                        },
                        15.0,
                    )
                {
                    reading["mcp_tools"] = value;
                    capabilities["mcp_tools"] =
                        json!({"available":true,"source":"same_app_server_thread_catalog"});
                }
            }
        }
        reading["forecast"] = forecast::compute(&report, &reading, now())?;
        Ok(
            json!({"content":[{"type":"text","text":"Open Token Weather Context to view the local reading."}],"_meta":{"reading":reading,"capabilities":capabilities}}),
        )
    }
    pub async fn dispatch(&self, method: &str, p: &Value) -> Result<Value> {
        match method {
            "initialize" => Ok(
                json!({"protocolVersion":p["protocolVersion"].as_str().unwrap_or("2025-11-25"),"capabilities":{"tools":{},"resources":{}},"serverInfo":{"name":"token_weather_context","version":env!("CARGO_PKG_VERSION")}}),
            ),
            "ping" => Ok(json!({})),
            "tools/list" => {
                let mut result = Ok(
                    json!({"tools":[{"name":TOOL,"title":"Token Weather Context","description":"Open the local context reading for the current Codex chat.","inputSchema":{"type":"object","properties":{"section":{"type":"string","enum":["skills","plugins","connectors","runtime","tools","usage"]},"catalog":{"type":"string","enum":["installed","marketplace"]},"refresh":{"type":"boolean"}},"additionalProperties":false},"annotations":{"readOnlyHint":true,"destructiveHint":false,"openWorldHint":false},"_meta":{"ui":{"resourceUri":RESOURCE,"visibility":["model","app"]},"openai/ui":{"entrypoints":[{"type":"thread"}]}}}]}),
                );
                if self.owner.is_some() {
                    let value: &mut Value = result.as_mut().unwrap();
                    value["tools"].as_array_mut().unwrap().push(json!({
                        "name":"token_weather_compact_session", "title":"Compact session",
                        "description":"Compact the idle, authorised host-bound chat after a button click.",
                        "inputSchema":{"type":"object","properties":{"operation_id":{"type":"string","format":"uuid"}},"required":["operation_id"],"additionalProperties":false},
                        "annotations":{"readOnlyHint":false,"destructiveHint":true,"openWorldHint":false},
                        "_meta":{"ui":{"resourceUri":RESOURCE,"visibility":["app"]}}
                    }));
                }
                result
            }
            "resources/list" => Ok(
                json!({"resources":[{"uri":RESOURCE,"name":"Token Weather Context","mimeType":MIME}]}),
            ),
            "resources/templates/list" => Ok(json!({"resourceTemplates":[]})),
            "resources/read" if p["uri"] == RESOURCE => Ok(
                json!({"contents":[{"uri":RESOURCE,"name":"Token Weather Context","mimeType":MIME,"text":html(),"_meta":{"ui":{"prefersBorder":false},"openai/ui":{"availableDisplayModes":["inline","fullscreen"]}}}]}),
            ),
            "tools/call" if p["name"] == "token_weather_compact_session" => {
                let id = p["_meta"]["threadId"]
                    .as_str()
                    .ok_or_else(|| anyhow!("No host binding"))?
                    .to_owned();
                let args = &p["arguments"];
                ensure!(
                    args.as_object()
                        .is_some_and(|a| a.len() == 1 && a.contains_key("operation_id")),
                    "Invalid compact arguments"
                );
                let operation = args["operation_id"]
                    .as_str()
                    .ok_or_else(|| anyhow!("Operation identity required"))?
                    .to_owned();
                thread_id(&id)?;
                thread_id(&operation)?;
                let spec = self
                    .owner
                    .clone()
                    .ok_or_else(|| anyhow!("Owner control unavailable"))?;
                let binding = digest(&id);
                let value = tokio::task::spawn_blocking(move || {
                    Owner::connect(&spec)?.compact(&id, &operation)
                })
                .await??;
                Ok(
                    json!({"content":[{"type":"text","text":"Compaction status updated."}],"_meta":{"thread_id":binding,"compaction":value}}),
                )
            }
            "tools/call" if p["name"] == TOOL => {
                let id = p["_meta"]["threadId"]
                    .as_str()
                    .ok_or_else(|| anyhow!("No local chat binding"))?;
                let args = p.get("arguments").cloned().unwrap_or(json!({}));
                ensure!(
                    args.as_object().is_some_and(|o| o
                        .keys()
                        .all(|k| ["section", "catalog", "refresh"].contains(&k.as_str()))),
                    "Unexpected arguments"
                );
                if let Some(v) = args.get("refresh") {
                    ensure!(v.is_boolean(), "Invalid refresh");
                }
                for key in ["section", "catalog"] {
                    if let Some(v) = args.get(key) {
                        ensure!(v.is_string(), "Invalid catalog selector");
                    }
                }
                let section = args["section"].as_str().unwrap_or("");
                let mut result = self
                    .reading(
                        id.into(),
                        section.into(),
                        args["catalog"].as_str().unwrap_or("installed").into(),
                        args["refresh"] == true,
                    )
                    .await?;
                let meta = &mut result["_meta"];
                if meta["reading"]["available"] == true && meta["reading"]["tokens"].is_u64() {
                    if section.is_empty() {
                        meta["entrance"] = json!("invocation");
                    } else if section == "runtime" {
                        let mut opened =
                            self.opened_panels.lock().unwrap_or_else(|e| e.into_inner());
                        let first = !opened.iter().any(|thread| thread == id);
                        if first {
                            if opened.len() == 128 {
                                opened.pop_front();
                            }
                            opened.push_back(id.to_owned());
                        }
                        meta["first_panel_view"] = json!(first);
                    }
                }
                Ok(result)
            }
            _ => Err(anyhow!("Method or resource not found")),
        }
    }
}
pub fn html() -> String {
    include_str!("../../../src/token_weather/assets/context.html")
        .replace("__WEATHER_LIVE__", "false")
        .replace("__CONTEXT_MCP__", "true")
        .replace("__WEATHER_DATA__", "{}")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_usage_keeps_units_and_distinguishes_unknown_from_zero() {
        let source = json!({"threadUsage":{"threadId":"id","estimatedUsageCreditsMicros":1230000,
            "estimatedUsageUsdMicros":0,"nativeUsageUsdMicros":null,"groups":["PRIVATE"]}});
        assert!(native_chat_usage(&source, "other").is_none());
        assert!(native_chat_usage(&json!({"threadUsage":null}), "id").is_none());
        let projected = native_chat_usage(&source, "id").unwrap();
        assert_eq!(projected["estimated_credits_micros"], 1230000);
        assert_eq!(projected["estimated_usd_micros"], 0);
        assert!(projected["native_usd_micros"].is_null());
        assert!(!projected.to_string().contains("PRIVATE"));
    }
    #[test]
    fn old_live_event_cannot_refresh_stale_forecast() {
        let original = json!({"thread_id":"bound","available":true,"tokens":10000,"observed_window":100000,"stale":true,"last_request":{"at":90}});
        let usage = json!({"at":100,"tokens":12000,"live_event":true});
        for clock in [131.0, 1000.0, 98.0] {
            let mut reading = original.clone();
            apply_live_usage(&mut reading, &usage, clock);
            assert_eq!(reading, original);
            let samples = json!([{"turn":"one","tokens":8000,"at":80,"epoch":0,"is_turn_end":true}, {"turn":"two","tokens":10000,"at":90,"epoch":0,"is_turn_end":true}]);
            let forecast = forecast::compute(
                &json!({"samples":samples,"last_request":samples[1]}),
                &reading,
                clock,
            )
            .unwrap();
            assert_eq!(forecast["paused"], true);
            assert_eq!(forecast["outlook"], json!([]));
        }
        let mut fresh = original.clone();
        apply_live_usage(&mut fresh, &usage, 110.0);
        assert_eq!(fresh["tokens"], 12000);
        assert_eq!(fresh["stale"], false);
        assert_eq!(fresh["last_request"]["at"], 100);
    }
}
