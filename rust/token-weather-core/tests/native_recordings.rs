//! Opt-in checks of minimised recordings from the native isolated owner.
//! Run with TW_NATIVE_RECORDINGS pointing at private acceptance evidence.
use anyhow::{Result, anyhow};
use serde_json::{Value, json};
use std::{collections::BTreeSet, fs, path::PathBuf};
use token_weather_core::catalogs::{self, Source};

struct Recording {
    root: PathBuf,
    calls: Vec<String>,
}
impl Source for Recording {
    fn request(&mut self, method: &str, _: Value) -> Result<Value> {
        self.calls.push(method.into());
        let value: Value = serde_json::from_slice(&fs::read(
            self.root.join(format!("{}.json", method.replace('/', "-"))),
        )?)?;
        value
            .get("result")
            .cloned()
            .ok_or_else(|| anyhow!("Native read failed"))
    }
    fn workspace(&self) -> PathBuf {
        self.root.clone()
    }
}

#[test]
#[ignore = "requires fresh recordings from the real isolated Desktop"]
fn recorded_native_plugin_catalogs_preserve_exact_identity_and_state() {
    let mut source = Recording {
        root: std::env::var_os("TW_NATIVE_RECORDINGS")
            .expect("Recording directory")
            .into(),
        calls: vec![],
    };
    for market in [false, true] {
        let method = if market {
            "plugin/list"
        } else {
            "plugin/installed"
        };
        let raw = source.request(method, json!({})).unwrap();
        source.calls.clear();
        // This is a schema/projection replay, not a live-thread binding test.
        let projected = catalogs::plugins(&mut source, "recording-only", market).unwrap();
        assert_eq!(source.calls, [method]);
        let expected: BTreeSet<_> = raw["marketplaces"]
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|m| m["plugins"].as_array().unwrap())
            .filter(|p| {
                if market {
                    p["installed"] == false
                        && p["availability"] == "AVAILABLE"
                        && ["AVAILABLE", "INSTALLED_BY_DEFAULT"]
                            .contains(&p["installPolicy"].as_str().unwrap_or(""))
                } else {
                    p["installed"] == true
                }
            })
            .map(|p| {
                (
                    p["remotePluginId"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or(p["id"].as_str().unwrap())
                        .to_owned(),
                    p["enabled"].as_bool().unwrap(),
                )
            })
            .collect();
        let rows = projected[if market { "marketplace" } else { "installed" }]
            .as_array()
            .unwrap();
        let actual: BTreeSet<_> = rows
            .iter()
            .map(|p| {
                (
                    p["id"].as_str().unwrap().to_owned(),
                    p["enabled"].as_bool().unwrap(),
                )
            })
            .collect();
        assert_eq!(actual, expected);
        assert_eq!(rows.len(), actual.len());
        println!("{method}: {} exact native identities", actual.len());
    }
}

#[test]
#[ignore = "requires fresh recordings from the real isolated Desktop"]
fn recorded_native_connector_marketplace() {
    let mut source = Recording {
        root: std::env::var_os("TW_NATIVE_RECORDINGS")
            .expect("Recording directory")
            .into(),
        calls: vec![],
    };
    let value = catalogs::connectors(&mut source, "recording-only").unwrap();
    println!(
        "connector Marketplace: {} native identities",
        value["apps"].as_array().unwrap().len()
    );
}

#[test]
#[ignore = "requires a fresh same-owner MCP status recording"]
fn recorded_native_tools_preserve_all_names_and_unreported_state() {
    let mut source = Recording {
        root: std::env::var_os("TW_NATIVE_RECORDINGS")
            .expect("Recording directory")
            .into(),
        calls: vec![],
    };
    let raw = source.request("mcpServerStatus/list", json!({})).unwrap();
    let value = catalogs::tools(&mut source, "recording-only").unwrap();
    let expected: BTreeSet<_> = raw["data"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|s| {
            s["tools"].as_object().unwrap().values().map(move |t| {
                (
                    s["name"].as_str().unwrap().to_owned(),
                    t["name"].as_str().unwrap().to_owned(),
                )
            })
        })
        .collect();
    let actual: BTreeSet<_> = value["servers"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|s| {
            s["tools"].as_array().unwrap().iter().map(move |t| {
                (
                    s["name"].as_str().unwrap().to_owned(),
                    t.as_str().unwrap().to_owned(),
                )
            })
        })
        .collect();
    assert_eq!(expected, actual);
    assert_eq!(value["count"], actual.len());
    for row in raw["data"].as_array().unwrap() {
        let projected = value["server_details"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["name"] == row["name"])
            .unwrap();
        assert_eq!(
            projected["status"],
            if row["runtimeStatus"].is_null() {
                "unreported"
            } else {
                "connected"
            }
        );
    }
    println!(
        "{} servers, {} exact server/tool identities",
        value["servers"].as_array().unwrap().len(),
        actual.len()
    );
}
