//! Pinned, read-only adapter to the existing isolated Desktop owner relay.
//! Never launches, patches, signs, replaces or takes control of a host process.
use crate::{history::no_links, thread_id};
use anyhow::{Result, anyhow, bail, ensure};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{FileTypeExt, MetadataExt},
        io::AsRawFd,
        net::UnixStream,
    },
    path::PathBuf,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};
pub const ENGINE: &str =
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex";
pub const DESKTOP: &str = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT";
pub const VERSION: &str = "codex-cli 0.167.0-alpha.3.2";
const FRAME: usize = 4 * 1024 * 1024;
#[derive(Clone)]
pub struct OwnerSpec {
    pub root: PathBuf,
    pub engine_pid: u32,
    pub allowed: HashSet<String>,
    workspaces: HashMap<String, PathBuf>,
}
impl OwnerSpec {
    pub fn new(root: PathBuf, engine_pid: u32) -> Result<Self> {
        let root = fs::canonicalize(root)?;
        no_links(&root)?;
        let targets: Value = serde_json::from_reader(fs::File::open(
            root.join("owner-bridge-audit/targets.json"),
        )?)?;
        let mut allowed = HashSet::new();
        let mut workspaces = HashMap::new();
        for row in targets
            .as_array()
            .ok_or_else(|| anyhow!("Invalid enrolled roots"))?
        {
            let id = row["thread_id"]
                .as_str()
                .ok_or_else(|| anyhow!("Invalid enrolled root"))?;
            thread_id(id)?;
            ensure!(allowed.insert(id.to_owned()), "Duplicate enrolled root");
            let workspace = match row.get("workspace") {
                Some(value) => PathBuf::from(
                    value
                        .as_str()
                        .ok_or_else(|| anyhow!("Invalid enrolled workspace"))?,
                ),
                None => root.join("workspace"),
            };
            ensure!(
                workspace.is_absolute() && workspace.is_dir(),
                "Absolute enrolled workspace required"
            );
            no_links(&workspace)?;
            ensure!(
                fs::canonicalize(&workspace)? == workspace,
                "Canonical enrolled workspace required"
            );
            workspaces.insert(id.into(), workspace);
        }
        Ok(Self {
            root,
            engine_pid,
            allowed,
            workspaces,
        })
    }
    pub fn home(&self) -> PathBuf {
        self.root.join("desktop-home")
    }
    pub fn workspace(&self) -> PathBuf {
        self.root.join("workspace")
    }
    pub fn workspace_for(&self, id: &str) -> Option<&PathBuf> {
        self.workspaces.get(id)
    }
}
pub struct Owner {
    spec: OwnerSpec,
    stream: BufReader<UnixStream>,
    serial: u64,
    peer_pid: u32,
    pub instance: String,
    workspace: PathBuf,
}
fn process_info(pid: u32) -> Result<(u32, String)> {
    ensure!(pid >= 2, "Invalid owner process");
    let mut child = Command::new("/bin/ps")
        .args(["-p", &pid.to_string(), "-o", "ppid=,comm="])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let start = Instant::now();
    loop {
        if child.try_wait()?.is_some() {
            break;
        }
        if start.elapsed() > Duration::from_secs(2) {
            let _ = child.kill();
            let _ = child.wait();
            bail!("Process identity lookup timed out");
        }
        thread::sleep(Duration::from_millis(5));
    }
    let out = child.wait_with_output()?;
    ensure!(out.status.success(), "Owner process unavailable");
    let text = String::from_utf8(out.stdout)?;
    let s = text.trim();
    let split = s
        .find(char::is_whitespace)
        .ok_or_else(|| anyhow!("Invalid process identity"))?;
    Ok((s[..split].parse()?, s[split..].trim().to_owned()))
}
impl Owner {
    pub fn connect(spec: &OwnerSpec) -> Result<Self> {
        ensure!(
            cfg!(target_os = "macos"),
            "Isolated owner adapter is macOS-only"
        );
        let path = spec.root.join("owner-bridge/owner.sock");
        for (p, socket) in [
            (path.parent().unwrap().to_path_buf(), false),
            (path.clone(), true),
        ] {
            no_links(&p)?;
            let m = fs::symlink_metadata(p)?;
            ensure!(
                m.uid() == unsafe { libc::getuid() }
                    && m.mode() & 0o077 == 0
                    && (if socket {
                        m.file_type().is_socket()
                    } else {
                        m.is_dir()
                    }),
                "Private owner endpoint required"
            );
        }
        let stream = UnixStream::connect(path)?;
        stream.set_read_timeout(Some(Duration::from_secs(5)))?;
        stream.set_write_timeout(Some(Duration::from_secs(3)))?;
        let mut peer: libc::pid_t = 0;
        let mut len = std::mem::size_of::<libc::pid_t>() as libc::socklen_t;
        // macOS SDK: SOL_LOCAL=0, LOCAL_PEERPID=2. The PID is obtained from the OS.
        let status = unsafe {
            libc::getsockopt(
                stream.as_raw_fd(),
                0,
                2,
                (&mut peer as *mut libc::pid_t).cast(),
                &mut len,
            )
        };
        ensure!(
            status == 0 && peer >= 2 && len as usize == std::mem::size_of::<libc::pid_t>(),
            "Peer process identity unavailable"
        );
        let mut owner = Self {
            spec: spec.clone(),
            stream: BufReader::new(stream),
            serial: 0,
            peer_pid: peer as u32,
            instance: String::new(),
            workspace: spec.workspace(),
        };
        owner.check_identity()?;
        Ok(owner)
    }
    fn rpc(&mut self, method: &str, params: Value, timeout: Duration) -> Result<Value> {
        self.stream.get_ref().set_read_timeout(Some(timeout))?;
        self.serial += 1;
        let mut bytes =
            serde_json::to_vec(&json!({"id":self.serial,"method":method,"params":params}))?;
        bytes.push(b'\n');
        self.stream.get_mut().write_all(&bytes)?;
        let mut line = vec![];
        self.stream
            .by_ref()
            .take((FRAME + 1) as u64)
            .read_until(b'\n', &mut line)?;
        ensure!(
            line.len() <= FRAME && line.last() == Some(&b'\n'),
            "Incomplete or oversized owner frame"
        );
        let response: Value = serde_json::from_slice(&line)?;
        ensure!(
            response["id"].as_u64() == Some(self.serial) && response.get("error").is_none(),
            "Owner read unavailable"
        );
        response
            .get("result")
            .cloned()
            .ok_or_else(|| anyhow!("Owner result missing"))
    }
    pub fn check_identity(&mut self) -> Result<()> {
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        let v = self.rpc(
            "bridge/identity",
            json!({"nonce":nonce}),
            Duration::from_secs(3),
        )?;
        let (desktop, engine_path) = process_info(self.spec.engine_pid)?;
        let (parent, _) = process_info(self.peer_pid)?;
        let (_, desktop_path) = process_info(desktop)?;
        ensure!(
            v["bridge_pid"].as_u64() == Some(self.peer_pid as u64)
                && v["engine_pid"].as_u64() == Some(self.spec.engine_pid as u64)
                && v["desktop_pid"].as_u64() == Some(desktop as u64)
                && v["engine_version"] == VERSION
                && v["codex_home"] == self.spec.home().to_string_lossy().as_ref()
                && v["engine"] == ENGINE
                && v["nonce"] == nonce
                && parent == self.spec.engine_pid
                && engine_path == ENGINE
                && desktop_path == DESKTOP,
            "Owner identity mismatch"
        );
        let instance = v["instance"]
            .as_str()
            .filter(|s| s.len() == 32 && s.bytes().all(|b| b.is_ascii_hexdigit()))
            .ok_or_else(|| anyhow!("Invalid owner instance"))?;
        ensure!(
            self.instance.is_empty() || self.instance == instance,
            "Owner changed"
        );
        self.instance = instance.into();
        Ok(())
    }
    pub fn read(&mut self, method: &str, params: Value) -> Result<Value> {
        let map = params
            .as_object()
            .ok_or_else(|| anyhow!("Invalid parameters"))?;
        let allowed: &[&str] = match method {
            "thread/loaded/list" => &["cursor", "limit"],
            "thread/read" => {
                ensure!(
                    params["includeTurns"] == false,
                    "Metadata-only reads required"
                );
                &["threadId", "includeTurns"]
            }
            "account/usage/read" => {
                ensure!(
                    params["threadId"]
                        .as_str()
                        .is_some_and(|id| self.spec.allowed.contains(id)),
                    "Usage requires an explicitly enrolled root"
                );
                &["threadId"]
            }
            "account/rateLimits/read" => &["threadId"],
            "mcpServerStatus/list" => {
                ensure!(
                    params["detail"] == "toolsAndAuthOnly",
                    "Catalog detail required"
                );
                &["threadId", "detail", "limit", "cursor", "serverName"]
            }
            "app/list" => {
                ensure!(
                    params["forceRefetch"] == false,
                    "Forced remote refresh forbidden"
                );
                &["threadId", "cursor", "limit", "forceRefetch"]
            }
            "app/installed" => {
                ensure!(
                    params["forceRefresh"] == false,
                    "Forced remote refresh forbidden"
                );
                &["threadId", "forceRefresh"]
            }
            "skills/list" => {
                ensure!(
                    params["forceReload"] == false,
                    "Forced skill reload forbidden"
                );
                &["cwds", "forceReload"]
            }
            "plugin/installed" => &["cwds"],
            "plugin/list" => {
                ensure!(
                    params["forceRefetch"] == false,
                    "Forced remote refresh forbidden"
                );
                &["cwds", "forceRefetch"]
            }
            "bridge/runtime" => &["threadId"],
            _ => bail!("Only allowlisted read routes are available"),
        };
        ensure!(
            map.keys().all(|k| allowed.contains(&k.as_str())),
            "Unexpected read parameter"
        );
        if let Some(id) = params.get("threadId") {
            thread_id(id.as_str().ok_or_else(|| anyhow!("Missing exact thread"))?)?;
        } else {
            ensure!(
                [
                    "thread/loaded/list",
                    "skills/list",
                    "plugin/list",
                    "plugin/installed"
                ]
                .contains(&method),
                "Exact thread required"
            );
        }
        if method == "skills/list" || method.starts_with("plugin/") {
            ensure!(
                params["cwds"] == json!([self.workspace()]),
                "Outside isolated workspace"
            );
        }
        if let Some(limit) = params.get("limit") {
            ensure!(
                limit.as_u64().is_some_and(|n| (1..=100).contains(&n)),
                "Invalid page bound"
            );
        }
        if let Some(cursor) = params.get("cursor") {
            ensure!(
                cursor
                    .as_str()
                    .is_some_and(|s| !s.is_empty() && s.len() <= 4096),
                "Invalid cursor"
            );
        }
        self.rpc(
            method,
            params,
            Duration::from_secs(
                if method.starts_with("app/") || method.starts_with("plugin/") {
                    22
                } else {
                    5
                },
            ),
        )
    }
    pub fn require_root(&mut self, id: &str) -> Result<()> {
        let v = self.read("thread/read", json!({"threadId":id,"includeTurns":false}))?;
        let t = &v["thread"];
        ensure!(
            t["id"] == id
                && t.get("parentThreadId") == Some(&Value::Null)
                && t["cliVersion"] == VERSION.trim_start_matches("codex-cli ")
                && t["cwd"] == self.workspace().to_string_lossy().as_ref()
                && ["idle", "active"].contains(&t["status"]["type"].as_str().unwrap_or("")),
            "Loaded root identity mismatch"
        );
        Ok(())
    }
    pub fn require_scope(&mut self, id: &str) -> Result<()> {
        thread_id(id)?;
        self.workspace = self
            .spec
            .workspace_for(id)
            .cloned()
            .unwrap_or_else(|| self.spec.workspace());
        if !self.spec.allowed.contains(id) {
            let v = self.read("bridge/runtime", json!({"threadId":id}))?;
            ensure!(
                v["compaction"]["target_thread_id"] == id
                    && v["compaction"]["source"] != "introduced_owner_session",
                "Outside enrolled test roots"
            );
        }
        let mut cursor = None;
        let mut seen = HashSet::new();
        let mut loaded = false;
        let start = Instant::now();
        let mut complete = false;
        for _ in 0..10 {
            ensure!(
                start.elapsed() < Duration::from_secs(5),
                "Loaded-root lookup expired"
            );
            let mut p = json!({"limit":100});
            if let Some(c) = &cursor {
                p["cursor"] = json!(c);
            }
            let v = self.read("thread/loaded/list", p)?;
            let rows = v["data"]
                .as_array()
                .filter(|r| r.len() <= 100)
                .ok_or_else(|| anyhow!("Invalid root inventory"))?;
            loaded |= rows.iter().any(|v| v == id);
            match v["nextCursor"].as_str() {
                None if v["nextCursor"].is_null() => {
                    complete = true;
                    break;
                }
                Some(c) if !c.is_empty() && seen.insert(c.to_owned()) => {
                    cursor = Some(c.to_owned())
                }
                _ => bail!("Invalid root pagination"),
            }
        }
        ensure!(loaded && complete, "Exact root is not loaded");
        self.require_root(id)
    }
    pub fn compact(&mut self, id: &str, operation: &str) -> Result<Value> {
        thread_id(id)?;
        thread_id(operation)?;
        self.check_identity()?;
        let state = self.read("bridge/runtime", json!({"threadId":id}))?;
        let source = state["compaction"]["source"].as_str();
        ensure!(
            state["thread_id"] == crate::digest(id)
                && state["compaction"]["target_thread_id"] == id
                && matches!(
                    source,
                    Some("enrolled_owner_session" | "introduced_owner_session")
                )
                && state["compaction"]["supported"] == true
                && state["active"] == false,
            "Session control unavailable"
        );
        if source == Some("enrolled_owner_session") {
            self.require_scope(id)?;
        }
        // The owner reserves and deduplicates the operation before the write.
        let result = self.rpc(
            "bridge/compact-session",
            json!({"threadId":id,"operation_id":operation}),
            Duration::from_secs(5),
        )?;
        ensure!(
            result["target_thread_id"] == id,
            "Control response binding mismatch"
        );
        self.check_identity()?;
        Ok(result)
    }
    pub fn reconnect(&mut self, id: &str) -> Result<()> {
        let mut next = Self::connect(&self.spec)?;
        ensure!(next.instance == self.instance, "Owner replaced during read");
        next.require_scope(id)?;
        *self = next;
        Ok(())
    }
    pub fn workspace(&self) -> PathBuf {
        self.workspace.clone()
    }
}
