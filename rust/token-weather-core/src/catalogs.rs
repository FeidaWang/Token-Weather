//! Small, strict projections of native metadata. Marketplace is never implicit.
use crate::{digest, name, now, owner::Owner};
use anyhow::{Result, anyhow, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet, HashSet},
    path::PathBuf,
    sync::OnceLock,
    time::{Duration, Instant},
};
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
pub enum Catalog {
    Plugins,
    Marketplace,
    Connectors,
    ConnectorMarketplace,
    Skills,
    Tools,
    AccountLimits,
}
impl Catalog {
    pub fn slow(self) -> bool {
        matches!(self, Self::Marketplace | Self::ConnectorMarketplace)
    }
    pub fn kind(self) -> &'static str {
        match self {
            Self::Plugins | Self::Marketplace => "owner_plugin_catalog",
            Self::Connectors | Self::ConnectorMarketplace => "native_connector_catalog",
            Self::Skills => "workspace_available",
            Self::Tools => "owner_tool_catalog",
            Self::AccountLimits => "codex_account_limits",
        }
    }
}
pub trait Source {
    fn request(&mut self, method: &str, params: Value) -> Result<Value>;
    fn workspace(&self) -> PathBuf;
    fn reconnect(&mut self, _thread: &str) -> Result<()> {
        Ok(())
    }
}
impl Source for Owner {
    fn request(&mut self, m: &str, p: Value) -> Result<Value> {
        self.read(m, p)
    }
    fn workspace(&self) -> PathBuf {
        self.workspace()
    }
    fn reconnect(&mut self, id: &str) -> Result<()> {
        self.reconnect(id)
    }
}
fn array<'a>(value: &'a Value, key: &str, max: usize) -> Result<&'a Vec<Value>> {
    value[key]
        .as_array()
        .filter(|a| a.len() <= max)
        .ok_or_else(|| anyhow!("Incomplete metadata list"))
}
fn boolean(value: &Value, key: &str) -> Result<bool> {
    value[key]
        .as_bool()
        .ok_or_else(|| anyhow!("Missing metadata state"))
}
fn description(v: &Value) -> Result<String> {
    if v.is_null() {
        return Ok(String::new());
    }
    Ok(v.as_str()
        .ok_or_else(|| anyhow!("Invalid description"))?
        .chars()
        .take(2000)
        .collect())
}
fn insert(map: &mut BTreeMap<String, Value>, key: String, value: Value, max: usize) -> Result<()> {
    if let Some(old) = map.get(&key) {
        ensure!(*old == value, "Conflicting catalog identity");
    }
    map.insert(key, value);
    ensure!(map.len() <= max, "Catalog bound exceeded");
    Ok(())
}
pub fn plugin_url(id: &Value) -> Option<String> {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    let re=RE.get_or_init(||regex::Regex::new(r"\A(?:plugins_[0-9a-f]{32}|(?:plugins~)?Plugin_[0-9a-f]{32}|plugin_[A-Za-z0-9][A-Za-z0-9_-]{0,247})\z").unwrap());
    id.as_str()
        .filter(|s| re.is_match(s))
        .map(|s| format!("codex://plugins/{s}"))
}
pub fn install_url(value: &Value, app: &str) -> Option<String> {
    let s = value.as_str()?;
    if s.len() > 1024 || s.trim() != s || s.chars().any(|c| c.is_control()) {
        return None;
    }
    let url = url::Url::parse(s).ok()?;
    let authority = s.split_once("://")?.1.split('/').next()?;
    if url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
        || authority.contains(':')
    {
        return None;
    }
    if (url.scheme() == "https"
        && url.host_str() == Some("chatgpt.com")
        && url.path().starts_with("/plugins/"))
        || (url.scheme() == "codex" && url.host_str() == Some("plugins"))
    {
        let id = if url.scheme() == "https" {
            url.path().strip_prefix("/plugins/")?
        } else {
            url.path().strip_prefix('/')?
        };
        return plugin_url(&json!(id));
    }
    let parts: Vec<_> = url.path().split('/').collect();
    let safe = |v: &str| {
        !v.is_empty()
            && v.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    };
    if url.scheme() == "https"
        && url.host_str() == Some("chatgpt.com")
        && parts.len() == 4
        && parts[1] == "apps"
        && safe(parts[2])
        && parts[3] == app
        && safe(app)
        && (app.starts_with("connector_") || app.starts_with("asdk_app_"))
    {
        Some(s.into())
    } else {
        None
    }
}
fn plugin_response(s: &mut impl Source, market: bool) -> Result<Value> {
    let mut p = json!({"cwds":[s.workspace()]});
    if market {
        p["forceRefetch"] = json!(false);
    }
    let v = s.request(
        if market {
            "plugin/list"
        } else {
            "plugin/installed"
        },
        p,
    )?;
    ensure!(
        v["marketplaceLoadErrors"] == json!([]),
        "Plugin catalog incomplete"
    );
    array(&v, "marketplaces", 20000)?;
    Ok(v)
}
pub fn plugins(s: &mut impl Source, id: &str, market: bool) -> Result<Value> {
    let response = plugin_response(s, market)?;
    let mut items = BTreeMap::new();
    for origin in array(&response, "marketplaces", 20000)? {
        let origin_name = name(&origin["name"])?;
        for p in array(origin, "plugins", 20000)? {
            let installed = boolean(p, "installed")?;
            let enabled = boolean(p, "enabled")?;
            if (!market && !installed)
                || (market
                    && (installed
                        || p["availability"] != "AVAILABLE"
                        || !["AVAILABLE", "INSTALLED_BY_DEFAULT"]
                            .contains(&p["installPolicy"].as_str().unwrap_or(""))))
            {
                continue;
            }
            let identity = name(
                p.get("remotePluginId")
                    .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
                    .unwrap_or(&p["id"]),
            )?;
            let title = name(
                p["interface"]
                    .get("displayName")
                    .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
                    .unwrap_or(&p["name"]),
            )?;
            let value = json!({"id":identity,"name":name(&p["name"])? ,"title":title,"description":description(&p["interface"]["shortDescription"])? ,"marketplace":origin_name,"installed":installed,"enabled":enabled,"native_url":plugin_url(&p["remotePluginId"])});
            insert(&mut items, identity, value, 20000)?;
        }
    }
    let mut rows: Vec<_> = items.into_values().collect();
    rows.sort_by_key(|p| {
        (
            p["title"].as_str().unwrap().to_lowercase(),
            p["id"].as_str().unwrap().to_owned(),
        )
    });
    let key = if market { "marketplace" } else { "installed" };
    let mut v = json!({"kind":"owner_plugin_catalog","thread_id":digest(id),"observed_at":now(),"workspace":s.workspace(),"catalog":key});
    v[key] = json!(rows);
    Ok(v)
}
pub fn installed(s: &mut impl Source, id: &str) -> Result<Value> {
    let v = s.request("app/installed", json!({"threadId":id,"forceRefresh":false}))?;
    let mut items = BTreeMap::new();
    for a in array(&v, "apps", 20000)? {
        let identity = name(&a["id"])?;
        let value = json!({"id":identity,"name":name(a.get("runtimeName").filter(|v|v.as_str().is_some_and(|s|!s.is_empty())).unwrap_or(&a["id"]))?,"description":"","accessible":true,"enabled":boolean(a,"enabled")?,"runtime_present":true,"runtime_callable":boolean(a,"callable")?,"native_url":"codex://skills"});
        insert(&mut items, identity, value, 20000)?;
    }
    let mut rows: Vec<_> = items.into_values().collect();
    rows.sort_by_key(|p| {
        (
            p["name"].as_str().unwrap().to_lowercase(),
            p["id"].as_str().unwrap().to_owned(),
        )
    });
    Ok(
        json!({"kind":"native_connector_catalog","thread_id":digest(id),"observed_at":now(),"catalog":"installed","manage_url":"codex://skills","apps":rows}),
    )
}
pub fn connectors(s: &mut impl Source, id: &str) -> Result<Value> {
    let catalog = plugin_response(s, true)?;
    let mut native: BTreeMap<String, Value> = BTreeMap::new();
    for market in array(&catalog, "marketplaces", 20000)? {
        for p in array(market, "plugins", 20000)? {
            if let Some(remote) = p["remotePluginId"]
                .as_str()
                .filter(|v| v.starts_with("plugin_asdk_app_"))
            {
                let target = if p["availability"] == "AVAILABLE"
                    && (p["installed"] == true
                        || ["AVAILABLE", "INSTALLED_BY_DEFAULT"]
                            .contains(&p["installPolicy"].as_str().unwrap_or("")))
                {
                    json!(plugin_url(&p["remotePluginId"]))
                } else {
                    Value::Null
                };
                insert(
                    &mut native,
                    remote.trim_start_matches("plugin_").into(),
                    target,
                    20000,
                )?;
            }
        }
    }
    let installed = installed(s, id)?;
    let runtime: HashSet<_> = array(&installed, "apps", 20000)?
        .iter()
        .map(|a| a["id"].as_str().unwrap().to_owned())
        .collect();
    let callable: HashSet<_> = array(&installed, "apps", 20000)?
        .iter()
        .filter(|a| a["runtime_callable"] == true)
        .map(|a| a["id"].as_str().unwrap().to_owned())
        .collect();
    let mut cursor = None;
    let mut seen = HashSet::new();
    let mut items = BTreeMap::new();
    let mut bounded = false;
    let mut complete = false;
    let start = Instant::now();
    for _ in 0..200 {
        ensure!(
            start.elapsed() < Duration::from_secs(35),
            "Connector catalog deadline expired"
        );
        let mut params = json!({"threadId":id,"forceRefetch":false});
        if bounded {
            params["limit"] = json!(100);
        }
        if let Some(c) = &cursor {
            params["cursor"] = json!(c);
        }
        let page = match s.request("app/list", params.clone()) {
            Ok(v) => v,
            Err(e) => {
                if bounded {
                    return Err(e);
                }
                bounded = true;
                s.reconnect(id)?;
                params["limit"] = json!(100);
                s.request("app/list", params)?
            }
        };
        for a in array(&page, "data", if bounded { 100 } else { 20000 })? {
            let identity = name(&a["id"])?;
            let target = native
                .get(&identity)
                .cloned()
                .unwrap_or_else(|| json!(install_url(&a["installUrl"], &identity)));
            let v = json!({"id":identity,"name":name(&a["name"])? ,"description":description(&a["description"])? ,"accessible":boolean(a,"isAccessible")?,"enabled":boolean(a,"isEnabled")?,"runtime_present":runtime.contains(&identity),"runtime_callable":callable.contains(&identity),"native_url":target});
            insert(&mut items, identity, v, 20000)?;
        }
        match page["nextCursor"].as_str() {
            None if page["nextCursor"].is_null() => {
                complete = true;
                break;
            }
            Some(c) if !c.is_empty() && seen.insert(c.to_owned()) => cursor = Some(c.to_owned()),
            _ => bail!("Invalid connector pagination"),
        }
    }
    ensure!(complete, "Connector page bound exceeded");
    let mut rows: Vec<_> = items.into_values().collect();
    rows.sort_by_key(|a| {
        (
            !a["accessible"].as_bool().unwrap(),
            a["name"].as_str().unwrap().to_lowercase(),
            a["id"].as_str().unwrap().to_owned(),
        )
    });
    Ok(
        json!({"kind":"native_connector_catalog","thread_id":digest(id),"observed_at":now(),"catalog":"marketplace","manage_url":"codex://skills","apps":rows}),
    )
}
pub fn skills(s: &mut impl Source, id: &str) -> Result<Value> {
    let v = s.request(
        "skills/list",
        json!({"cwds":[s.workspace()],"forceReload":false}),
    )?;
    let data = array(&v, "data", 1)?;
    ensure!(data.len() == 1, "Incomplete workspace skills");
    let entry = &data[0];
    ensure!(
        entry["cwd"] == json!(s.workspace()) && entry["errors"] == json!([]),
        "Skill workspace mismatch"
    );
    let mut items = BTreeMap::new();
    for p in array(entry, "skills", 5000)? {
        let path = p["path"]
            .as_str()
            .filter(|p| !p.is_empty() && p.len() <= 4096)
            .ok_or_else(|| anyhow!("Invalid skill identity"))?;
        ensure!(
            ["repo", "user", "system", "admin"].contains(&p["scope"].as_str().unwrap_or("")),
            "Unknown skill scope"
        );
        let desc = p["interface"]
            .get("shortDescription")
            .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
            .or_else(|| {
                p.get("shortDescription")
                    .filter(|v| v.as_str().is_some_and(|s| !s.is_empty()))
            })
            .unwrap_or(&p["description"]);
        ensure!(desc.is_string(), "Invalid skill description");
        let identity = digest(path);
        let mut item = json!({"id":identity,"name":name(&p["name"])? ,"description":description(desc)?,"scope":p["scope"],"enabled":boolean(p,"enabled")?});
        for (key, value) in [
            ("title", &p["interface"]["displayName"]),
            ("plugin_id", &p["pluginId"]),
        ] {
            if value.as_str().is_some_and(|s| !s.is_empty()) {
                item[key] = json!(name(value)?);
            }
        }
        insert(&mut items, identity, item, 5000)?;
    }
    let mut rows: Vec<_> = items.into_values().collect();
    rows.sort_by_key(|p| {
        (
            p.get("title")
                .unwrap_or(&p["name"])
                .as_str()
                .unwrap()
                .to_lowercase(),
            p["id"].as_str().unwrap().to_owned(),
        )
    });
    Ok(
        json!({"kind":"workspace_available","thread_id":digest(id),"observed_at":now(),"workspace":s.workspace(),"count":rows.len(),"skills":rows}),
    )
}
pub fn tools(s: &mut impl Source, id: &str) -> Result<Value> {
    let at = now();
    let start = Instant::now();
    let mut cursor = None;
    let mut seen = HashSet::new();
    let mut groups: BTreeMap<String, Value> = BTreeMap::new();
    let mut details: BTreeMap<String, Value> = BTreeMap::new();
    let mut complete = false;
    for _ in 0..100 {
        ensure!(
            start.elapsed() < Duration::from_secs(5),
            "Tool catalog deadline expired"
        );
        let mut p = json!({"threadId":id,"detail":"toolsAndAuthOnly","limit":100});
        if let Some(c) = &cursor {
            p["cursor"] = json!(c);
        }
        let page = s.request("mcpServerStatus/list", p)?;
        for server in array(&page, "data", 100)? {
            // The native schema explicitly allows null when runtime state is
            // unavailable or configuration changed. Successful catalog discovery
            // is independent of a reported live connection state.
            let status = match server.get("runtimeStatus") {
                Some(Value::String(s)) if s == "connected" => "connected",
                Some(Value::Null) => "unreported",
                _ => bail!("Tool runtime state unavailable"),
            };
            ensure!(
                server.get("toolsError") == Some(&Value::Null),
                "Tool catalog unavailable"
            );
            let label = name(&server["name"])?;
            let tools = server["tools"]
                .as_object()
                .filter(|t| t.len() <= 20000)
                .ok_or_else(|| anyhow!("Invalid tool catalog"))?;
            let mut names = BTreeSet::new();
            for tool in tools.values() {
                names.insert(name(&tool["name"])?);
            }
            let transport = match server.get("httpOrigin") {
                Some(Value::Null) => "local",
                Some(Value::String(s))
                    if url::Url::parse(s).is_ok_and(|u| {
                        ["http", "https"].contains(&u.scheme())
                            && u.host_str().is_some()
                            && u.username().is_empty()
                            && u.password().is_none()
                    }) =>
                {
                    "web"
                }
                _ => "unknown",
            };
            let mut detail = json!({"name":label,"status":status,"transport":transport});
            for (k, v) in [
                ("title", &server["serverInfo"]["title"]),
                ("plugin_id", &server["pluginId"]),
            ] {
                if let Ok(n) = name(v) {
                    detail[k] = json!(n);
                }
            }
            insert(
                &mut groups,
                label.clone(),
                json!({"name":label,"count":names.len(),"tools":names}),
                512,
            )?;
            insert(&mut details, label, detail, 512)?;
        }
        match page["nextCursor"].as_str() {
            None if page["nextCursor"].is_null() => {
                complete = true;
                break;
            }
            Some(c) if !c.is_empty() && seen.insert(c.to_owned()) => cursor = Some(c.to_owned()),
            _ => bail!("Invalid tools pagination"),
        }
    }
    ensure!(
        complete && start.elapsed() < Duration::from_secs(5),
        "Tool catalog incomplete"
    );
    let total: u64 = groups.values().map(|v| v["count"].as_u64().unwrap()).sum();
    ensure!(total <= 20000, "Tool count exceeded");
    Ok(
        json!({"kind":"owner_tool_catalog","thread_id":digest(id),"observed_at":at,"workspace":s.workspace(),"count":total,"servers":groups.into_values().collect::<Vec<_>>(),"server_details":details.into_values().collect::<Vec<_>>()}),
    )
}
pub fn collect(s: &mut impl Source, id: &str, kind: Catalog) -> Result<Value> {
    match kind {
        Catalog::Plugins => plugins(s, id, false),
        Catalog::Marketplace => plugins(s, id, true),
        Catalog::Connectors => installed(s, id),
        Catalog::ConnectorMarketplace => connectors(s, id),
        Catalog::Skills => skills(s, id),
        Catalog::Tools => tools(s, id),
        Catalog::AccountLimits => {
            let value = s.request("account/rateLimits/read", json!({"threadId":id}))?;
            let number = |key: &str| value[key].as_u64();
            Ok(json!({
                "kind": kind.kind(), "thread_id": digest(id), "observed_at": now(),
                "weekly_used_percent": number("weekly_used_percent"),
                "weekly_resets_at": number("weekly_resets_at"),
                "banked_reset_available": number("banked_reset_available"),
                "banked_reset_expires_at": number("banked_reset_expires_at")
            }))
        }
    }
}
