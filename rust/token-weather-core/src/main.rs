use anyhow::{Result, anyhow, ensure};
use axum::{
    Json, Router,
    extract::{Query, State},
    http::{HeaderMap, StatusCode, header},
    response::{Html, IntoResponse, Response},
    routing::get,
};
use clap::{Parser, Subcommand};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{path::PathBuf, sync::Arc};
use token_weather_core::{
    forecast, now,
    owner::OwnerSpec,
    service::{self, Service},
};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt};

#[derive(Parser)]
#[command(version, about = "Token Weather local Rust runtime")]
struct Args {
    #[arg(long, global = true)]
    codex_home: Option<PathBuf>,
    #[arg(long, global = true)]
    isolated_root: Option<PathBuf>,
    /// Only for an explicitly isolated read-only preview. MCP uses its actual parent PID.
    #[arg(long, global = true)]
    owner_pid: Option<u32>,
    #[arg(long, global = true)]
    state_dir: Option<PathBuf>,
    #[command(subcommand)]
    command: Cmd,
}
#[derive(Subcommand)]
enum Cmd {
    Mcp,
    Serve {
        #[arg(long)]
        thread_id: Option<String>,
        #[arg(long, default_value_t = 0)]
        port: u16,
        #[arg(long)]
        demo: bool,
    },
    Snapshot {
        #[arg(long)]
        thread_id: String,
    },
    Forecast,
}
#[derive(Clone)]
struct Web {
    service: Service,
    thread: Option<String>,
    demo: bool,
    port: u16,
}
#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct ReadQuery {
    section: String,
    catalog: String,
    refresh: bool,
}
fn allowed(headers: &HeaderMap, port: u16) -> bool {
    let host = headers.get(header::HOST).and_then(|v| v.to_str().ok());
    let expected = format!("127.0.0.1:{port}");
    let local = format!("localhost:{port}");
    if host != Some(expected.as_str()) && host != Some(local.as_str()) {
        return false;
    }
    headers.get(header::ORIGIN).is_none_or(|v| {
        v.to_str()
            .is_ok_and(|o| o == format!("http://{expected}") || o == format!("http://{local}"))
    })
}
fn headers_response(body: impl IntoResponse) -> Response {
    let mut r = body.into_response();
    r.headers_mut()
        .insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    r.headers_mut()
        .insert(header::X_CONTENT_TYPE_OPTIONS, "nosniff".parse().unwrap());
    r
}
async fn host(State(w): State<Web>, h: HeaderMap) -> Response {
    if !allowed(&h, w.port) {
        return StatusCode::FORBIDDEN.into_response();
    }
    headers_response(Html(HOST.replace(
        "__LABEL__",
        if w.demo {
            "Demo data · not your chat usage"
        } else {
            "Local chat · read-only preview"
        },
    )))
}
async fn panel(State(w): State<Web>, h: HeaderMap) -> Response {
    if !allowed(&h, w.port) {
        return StatusCode::FORBIDDEN.into_response();
    }
    headers_response(Html(service::html()))
}
async fn reading(State(w): State<Web>, h: HeaderMap, Query(q): Query<ReadQuery>) -> Response {
    if !allowed(&h, w.port) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let result = if w.demo {
        demo(&q)
    } else if let Some(id) = w.thread {
        w.service
            .reading(
                id,
                q.section,
                if q.catalog.is_empty() {
                    "installed".into()
                } else {
                    q.catalog
                },
                q.refresh,
            )
            .await
    } else {
        Err(anyhow!("No bound thread"))
    };
    match result {
        Ok(v) => headers_response(Json(v)),
        Err(_) => headers_response((
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error":"Reading unavailable"})),
        )),
    }
}
const HOST: &str = r#"<!doctype html><html lang="en-AU"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Token Weather</title><style>html,body{margin:0;background:#191919;color:#aaa;font:11px system-ui}header{padding:9px 14px;border-bottom:1px solid #ffffff0d}iframe{display:block;width:100%;height:calc(100dvh - 34px);border:0}</style><header>__LABEL__</header><iframe title="Token Weather" src="/panel"></iframe><script>
const frame=document.querySelector('iframe');window.qaCalls=[];addEventListener('message',async event=>{if(event.source!==frame.contentWindow||!event.data.id)return;const m=event.data;qaCalls.push({method:m.method,params:m.params,at:performance.now()});if(qaCalls.length>300)qaCalls.shift();try{let result={};if(m.method==='ui/initialize')result={hostContext:{availableDisplayModes:['inline'],displayMode:'inline'}};else if(m.method==='ui/open-link'){document.querySelector('header').textContent='Navigation preview: '+m.params.url;result={isError:false};}else if(m.method==='tools/call'){if(m.params.name!=='token_weather_context')throw Error('Read-only preview');const a=m.params.arguments||{};const response=await fetch('/reading?'+new URLSearchParams({section:a.section||'',catalog:a.catalog||'installed',refresh:String(a.refresh||false)}));if(!response.ok)throw Error('Unavailable');result=await response.json();}event.source.postMessage({jsonrpc:'2.0',id:m.id,result},'*');}catch(_){event.source.postMessage({jsonrpc:'2.0',id:m.id,error:{code:-1,message:'Reading unavailable'}},'*');}});
</script></html>"#;
fn demo(q: &ReadQuery) -> Result<Value> {
    let time = now();
    let mut samples = vec![];
    let mut ends: Vec<u64> = (0..15)
        .map(|i| 240000 + i * 15000)
        .chain((0..21).map(|i| 110000 + i * 11975))
        .collect();
    ends.extend([353500, 357500, 361000, 364500, 367000]);
    for (i, tokens) in ends.into_iter().enumerate() {
        let values = if i == 40 {
            vec![365000, 366000, 367000]
        } else {
            vec![tokens]
        };
        let len = values.len();
        for (j, value) in values.into_iter().enumerate() {
            let output = if i == 40 { [1000, 500, 16000][j] } else { 500 };
            samples.push(json!({"turn":format!("demo-{i}"),"epoch":if i<15{0}else{1},"tokens":value,"at":time-(40-i)as f64*60.0-(len-1-j)as f64,"is_compaction":i==15,"is_turn_end":j==len-1,"output":output}));
        }
    }
    let id = "demo-weather";
    let mut r = json!({"schema_version":1,"thread_id":id,"available":true,"tokens":367000,"observed_window":1000000,"stale":false,"max_age_seconds":120,
        "last_request":{"at":time,"input":351000,"cached_input":240000,"cache_write_input":34000,"output":16000,"reasoning_output":9000},
        "session_usage":{"at":time,"total":12680000,"input":12460000,"cached_input":10800000,"cache_write_input":250000,"output":220000,"reasoning_output":130000},
        "api_cost_estimate":{"usd":6.725,"priced_tokens":12680000,"observed_tokens":12680000,"partial":false,"prices_as_of":"2026-11-07"},
        "runtime_supported":true,"skill_catalog_supported":true,"plugin_catalog_supported":true,"native_navigation_supported":true,"native_usage_supported":true,"tool_catalog_supported":true,
        "runtime":{"thread_id":id,"observed_at":time,"active":false,"compaction":{"supported":false},"auto_compact":{"kind":"effective_runtime_remaining","source":"same_owner_engine_compaction_trace","thread_id":id,"observed_at":time,"measured_at":time,"active_context_tokens":367000,"scope_tokens":367000,"scope_limit":967000,"full_context_limit":1000000,"remaining_tokens":600000}}});
    let report = json!({"samples":samples,"last_request":samples.last()});
    r["forecast"] = forecast::compute(&report, &r, time)?;
    // These catalog fixtures exist only in the explicitly labelled --demo HTTP
    // preview. Stdio MCP and real reading modes never invoke this function.
    let market = q.catalog == "marketplace";
    match q.section.as_str() {
        "usage" => {
            r["native_usage"] = json!({"kind":"codex_thread_usage","thread_id":id,"observed_at":time,"estimated_credits_micros":92160000});
        }
        "plugins" => {
            let names = if market {
                vec!["Notion", "Slack", "Linear"]
            } else {
                vec!["Token Weather", "Figma", "GitHub", "Google Drive"]
            };
            let rows:Vec<_>=names.iter().enumerate().map(|(i,n)|json!({"id":format!("demo-plugin-{i}"),"name":n,"title":n,"description":match *n{"Token Weather"=>"Context insights and five-turn forecasts","Figma"=>"Design files and component libraries","GitHub"=>"Repositories, pull requests and issues","Google Drive"=>"Documents and shared files",_=>"Explore this integration in the plugin directory"},"marketplace":"Demo catalog","enabled":true,"installed":!market,"native_url":"codex://skills"})).collect();
            let key = if market { "marketplace" } else { "installed" };
            let mut value = json!({"kind":"owner_plugin_catalog","thread_id":id,"observed_at":time,"workspace":"/demo","catalog":key});
            value[key] = json!(rows);
            r["plugins"] = value;
        }
        "connectors" => {
            let names = if market {
                vec!["Notion", "Slack", "Linear"]
            } else {
                vec!["Figma", "GitHub", "Google Drive"]
            };
            let rows:Vec<_>=names.iter().enumerate().map(|(i,n)|json!({"id":format!("connector_demo_{i}"),"name":n,"description":"Preview connection","enabled":true,"accessible":!market,"runtime_present":!market,"runtime_callable":!market,"native_url":"codex://skills"})).collect();
            r["connectors"] = json!({"kind":"native_connector_catalog","thread_id":id,"observed_at":time,"catalog":if market{"marketplace"}else{"installed"},"apps":rows});
        }
        "skills" => {
            r["skills"] = json!({"kind":"workspace_available","thread_id":id,"observed_at":time,"workspace":"/demo","count":2,"skills":[{"id":"weather","name":"token-weather","title":"Token Weather","description":"Understand this chat’s context usage","scope":"user","enabled":true},{"id":"review","name":"code-review","title":"Code review","description":"Review changes with focused validation","scope":"user","enabled":true}]});
        }
        "" | "tools" => {
            r["mcp_tools"] = json!({"kind":"owner_tool_catalog","thread_id":id,"observed_at":time,"count":3,"servers":[{"name":"github","count":2,"tools":["read_repository","search_code"]},{"name":"token_weather_context","count":1,"tools":["token_weather_context"]}],"server_details":[{"name":"github","title":"GitHub","transport":"web","status":"connected"},{"name":"token_weather_context","title":"Token Weather","transport":"local","status":"connected"}]});
        }
        _ => {}
    }

    Ok(json!({"_meta":{"reading":r}}))
}
async fn read_frame<R: tokio::io::AsyncBufRead + Unpin>(
    reader: &mut R,
    max: usize,
) -> Result<Option<Vec<u8>>> {
    let mut line = vec![];
    loop {
        let data = reader.fill_buf().await?;
        if data.is_empty() {
            return Ok(None);
        }
        let n = data
            .iter()
            .position(|b| *b == b'\n')
            .map_or(data.len(), |i| i + 1);
        ensure!(line.len() + n <= max, "MCP input frame too large");
        line.extend_from_slice(&data[..n]);
        reader.consume(n);
        if line.last() == Some(&b'\n') {
            return Ok(Some(line));
        }
    }
}
async fn stdio(service: Service) -> Result<()> {
    let (send, mut recv) = tokio::sync::mpsc::channel::<Value>(32);
    let writer = tokio::spawn(async move {
        let mut out = tokio::io::stdout();
        while let Some(v) = recv.recv().await {
            let mut bytes = serde_json::to_vec(&v)?;
            bytes.push(b'\n');
            out.write_all(&bytes).await?;
            out.flush().await?;
        }
        Ok::<_, anyhow::Error>(())
    });
    let slots = Arc::new(tokio::sync::Semaphore::new(16));
    let mut tasks = tokio::task::JoinSet::new();
    let mut input = tokio::io::BufReader::new(tokio::io::stdin());
    while let Some(line) = read_frame(&mut input, 64 * 1024).await? {
        let request: Value = match serde_json::from_slice(&line) {
            Ok(v) => v,
            Err(_) => {
                send.send(json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Invalid JSON"}})).await?;
                continue;
            }
        };
        let Some(id) = request.get("id").cloned() else {
            continue;
        };
        let Some(method) = request["method"].as_str().map(str::to_owned) else {
            send.send(json!({"jsonrpc":"2.0","id":id,"error":{"code":-32600,"message":"Invalid request"}})).await?;
            continue;
        };
        let params = request.get("params").cloned().unwrap_or(json!({}));
        let permit = slots.clone().acquire_owned().await?;
        let tx = send.clone();
        let server = service.clone();
        tasks.spawn(async move{let response=match server.dispatch(&method,&params).await{Ok(v)=>json!({"jsonrpc":"2.0","id":id,"result":v}),Err(_)=>json!({"jsonrpc":"2.0","id":id,"error":{"code":if ["initialize","ping","tools/list","tools/call","resources/list","resources/templates/list","resources/read"].contains(&method.as_str()){-32602}else{-32601},"message":"Request unavailable or invalid"}})};let _=tx.send(response).await;drop(permit);});
        while tasks.try_join_next().is_some() {}
    }
    while tasks.join_next().await.is_some() {}
    drop(send);
    writer.await??;
    Ok(())
}
#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    if matches!(args.command, Cmd::Forecast) {
        let mut input = tokio::io::BufReader::new(tokio::io::stdin());
        while let Some(line) = read_frame(&mut input, 512 * 1024).await? {
            let v: Value = serde_json::from_slice(&line)?;
            let out = forecast::compute(
                &v["report"],
                &v["reading"],
                v["now"].as_f64().unwrap_or_else(now),
            )?;
            println!("{}", out);
        }
        return Ok(());
    }
    let home = args
        .codex_home
        .or_else(|| std::env::var_os("CODEX_HOME").map(PathBuf::from))
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".codex")
        });
    let mcp = matches!(args.command, Cmd::Mcp);
    ensure!(
        !mcp || args.owner_pid.is_none(),
        "MCP owner PID must come from actual process ancestry"
    );
    let owner = if let Some(root) = args.isolated_root {
        Some(OwnerSpec::new(
            root,
            if mcp {
                (unsafe { libc::getppid() }) as u32
            } else {
                args.owner_pid
                    .ok_or_else(|| anyhow!("Explicit isolated preview owner required"))?
            },
        )?)
    } else {
        ensure!(args.owner_pid.is_none(), "Owner PID requires isolation");
        None
    };
    let service = Service::new(home, owner, args.state_dir)?;
    match args.command {
        Cmd::Mcp => stdio(service).await?,
        Cmd::Snapshot { thread_id } => println!(
            "{}",
            service
                .reading(thread_id, "runtime".into(), "installed".into(), false)
                .await?
        ),
        Cmd::Serve {
            thread_id,
            port,
            demo,
        } => {
            ensure!(
                demo || thread_id.is_some(),
                "A bound thread or --demo is required"
            );
            if let Some(id) = &thread_id {
                token_weather_core::thread_id(id)?;
            }
            let socket =
                tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port)).await?;
            let port = socket.local_addr()?.port();
            let state = Web {
                service,
                thread: thread_id,
                demo,
                port,
            };
            let router = Router::new()
                .route("/", get(host))
                .route("/panel", get(panel))
                .route("/reading", get(reading))
                .with_state(state);
            println!("http://127.0.0.1:{port}/");
            axum::serve(socket, router)
                .with_graceful_shutdown(async {
                    let _ = tokio::signal::ctrl_c().await;
                })
                .await?;
        }
        Cmd::Forecast => unreachable!(),
    }
    Ok(())
}
