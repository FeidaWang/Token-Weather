use anyhow::{Result, anyhow};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    fs,
    io::Write,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use token_weather_core::{
    cache::{CatalogCache, Key},
    catalogs::{self, Catalog, Source},
    digest, forecast,
    history::{History, HistoryStore},
    now,
};
const A: &str = "00000000-0000-4000-8000-000000000001";
const B: &str = "00000000-0000-4000-8000-000000000002";
#[test]
fn owner_enrollment_keeps_each_root_bound_to_its_explicit_workspace() {
    let f = Fixture::new();
    fs::create_dir(f.dir.join("workspace")).unwrap();
    fs::create_dir(f.dir.join("projectless")).unwrap();
    fs::create_dir(f.dir.join("owner-bridge-audit")).unwrap();
    let path = f.dir.join("owner-bridge-audit/targets.json");
    fs::write(
        &path,
        json!([{"thread_id":A},{"thread_id":B,"workspace":f.dir.join("projectless")}]).to_string(),
    )
    .unwrap();
    let spec = token_weather_core::owner::OwnerSpec::new(f.dir.clone(), 42).unwrap();
    assert_eq!(spec.workspace_for(A), Some(&f.dir.join("workspace")));
    assert_eq!(spec.workspace_for(B), Some(&f.dir.join("projectless")));
    assert!(spec.workspace_for("unknown").is_none());
    for rows in [
        json!([{"thread_id":A},{"thread_id":A}]),
        json!([{"thread_id":A,"workspace":"relative"}]),
    ] {
        fs::write(&path, rows.to_string()).unwrap();
        assert!(token_weather_core::owner::OwnerSpec::new(f.dir.clone(), 42).is_err());
    }
}
struct Fixture {
    dir: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let dir = std::env::temp_dir().join(format!("weather-rust-test-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&dir).unwrap();
        Self {
            dir: fs::canonicalize(dir).unwrap(),
        }
    }
    fn file(&self) -> PathBuf {
        self.dir.join("sessions").join(format!("rollout-{A}.jsonl"))
    }
    fn write(&self, rows: &[Value]) {
        fs::create_dir_all(self.dir.join("sessions")).unwrap();
        fs::write(
            self.file(),
            rows.iter().map(|r| format!("{r}\n")).collect::<String>(),
        )
        .unwrap();
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}
fn header(id: &str) -> Value {
    json!({"type":"session_meta","payload":{"id":id,"cwd":"/test","source":"cli","cli_version":"0.167.0-alpha.3.2"}})
}
fn turn(id: &str) -> Value {
    json!({"type":"turn_context","timestamp":"2026-11-06T00:00:00Z","payload":{"turn_id":id,"model":"test","effort":"high"}})
}
fn usage(n: u64, request: u64) -> Value {
    json!({"type":"event_msg","timestamp":"2026-11-06T00:00:01Z","payload":{"type":"token_count","info":{"model_context_window":100000,"total_token_usage":{"total_tokens":request},"last_token_usage":{"input_tokens":n-5,"output_tokens":5,"cached_input_tokens":0,"reasoning_output_tokens":0,"total_tokens":n}}}})
}
#[test]
fn session_totals_use_engine_snapshots_and_survive_compaction() {
    let f = Fixture::new();
    let mut event = usage(100, 100);
    let info = &mut event["payload"]["info"];
    info["last_token_usage"]["cache_write_input_tokens"] = json!(12);
    info["total_token_usage"] = info["last_token_usage"].clone();
    let ledger = |id, input, cached, total| {
        json!({"type":"token_usage_record","timestamp":"2026-11-06T00:00:02Z","payload":{
            "thread_id":id,"thread_token_usage":{"input_tokens":input,"output_tokens":10,
            "cached_input_tokens":cached,"cache_write_input_tokens":24,"reasoning_output_tokens":3,"total_tokens":total}
        }})
    };
    f.write(&[
        header(A),
        turn(A),
        event.clone(),
        event,
        ledger(A, 185, 40, 195),
    ]);
    let mut store = HistoryStore::default();
    let (r, _) = store.read(&f.dir, A, now()).unwrap();
    assert_eq!(r["tokens"], 100);
    assert_eq!(r["last_request"]["cache_write_input"], 12);
    assert_eq!(r["samples"].as_array().unwrap().len(), 1);
    assert_eq!(r["session_usage"]["total"], 195);
    let append = [
        ledger(B, 800, 500, 810),
        ledger(A, 900, 890, 910), // invalid overlapping read/write counters
        json!({"type":"compacted","payload":{}}),
        usage(50, 200), // legacy total has no detailed counters
    ];
    let mut file = fs::OpenOptions::new().append(true).open(f.file()).unwrap();
    for row in append {
        writeln!(file, "{row}").unwrap();
    }
    let (r, c) = store.read(&f.dir, A, now()).unwrap();
    assert_eq!(c["cache_reused"], true);
    assert_eq!(r["tokens"], 50);
    assert!(r["last_request"].get("cache_write_input").is_none());
    assert_eq!(r["session_usage"]["total"], 195);
    assert_eq!(r["session_usage"]["cache_write_input"], 24);
}

#[test]
fn api_cost_uses_per_request_models_and_skips_unattributed_usage() {
    const LATE: &str = "00000000-0000-4000-8000-000000000003";
    let f = Fixture::new();
    let mut meta = header(A);
    meta["payload"]["model_provider"] = json!("openai");
    let model_turn = |id, model| {
        let mut row = turn(id);
        row["payload"]["model"] = json!(model);
        row
    };
    let tokens = |input, cached, written, output| {
        json!({"input_tokens":input,"cached_input_tokens":cached,
            "cache_write_input_tokens":written,"output_tokens":output,
            "reasoning_output_tokens":output,"total_tokens":input+output})
    };
    let ledger = |id, req, sum| {
        json!({"type":"token_usage_record","timestamp":"2026-11-06T00:00:02Z",
            "payload":{"thread_id":A,"turn_id":id,"usage":req,"thread_token_usage":sum}})
    };
    let first = tokens(272_000, 100_000, 20_000, 1000);
    let duplicate = json!({"type":"event_msg","timestamp":"2026-11-06T00:00:02Z",
        "payload":{"type":"token_count","info":{"model_context_window":1_000_000,
        "last_token_usage":first,"total_token_usage":first}}});
    let long = tokens(300_000, 100_000, 30_000, 2000);
    let sum = tokens(572_000, 200_000, 50_000, 3000);
    f.write(&[
        meta,
        model_turn(A, "gpt-6.1-sol"),
        ledger(A, first.clone(), first),
        duplicate.clone(),
        model_turn(B, "gpt-6.1-terra"),
        ledger(B, long, sum),
    ]);
    let mut store = HistoryStore::default();
    let (reading, _) = store.read(&f.dir, A, now()).unwrap();
    let estimate = &reading["api_cost_estimate"];
    assert_eq!(estimate["priced_tokens"], 575_000);
    assert_eq!(estimate["partial"], false);
    // Sol at the threshold: $0.374. Terra above the threshold: $0.440.
    assert!((estimate["usd"].as_f64().unwrap() - 0.814).abs() < 1e-9);

    let mut file = fs::OpenOptions::new().append(true).open(f.file()).unwrap();
    let unknown = ledger(
        LATE,
        tokens(1000, 0, 0, 5),
        tokens(573_000, 200_000, 50_000, 3005),
    );
    writeln!(file, "{unknown}").unwrap();
    // Older aggregates can be emitted after the newest request ledger record.
    let mut trailing = duplicate;
    trailing["timestamp"] = json!("2026-11-06T00:00:03Z");
    writeln!(file, "{trailing}").unwrap();
    let (reading, _) = store.read(&f.dir, A, now()).unwrap();
    let estimate = &reading["api_cost_estimate"];
    assert_eq!(estimate["priced_tokens"], 575_000);
    assert_eq!(estimate["observed_tokens"], 576_005);
    assert_eq!(
        reading["session_usage"]["total"],
        estimate["observed_tokens"]
    );
    assert_eq!(estimate["partial"], true);
    assert!(estimate["usd"].is_null());
    // A new model cannot price a request from a different turn.
    writeln!(file, "{}", model_turn(B, "gpt-6.1-sol")).unwrap();
    let (reading, _) = store.read(&f.dir, A, now()).unwrap();
    assert!(reading["api_cost_estimate"]["usd"].is_null());

    // Resolve the late record from its own model, even across incremental reads.
    writeln!(file, "{}", model_turn(LATE, "gpt-6.1-terra")).unwrap();
    writeln!(
        file,
        "{{\"type\":\"response_item\",\"payload\":\"{}\"}}",
        "x".repeat(270_000)
    )
    .unwrap();
    let (reading, _) = store.read(&f.dir, A, now()).unwrap();
    let estimate = &reading["api_cost_estimate"];
    assert_eq!(estimate["priced_tokens"], 576_005);
    assert_eq!(estimate["partial"], false);
    assert!((estimate["usd"].as_f64().unwrap() - 0.815025).abs() < 1e-9);
}
fn complete(id: &str) -> Value {
    json!({"type":"event_msg","payload":{"type":"task_complete","turn_id":id}})
}
#[test]
fn history_deduplicates_and_retains_reported_total() {
    let mut h = History::default();
    h.consume(&turn(A)).unwrap();
    h.consume(&usage(100, 1)).unwrap();
    h.consume(&usage(100, 1)).unwrap();
    h.consume(&complete(A)).unwrap();
    assert_eq!(h.samples.len(), 1);
    assert!(h.samples[0].is_turn_end);
    let mut u = usage(150, 2);
    u["payload"]["info"]["last_token_usage"]["input_tokens"] = json!(0);
    u["payload"]["info"]["last_token_usage"]["output_tokens"] = json!(0);
    h.consume(&u).unwrap();
    assert_eq!(h.samples.back().unwrap().tokens, 150);
}
#[test]
fn aborted_turn_does_not_become_a_completed_forecast_sample() {
    let mut h = History::default();
    h.consume(&turn(A)).unwrap();
    h.consume(&usage(10000, 1)).unwrap();
    h.consume(&complete(A)).unwrap();
    h.consume(&turn(B)).unwrap();
    h.consume(&usage(12000, 2)).unwrap();
    h.consume(&json!({"type":"event_msg","payload":{"type":"turn_aborted","turn_id":B}}))
        .unwrap();
    assert!(!h.samples.back().unwrap().is_turn_end);
    let report = h.report(now());
    let reading =
        json!({"thread_id":"bound","tokens":12000,"observed_window":100000,"stale":false});
    let forecast = forecast::compute(&report, &reading, now()).unwrap();
    assert!(forecast["growth_per_turn"].is_null());
    assert_eq!(forecast["outlook"], json!([]));
}
#[test]
fn history_resets_on_compaction_and_setting_change() {
    let mut h = History::default();
    h.consume(&turn(A)).unwrap();
    h.consume(&usage(100, 1)).unwrap();
    h.consume(&json!({"type":"compacted","payload":{}}))
        .unwrap();
    h.consume(&json!({"type":"event_msg","payload":{"type":"context_compacted"}}))
        .unwrap();
    assert_eq!(h.epoch, 1);
    assert!(!h.has_current);
    h.consume(&usage(50, 2)).unwrap();
    assert!(h.samples.back().unwrap().is_compaction);
    let mut t = turn(B);
    t["payload"]["effort"] = json!("low");
    h.consume(&t).unwrap();
    assert_eq!(h.epoch, 2);
    assert!(!h.has_current);
}
#[test]
fn history_growth_decline_is_not_a_proven_compaction() {
    let mut h = History::default();
    h.consume(&turn(A)).unwrap();
    h.consume(&usage(100, 1)).unwrap();
    h.consume(&usage(90, 2)).unwrap();
    assert_eq!(h.epoch, 1);
    assert!(!h.samples.back().unwrap().is_compaction);
}
#[test]
fn incremental_reader_partial_append_restart_and_privacy() {
    let f = Fixture::new();
    f.write(&[
        header(A),
        turn(A),
        usage(100, 1),
        json!({"type":"response_item","payload":{"message":"PRIVATE_TEXT_MUST_NOT_PERSIST"}}),
    ]);
    let mut store = HistoryStore::new(Some(f.dir.join("numeric-cache")));
    let (r, c) = store.read(&f.dir, A, now()).unwrap();
    assert_eq!(r["tokens"], 100);
    assert!(c["bytes_read"].as_u64().unwrap() > 0);
    let (_, c) = store.read(&f.dir, A, now()).unwrap();
    assert_eq!(c["bytes_read"], 0);
    let row = format!("{}\n", usage(120, 2));
    let mut out = fs::OpenOptions::new().append(true).open(f.file()).unwrap();
    out.write_all(&row.as_bytes()[..10]).unwrap();
    let (r, _) = store.read(&f.dir, A, now()).unwrap();
    assert_eq!(r["tokens"], 100);
    out.write_all(&row.as_bytes()[10..]).unwrap();
    let (r, _) = store.read(&f.dir, A, now()).unwrap();
    assert_eq!(r["tokens"], 120);
    let mut restart = HistoryStore::new(Some(f.dir.join("numeric-cache")));
    let (r, c) = restart.read(&f.dir, A, now()).unwrap();
    assert_eq!(r["tokens"], 120);
    assert_eq!(c["cache_reused"], true);
    let text = fs::read_to_string(
        f.dir
            .join("numeric-cache")
            .join(format!("history-{}.json", digest(A))),
    )
    .unwrap();
    assert!(!text.contains("PRIVATE_TEXT"));
}
#[test]
fn replaced_or_rewritten_transcript_invalidates_cache() {
    let f = Fixture::new();
    f.write(&[header(A), turn(A), usage(100, 1)]);
    let mut store = HistoryStore::default();
    store.read(&f.dir, A, now()).unwrap();
    f.write(&[header(A), turn(A), usage(200, 2)]);
    let (r, c) = store.read(&f.dir, A, now()).unwrap();
    assert_eq!(r["tokens"], 200);
    assert_eq!(c["cache_reused"], false);
    fs::remove_file(f.file()).unwrap();
    f.write(&[header(A), turn(A), usage(300, 3)]);
    let (r, c) = store.read(&f.dir, A, now()).unwrap();
    assert_eq!(r["tokens"], 300);
    assert_eq!(c["cache_reused"], false);
}
#[test]
fn rejects_foreign_subagent_and_unverified_source() {
    let f = Fixture::new();
    let mut store = HistoryStore::default();
    for h in [
        header(B),
        {
            let mut h = header(A);
            h["payload"]["parent_thread_id"] = json!(B);
            h
        },
        {
            let mut h = header(A);
            h["payload"]["cli_version"] = json!("unverified");
            h
        },
    ] {
        f.write(&[h, turn(A), usage(100, 1)]);
        assert!(store.read(&f.dir, A, now()).is_err());
    }
}
#[test]
fn rejects_symlink_transcript() {
    let f = Fixture::new();
    fs::create_dir(f.dir.join("sessions")).unwrap();
    let other = f.dir.join("outside.jsonl");
    fs::write(&other, format!("{}\n", header(A))).unwrap();
    std::os::unix::fs::symlink(other, f.file()).unwrap();
    assert!(HistoryStore::default().read(&f.dir, A, now()).is_err());
}
#[test]
fn sample_retention_is_bounded() {
    let mut h = History::default();
    h.consume(&turn(A)).unwrap();
    for i in 10..510 {
        h.consume(&usage(i, i)).unwrap();
    }
    assert_eq!(h.samples.len(), 400);
}
fn sample(id: &str, n: u64, done: bool) -> Value {
    json!({"turn":id,"epoch":0,"tokens":n,"at":100,"is_turn_end":done,"output":0})
}
#[test]
fn forecast_uses_completed_turns_and_request_intervals() {
    let samples = json!([
        sample(A, 10000, true),
        sample(B, 11000, false),
        sample(B, 12000, false),
        sample(B, 13000, true)
    ]);
    let r = json!({"samples":samples,"last_request":samples[3]});
    let reading =
        json!({"thread_id":"bound","tokens":13000,"observed_window":100000,"stale":false});
    let f = forecast::compute(&r, &reading, 100.0).unwrap();
    assert_eq!(f["growth_per_turn"], 3000.0);
    assert_eq!(f["growth_per_request"], 1000.0);
    assert_eq!(f["last_turn_requests"], 3);
    assert!(f["turns_left"].is_null());
    assert!(f.get("session_cost").is_none());
}
#[test]
fn forecast_budget_requires_same_owner_counter_and_freshness() {
    let samples = json!([sample(A, 10000, true), sample(B, 12000, true)]);
    let r = json!({"samples":samples,"last_request":samples[1]});
    let budget = json!({"thread_id":"bound","observed_at":100,"kind":"effective_runtime_remaining","source":"same_owner_engine_compaction_trace","active_context_tokens":12000,"scope_tokens":12000,"scope_limit":90000,"full_context_limit":100000});
    let mut reading = json!({"thread_id":"bound","tokens":12000,"observed_window":100000,"stale":false,"runtime":{"thread_id":"bound","observed_at":100,"auto_compact":budget}});
    assert_eq!(
        forecast::compute(&r, &reading, 100.0).unwrap()["turns_left"],
        39
    );
    reading["runtime"]["auto_compact"]["scope_tokens"] = json!(6000);
    assert!(forecast::compute(&r, &reading, 100.0).unwrap()["turns_left"].is_null());
    reading["stale"] = json!(true);
    assert_eq!(
        forecast::compute(&r, &reading, 100.0).unwrap()["outlook"],
        json!([])
    );
}
struct Mock {
    replies: VecDeque<Result<Value>>,
    calls: Vec<String>,
}
impl Mock {
    fn values(v: Vec<Value>) -> Self {
        Self {
            replies: v.into_iter().map(Ok).collect(),
            calls: vec![],
        }
    }
}
impl Source for Mock {
    fn request(&mut self, m: &str, _p: Value) -> Result<Value> {
        self.calls.push(m.into());
        self.replies
            .pop_front()
            .unwrap_or_else(|| Err(anyhow!("unexpected request")))
    }
    fn workspace(&self) -> PathBuf {
        PathBuf::from("/isolated/workspace")
    }
}
fn plugin(n: &str, installed: bool) -> Value {
    json!({"name":n,"id":format!("{n}@market"),"installed":installed,"enabled":true,"availability":"AVAILABLE","installPolicy":"AVAILABLE","private":"SECRET"})
}
#[test]
fn installed_plugins_never_query_marketplace() {
    let response = json!({"marketplaces":[{"name":"market","plugins":[plugin("one",true),plugin("two",false)]}],"marketplaceLoadErrors":[]});
    let mut s = Mock::values(vec![response]);
    let v = catalogs::plugins(&mut s, A, false).unwrap();
    assert_eq!(s.calls, vec!["plugin/installed"]);
    assert_eq!(v["installed"].as_array().unwrap().len(), 1);
    assert!(v.get("marketplace").is_none());
    assert!(!v.to_string().contains("SECRET"));
}
#[test]
fn installed_connectors_never_query_plugin_or_app_marketplace() {
    let mut s = Mock::values(vec![
        json!({"apps":[{"id":"connector_one","runtimeName":"One","enabled":true,"callable":true}]}),
    ]);
    let v = catalogs::installed(&mut s, A).unwrap();
    assert_eq!(s.calls, vec!["app/installed"]);
    assert_eq!(v["apps"][0]["name"], "One");
}
#[test]
fn tool_catalog_accepts_reported_null_state_without_claiming_connection() {
    let row = json!({"name":"plugin","runtimeStatus":null,"toolsError":null,"httpOrigin":null,
        "tools":{"read":{"name":"read"}}});
    let mut source = Mock::values(vec![json!({"data":[row],"nextCursor":null})]);
    let result = catalogs::tools(&mut source, A).unwrap();
    assert_eq!(result["count"], 1);
    assert_eq!(result["server_details"][0]["status"], "unreported");
    assert_eq!(result["servers"][0]["tools"], json!(["read"]));
    for bad in [
        {
            let mut r = row.clone();
            r["runtimeStatus"] = json!("failed");
            r
        },
        {
            let mut r = row.clone();
            r["toolsError"] = json!("discovery failed");
            r
        },
        {
            let mut r = row.clone();
            r.as_object_mut().unwrap().remove("runtimeStatus");
            r
        },
    ] {
        let mut source = Mock::values(vec![json!({"data":[bad],"nextCursor":null})]);
        assert!(catalogs::tools(&mut source, A).is_err());
    }
}
#[test]
fn incomplete_or_conflicting_catalog_is_not_empty_success() {
    let mut s = Mock::values(vec![
        json!({"marketplaces":[],"marketplaceLoadErrors":["bad"]}),
    ]);
    assert!(catalogs::plugins(&mut s, A, false).is_err());
    let one = plugin("one", true);
    let mut two = one.clone();
    two["enabled"] = json!(false);
    let mut s = Mock::values(vec![
        json!({"marketplaces":[{"name":"market","plugins":[one,two]}],"marketplaceLoadErrors":[]}),
    ]);
    assert!(catalogs::plugins(&mut s, A, false).is_err());
}
#[test]
fn native_urls_reject_credentials_unrelated_ids_and_query_parameters() {
    let app = "connector_one";
    let good = "https://chatgpt.com/apps/one/connector_one";
    assert_eq!(catalogs::install_url(&json!(good), app), Some(good.into()));
    for bad in [
        format!("{good}?token=secret"),
        good.replace("chatgpt.com", "evil.example"),
        good.replace("chatgpt.com", "chatgpt.com:443"),
        good.replace("connector_one", "connector_two"),
        "https://chatgpt.com/oauth/authorize".into(),
    ] {
        assert_eq!(catalogs::install_url(&json!(bad), app), None);
    }
    assert!(catalogs::plugin_url(&json!("../../settings")).is_none());
}
#[test]
fn connector_bulk_and_page_cursor_conflicts() {
    let mut s = Mock::values(vec![
        json!({"marketplaces":[],"marketplaceLoadErrors":[]}),
        json!({"apps":[]}),
        json!({"data":[],"nextCursor":"loop"}),
        json!({"data":[],"nextCursor":"loop"}),
    ]);
    assert!(catalogs::connectors(&mut s, A).is_err());
}
#[tokio::test]
async fn cache_deduplicates_and_reserves_fast_capacity() {
    let cache = CatalogCache::default();
    let slow = Key {
        owner: "one".into(),
        thread: A.into(),
        catalog: Catalog::Marketplace,
    };
    let fast = Key {
        catalog: Catalog::Plugins,
        ..slow.clone()
    };
    let calls = Arc::new(AtomicUsize::new(0));
    let c = calls.clone();
    cache.read(slow.clone(), false, move || {
        c.fetch_add(1, Ordering::SeqCst);
        std::thread::sleep(Duration::from_millis(150));
        Ok(json!({"thread_id":digest(A),"observed_at":now(),"marketplace":[]}))
    });
    for _ in 0..5 {
        assert_eq!(
            cache.read(slow.clone(), false, || panic!("duplicate"))["state"],
            "loading"
        );
    }
    cache.read(fast.clone(), false, || {
        Ok(json!({"thread_id":digest(A),"observed_at":now(),"installed":[]}))
    });
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert!(
        cache
            .read(fast, false, || panic!("duplicate"))
            .get("installed")
            .is_some()
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}
#[tokio::test]
async fn cache_keys_include_owner_and_thread_and_errors_are_redacted() {
    let c = CatalogCache::default();
    let k = Key {
        owner: "one".into(),
        thread: A.into(),
        catalog: Catalog::Plugins,
    };
    c.read(k.clone(), false, || Err(anyhow!("PRIVATE")));
    tokio::time::sleep(Duration::from_millis(20)).await;
    let failed = c.read(k.clone(), false, || panic!());
    assert_eq!(failed["state"], "failed");
    assert!(!failed.to_string().contains("PRIVATE"));
    let other = Key {
        thread: B.into(),
        ..k
    };
    assert_eq!(
        c.read(other, false, || Ok(
            json!({"thread_id":digest(B),"observed_at":now(),"installed":[]})
        ))["state"],
        "loading"
    );
}
#[tokio::test]
async fn tool_refresh_retains_fresh_snapshot_without_renewing_its_timestamp() {
    let cache = CatalogCache::default();
    let key = Key {
        owner: "owner".into(),
        thread: A.into(),
        catalog: Catalog::Tools,
    };
    let at = now() - 11.0;
    cache.read(key.clone(), false, move || {
        Ok(json!({"thread_id":digest(A),"observed_at":at,"count":1}))
    });
    tokio::time::timeout(Duration::from_secs(1), async {
        while cache.ready(&key, 15.0).is_none() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let (send, receive) = std::sync::mpsc::channel();
    assert_eq!(
        cache.read(key.clone(), false, move || {
            receive.recv().unwrap();
            Err(anyhow!("failed refresh"))
        })["state"],
        "loading"
    );
    assert_eq!(cache.ready(&key, 15.0).unwrap()["observed_at"], at);
    assert!(cache.ready(&key, 10.0).is_none());
    assert!(
        cache
            .ready(
                &Key {
                    owner: "another".into(),
                    ..key.clone()
                },
                15.0
            )
            .is_none()
    );
    send.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            let state = cache.read(key.clone(), false, || panic!("unrequested retry"));
            if state["state"] == "failed" {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(cache.ready(&key, 15.0).unwrap()["observed_at"], at);
}

#[test]
fn sampling_threshold_preserves_prefix_and_is_labelled_separately() {
    let reading = json!({"thread_id":"bound","tokens":30000,"observed_window":100000,"stale":false,
        "runtime":{"thread_id":"bound","observed_at":200,"auto_compact":{
            "thread_id":"bound","observed_at":200,"kind":"sampling_runtime_remaining",
            "source":"same_owner_engine_compaction_trace","active_context_tokens":30000,
            "scope":"body_after_prefix","prefill_tokens":20000,"scope_tokens":10000,
            "scope_limit":80000,"full_context_limit":100000,"remaining_tokens":70000}}});
    let report = json!({"samples":[],"last_request":{}});
    let value = forecast::compute(&report, &reading, 200.0).unwrap();
    assert_eq!(value["reference_kind"], "sampling_auto_compact");
    assert_eq!(value["reference_limit"], 100000);
    assert_eq!(value["remaining_tokens"], 70000);
    let mut mismatch = reading.clone();
    mismatch["runtime"]["auto_compact"]["scope_tokens"] = json!(10001);
    assert_eq!(
        forecast::compute(&report, &mismatch, 200.0).unwrap()["reference_kind"],
        "observed_window"
    );
}
