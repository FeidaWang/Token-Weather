//! Incremental, bounded numeric-only rollout reader. No transcript text is stored.
use crate::{api_cost, digest, forecast::Sample, thread_id};
use anyhow::{Result, anyhow, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, VecDeque},
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};
const LINE: usize = 256 * 1024;
const BUDGET: u64 = 8 * 1024 * 1024;
const CACHE_VERSION: u8 = 5;
const MAX_PENDING_COST_REQUESTS: usize = 64;
const VERSIONS: &[&str] = &[
    "0.160.0",
    "0.162.0-alpha.17.2",
    "0.163.0-alpha.10.2",
    "0.167.0-alpha.3.2",
];
#[derive(Clone, Debug, Serialize, Deserialize)]
struct RecordedUsage {
    at: f64,
    total: u64,
    input: u64,
    output: u64,
    cached_input: u64,
    cache_write_input: Option<u64>,
    reasoning_output: u64,
}
impl RecordedUsage {
    fn parse(usage: &Value, at: f64) -> Option<Self> {
        let n = |key: &str| usage[key].as_u64();
        let result = Self {
            at,
            total: n("total_tokens")?,
            input: n("input_tokens")?,
            output: n("output_tokens")?,
            cached_input: n("cached_input_tokens")?,
            cache_write_input: n("cache_write_input_tokens"),
            reasoning_output: n("reasoning_output_tokens")?,
        };
        result.valid().then_some(result)
    }
    fn valid(&self) -> bool {
        self.at.is_finite()
            && self.at >= 0.0
            && self.total <= 9_007_199_254_740_991 // safe integer in the panel
            && self.input.checked_add(self.output) == Some(self.total)
            && self.cached_input <= self.input
            && self.cache_write_input.is_none_or(|w| w <= self.input - self.cached_input)
            && self.reasoning_output <= self.output
    }

    fn accounts_for(&self, request: &Self, previous: Option<&Self>) -> bool {
        let after = [
            self.input,
            self.cached_input,
            self.output,
            self.reasoning_output,
        ];
        let before = previous
            .map(|p| [p.input, p.cached_input, p.output, p.reasoning_output])
            .unwrap_or_default();
        let used = [
            request.input,
            request.cached_input,
            request.output,
            request.reasoning_output,
        ];
        if !after
            .into_iter()
            .zip(before)
            .zip(used)
            .all(|((a, b), n)| a.checked_sub(b) == Some(n))
        {
            return false;
        }
        let previous_writes = previous.map_or(Some(0), |p| p.cache_write_input);
        match (
            self.cache_write_input,
            previous_writes,
            request.cache_write_input,
        ) {
            (Some(current), Some(prior), Some(written)) => {
                current.checked_sub(prior) == Some(written)
            }
            _ => false,
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct ApiEstimate {
    model: Option<api_cost::Model>,
    model_turn: Option<String>,
    openai_provider: Option<bool>,
    accounted: Option<RecordedUsage>,
    pending: VecDeque<PendingCostRequest>,
    priced_tokens: u64,
    usd: f64,
    partial: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct PendingCostRequest {
    turn: String,
    usage: RecordedUsage,
}

impl ApiEstimate {
    fn valid(&self) -> bool {
        self.usd.is_finite()
            && self.usd >= 0.0
            && self.accounted.as_ref().is_none_or(RecordedUsage::valid)
            && self.pending.len() <= MAX_PENDING_COST_REQUESTS
            && self.pending.iter().all(|pending| {
                thread_id(&pending.turn).is_ok()
                    && pending.usage.valid()
                    && pending.usage.cache_write_input.is_some()
            })
            && self
                .priced_tokens
                .checked_add(self.pending.iter().map(|p| p.usage.total).sum())
                .is_some_and(|tokens| {
                    tokens <= self.accounted.as_ref().map_or(0, |usage| usage.total)
                })
            && self
                .model_turn
                .as_deref()
                .is_none_or(|id| thread_id(id).is_ok())
    }

    fn price(&mut self, model: api_cost::Model, usage: &RecordedUsage) {
        if let Some(writes) = usage.cache_write_input {
            self.usd += model.standard_usd(usage.input, usage.cached_input, writes, usage.output);
            self.priced_tokens += usage.total;
        }
    }

    fn record_model(&mut self, turn: Option<&str>, model: Option<api_cost::Model>) {
        self.model = model;
        self.model_turn = turn.map(str::to_owned);
        if let (Some(turn), Some(model)) = (turn, model)
            && self.openai_provider == Some(true)
        {
            for pending in std::mem::take(&mut self.pending) {
                if pending.turn == turn {
                    self.price(model, &pending.usage);
                } else {
                    self.pending.push_back(pending);
                }
            }
        }
    }

    fn record(
        &mut self,
        total: &RecordedUsage,
        request: Option<RecordedUsage>,
        turn: Option<&str>,
        explicit_turn: bool,
    ) {
        if self
            .accounted
            .as_ref()
            .is_some_and(|previous| total.total <= previous.total)
        {
            return;
        }
        if let (Some(turn), Some(request)) = (turn, request)
            && self.openai_provider == Some(true)
            && total.accounts_for(&request, self.accounted.as_ref())
        {
            if let Some(model) = self
                .model
                .filter(|_| Some(turn) == self.model_turn.as_deref())
            {
                self.price(model, &request);
            } else if explicit_turn && thread_id(turn).is_ok() {
                // Some rollouts write the usage before the model for the same
                // turn. Hold only validated request deltas with explicit IDs.
                if self.pending.len() == MAX_PENDING_COST_REQUESTS {
                    self.pending.pop_front();
                }
                self.pending.push_back(PendingCostRequest {
                    turn: turn.to_owned(),
                    usage: request,
                });
            }
        }
        self.accounted = Some(total.clone());
    }

    fn report(&self) -> Value {
        let total = self.accounted.as_ref().map_or(0, |usage| usage.total);
        if total == 0 {
            return Value::Null;
        }
        let partial = self.partial || self.priced_tokens < total;
        json!({
            "usd": (!partial).then_some(self.usd),
            "priced_tokens": self.priced_tokens,
            "observed_tokens": total,
            "partial": partial,
            "prices_as_of": api_cost::PRICES_AS_OF,
        })
    }
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct History {
    pub samples: VecDeque<Sample>,
    pub epoch: u64,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub window: Option<u64>,
    pub turn: Option<String>,
    pub has_current: bool,
    pub pending_compaction: bool,
    pub truncated: bool,
    cache_write_input: Option<u64>,
    session_usage: Option<RecordedUsage>,
    api_estimate: ApiEstimate,
    signature: Option<String>,
}
impl History {
    fn valid(&self) -> bool {
        self.samples.len() <= 400
            && self.api_estimate.valid()
            && self.session_usage.as_ref().is_none_or(RecordedUsage::valid)
            && self.cache_write_input.is_none_or(|w| {
                self.samples
                    .back()
                    .is_some_and(|s| w <= s.input - s.cached_input.min(s.input))
            })
            && self.window != Some(0)
            && self
                .model
                .as_ref()
                .is_none_or(|s| !s.is_empty() && s.len() <= 256)
            && self
                .effort
                .as_ref()
                .is_none_or(|s| !s.is_empty() && s.len() <= 64)
            && self
                .signature
                .as_ref()
                .is_none_or(|s| s.len() == 64 && s.bytes().all(|c| c.is_ascii_hexdigit()))
            && self.turn.as_ref().is_none_or(|id| thread_id(id).is_ok())
            && (!self.has_current || self.samples.back().is_some_and(|s| s.epoch == self.epoch))
            && self.samples.iter().all(|s| {
                s.at.is_finite()
                    && s.at >= 0.0
                    && s.epoch <= self.epoch
                    && s.cached_input <= s.input
                    && s.reasoning_output <= s.output
                    && [
                        s.tokens,
                        s.input,
                        s.output,
                        s.cached_input,
                        s.reasoning_output,
                    ]
                    .iter()
                    .all(|n| *n <= i64::MAX as u64)
                    && s.turn.as_ref().is_none_or(|id| thread_id(id).is_ok())
            })
    }
    fn reset(&mut self, compaction: bool) {
        self.epoch += 1;
        self.has_current = false;
        self.signature = None;
        self.cache_write_input = None;
        self.pending_compaction = compaction;
    }
    fn record_session_usage(&mut self, row: &Value, id: &str) {
        let p = &row["payload"];
        if row["type"] == "turn_context" {
            self.api_estimate.record_model(
                p["turn_id"].as_str(),
                p["model"].as_str().and_then(api_cost::Model::from_id),
            );
            return;
        }
        if row["type"] == "event_msg"
            && p["type"] == "thread_settings_applied"
            && p["thread_id"] == id
        {
            if let Some(provider) = p["thread_settings"]["model_provider_id"].as_str() {
                self.api_estimate.openai_provider = Some(provider == "openai");
            }
            if let Some(model) = p["thread_settings"]["model"].as_str()
                && api_cost::Model::from_id(model) != self.api_estimate.model
            {
                self.api_estimate.model = None;
            }
            return;
        }
        let (usage, request, turn) = if row["type"] == "token_usage_record" && p["thread_id"] == id
        {
            (&p["thread_token_usage"], &p["usage"], p["turn_id"].as_str())
        } else if row["type"] == "event_msg" && p["type"] == "token_count" {
            let info = &p["info"];
            let request = if info["last_usage_includes_reasoning"] == false {
                &Value::Null
            } else {
                &info["last_token_usage"]
            };
            (&info["total_token_usage"], request, self.turn.as_deref())
        } else {
            return;
        };
        if let Ok(at) = stamp(&row["timestamp"])
            && let Some(total) = RecordedUsage::parse(usage, at)
        {
            if row["type"] == "token_usage_record"
                && self
                    .api_estimate
                    .accounted
                    .as_ref()
                    .is_some_and(|previous| total.total < previous.total)
            {
                self.api_estimate.partial = true;
            }
            self.api_estimate.record(
                &total,
                RecordedUsage::parse(request, at),
                turn,
                row["type"] == "token_usage_record",
            );
            // A token-count event may carry an older running total after the
            // request ledger has advanced. Preserve the latest cumulative usage.
            if self.session_usage.as_ref().is_none_or(|last| {
                total.total > last.total || (total.total == last.total && at >= last.at)
            }) {
                self.session_usage = Some(total);
            }
        } else if row["type"] == "token_usage_record" {
            self.api_estimate.partial = true;
        }
    }
    fn settings(
        &mut self,
        model: Option<&str>,
        effort: Option<&str>,
        window: Option<u64>,
    ) -> Result<()> {
        ensure!(
            model.is_none_or(|s| !s.is_empty() && s.len() <= 256)
                && effort.is_none_or(|s| !s.is_empty() && s.len() <= 64)
                && window != Some(0),
            "Invalid model metadata"
        );
        if self
            .model
            .as_deref()
            .zip(model)
            .is_some_and(|(a, b)| a != b)
            || self
                .effort
                .as_deref()
                .zip(effort)
                .is_some_and(|(a, b)| a != b)
            || self.window.zip(window).is_some_and(|(a, b)| a != b)
        {
            self.reset(false);
        }
        if let Some(m) = model {
            self.model = Some(m.into());
        }
        if let Some(e) = effort {
            self.effort = Some(e.into());
        }
        if let Some(w) = window {
            self.window = Some(w);
        }
        Ok(())
    }
    pub fn consume(&mut self, row: &Value) -> Result<()> {
        let p = &row["payload"];
        if !p.is_object() {
            return Ok(());
        }
        let kind = row["type"].as_str().unwrap_or("");
        let event = p["type"].as_str().unwrap_or("");
        if kind == "turn_context" {
            let effort = [
                "reasoningEffort",
                "reasoning_effort",
                "model_reasoning_effort",
                "effort",
            ]
            .iter()
            .find_map(|k| p[k].as_str());
            self.settings(p["model"].as_str(), effort, None)?;
            if let Some(turn) = p["turn_id"].as_str() {
                thread_id(turn)?;
                self.turn = Some(turn.into());
            }
        } else if kind == "compacted" || (kind == "event_msg" && event == "context_compacted") {
            if !self.pending_compaction {
                self.reset(true);
            }
        } else if kind == "event_msg" {
            match event {
                "task_started" => {
                    self.settings(None, None, p["model_context_window"].as_u64())?;
                    if let Some(turn) = p["turn_id"].as_str() {
                        thread_id(turn)?;
                        self.turn = Some(turn.into());
                    }
                }
                "task_complete" | "task_completed" => {
                    let turn = p["turn_id"].as_str().or(self.turn.as_deref());
                    if self.has_current
                        && let Some(last) = self.samples.back_mut()
                        && last.turn.as_deref() == turn
                    {
                        last.is_turn_end = true;
                    }
                }
                "token_count" => {
                    let info = &p["info"];
                    if info.is_null() {
                        return Ok(());
                    }
                    let usage = &info["last_token_usage"];
                    let get = |key: &str| -> Result<u64> {
                        let n = usage[key]
                            .as_u64()
                            .ok_or_else(|| anyhow!("Invalid numeric usage"))?;
                        ensure!(n <= i64::MAX as u64, "Usage overflow");
                        Ok(n)
                    };
                    let (input, output, cached, reasoning, tokens) = (
                        get("input_tokens")?,
                        get("output_tokens")?,
                        get("cached_input_tokens")?,
                        get("reasoning_output_tokens")?,
                        get("total_tokens")?,
                    );
                    ensure!(
                        cached <= input && reasoning <= output,
                        "Invalid usage subsets"
                    );
                    self.settings(None, None, info["model_context_window"].as_u64())?;
                    let at = stamp(&row["timestamp"])?;
                    let signature = digest(serde_json::to_vec(&json!([
                        self.turn,
                        usage,
                        info["total_token_usage"]["total_tokens"]
                    ]))?);
                    if self.signature.as_ref() == Some(&signature) {
                        return Ok(());
                    }
                    if self.has_current && self.samples.back().is_some_and(|s| tokens < s.tokens) {
                        self.reset(false);
                    }
                    self.samples.push_back(Sample {
                        at,
                        tokens,
                        input,
                        output,
                        cached_input: cached,
                        reasoning_output: reasoning,
                        turn: self.turn.clone(),
                        epoch: self.epoch,
                        is_compaction: self.pending_compaction,
                        is_turn_end: false,
                    });
                    self.cache_write_input = usage["cache_write_input_tokens"]
                        .as_u64()
                        .filter(|w| *w <= input - cached);
                    while self.samples.len() > 400 {
                        self.samples.pop_front();
                    }
                    self.has_current = true;
                    self.signature = Some(signature);
                    self.pending_compaction = false;
                }
                _ => {}
            }
        }
        Ok(())
    }
    pub fn report(&self, now: f64) -> Value {
        let last = if self.has_current {
            self.samples.back()
        } else {
            None
        };
        let mut request = json!(last);
        if let Some(w) = self.cache_write_input.filter(|_| last.is_some()) {
            request["cache_write_input"] = json!(w);
        }
        json!({"samples":self.samples,"last_request":request,"tokens":last.map(|p|p.tokens),"observed_window":self.window,
            "model":self.model,"effort":self.effort,"history_truncated":self.truncated,
            "session_usage":self.session_usage,
            "api_cost_estimate":self.api_estimate.report(),
            "stale":last.is_none_or(|p|now-p.at>120.0||p.at>now+1.0),"max_age_seconds":120})
    }
}
fn stamp(v: &Value) -> Result<f64> {
    let dt = chrono::DateTime::parse_from_rfc3339(
        v.as_str().ok_or_else(|| anyhow!("Missing timestamp"))?,
    )?;
    let n = dt.timestamp_micros() as f64 / 1e6;
    ensure!(n >= 0.0, "Invalid timestamp");
    Ok(n)
}

fn oversized_response_item(line: &[u8]) -> bool {
    // Codex places the record type before the payload in its rollout envelope.
    // A large response body can be skipped without losing a billing record.
    let prefix = &line[..line.len().min(512)];
    let payload = b"\"payload\":";
    let Some(end) = prefix.windows(payload.len()).position(|w| w == payload) else {
        return false;
    };
    let Some(fields) = prefix[..end].trim_ascii_end().strip_suffix(b",") else {
        return false;
    };
    let mut envelope = fields.to_vec();
    envelope.push(b'}');
    serde_json::from_slice::<Value>(&envelope).is_ok_and(|row| row["type"] == "response_item")
}
pub fn no_links(path: &Path) -> Result<()> {
    ensure!(path.is_absolute(), "Absolute path required");
    for p in path.ancestors() {
        ensure!(
            !fs::symlink_metadata(p)?.file_type().is_symlink(),
            "Symlink path rejected"
        );
    }
    Ok(())
}
fn open_read(path: &Path) -> Result<File> {
    no_links(path)?;
    let f = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    ensure!(f.metadata()?.is_file(), "Regular file required");
    Ok(f)
}
fn find(home: &Path, id: &str) -> Result<PathBuf> {
    thread_id(id)?;
    no_links(home)?;
    let suffix = format!("{id}.jsonl");
    let mut matches = vec![];
    let mut stack = vec![home.join("sessions"), home.join("archived_sessions")];
    let mut visited = 0;
    while let Some(dir) = stack.pop() {
        if !dir.exists() {
            continue;
        }
        no_links(&dir)?;
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            visited += 1;
            ensure!(visited <= 200_000, "Transcript discovery budget exhausted");
            let t = entry.file_type()?;
            if t.is_symlink() {
                continue;
            }
            if t.is_dir() {
                stack.push(entry.path());
            } else if t.is_file() && entry.file_name().to_string_lossy().ends_with(&suffix) {
                matches.push(entry.path());
            }
        }
    }
    ensure!(
        matches.len() == 1,
        "Exact unique root transcript unavailable"
    );
    Ok(matches.remove(0))
}
#[derive(Clone, Serialize, Deserialize)]
struct Cached {
    version: u8,
    path: PathBuf,
    device: u64,
    inode: u64,
    header: String,
    cursor: u64,
    mtime: i64,
    mtime_ns: i64,
    anchor: String,
    discarding: bool,
    history: History,
}
#[derive(Default)]
pub struct HistoryStore {
    entries: HashMap<String, Cached>,
    pub state_dir: Option<PathBuf>,
}
impl HistoryStore {
    pub fn new(state_dir: Option<PathBuf>) -> Self {
        Self {
            entries: HashMap::new(),
            state_dir,
        }
    }
    pub fn read(&mut self, home: &Path, id: &str, now: f64) -> Result<(Value, Value)> {
        let path = find(home, id)?;
        let f = open_read(&path)?;
        let meta = f.metadata()?;
        let mut stream = BufReader::new(f);
        let mut header = vec![];
        stream
            .by_ref()
            .take((LINE + 1) as u64)
            .read_until(b'\n', &mut header)?;
        ensure!(
            header.len() <= LINE && header.last() == Some(&b'\n'),
            "Incomplete transcript header"
        );
        let head: Value = serde_json::from_slice(&header)?;
        let m = &head["payload"];
        ensure!(
            head["type"] == "session_meta"
                && m["id"] == id
                && m["cwd"]
                    .as_str()
                    .is_some_and(|s| Path::new(s).is_absolute()),
            "Transcript identity mismatch"
        );
        ensure!(
            m["parent_thread_id"].is_null()
                && m["parentThreadId"].is_null()
                && m["source"].get("subagent").is_none(),
            "Root transcript required"
        );
        ensure!(
            m["cli_version"]
                .as_str()
                .is_some_and(|s| VERSIONS.contains(&s)),
            "Unsupported rollout version"
        );
        let hash = digest(&header);
        let header_len = header.len() as u64;
        if !self.entries.contains_key(id)
            && let Some(dir) = &self.state_dir
        {
            let p = dir.join(format!("history-{}.json", digest(id)));
            if p.exists()
                && let Ok(mut file) = open_read(&p)
                && file.metadata()?.len() <= 1024 * 1024
            {
                let mut bytes = vec![];
                file.read_to_end(&mut bytes)?;
                if let Ok(c) = serde_json::from_slice::<Cached>(&bytes) {
                    self.entries.insert(id.into(), c);
                }
            }
        }
        let mut cached = self.entries.remove(id).filter(|c| {
            c.version == CACHE_VERSION
                && c.path == path
                && c.device == meta.dev()
                && c.inode == meta.ino()
                && c.header == hash
                && c.cursor >= header_len
                && c.cursor <= meta.len()
                && c.history.valid()
        });
        if let Some(c) = &cached {
            let start = header_len.max(c.cursor.saturating_sub(2048));
            stream.seek(SeekFrom::Start(start))?;
            let mut bytes = vec![];
            stream
                .by_ref()
                .take(c.cursor - start)
                .read_to_end(&mut bytes)?;
            if digest(bytes) != c.anchor
                || (c.cursor == meta.len()
                    && (c.mtime != meta.mtime() || c.mtime_ns != meta.mtime_nsec()))
            {
                cached = None;
            }
        }
        let reused = cached.is_some();
        let mut c = cached.unwrap_or(Cached {
            version: CACHE_VERSION,
            path,
            device: meta.dev(),
            inode: meta.ino(),
            header: hash,
            cursor: header_len,
            mtime: meta.mtime(),
            mtime_ns: meta.mtime_nsec(),
            anchor: String::new(),
            discarding: false,
            history: History {
                api_estimate: ApiEstimate {
                    openai_provider: m["model_provider"].as_str().map(|p| p == "openai"),
                    ..ApiEstimate::default()
                },
                ..History::default()
            },
        });
        stream.seek(SeekFrom::Start(c.cursor))?;
        let mut consumed = 0;
        while c.cursor < meta.len() && consumed < BUDGET {
            let available = (BUDGET - consumed).min(meta.len() - c.cursor);
            if available == 0 {
                break;
            }
            let mut line = vec![];
            let n = stream
                .by_ref()
                .take(available.min((LINE + 1) as u64))
                .read_until(b'\n', &mut line)? as u64;
            if n == 0 {
                break;
            }
            consumed += n;
            if c.discarding {
                c.cursor += n;
                c.discarding = line.last() != Some(&b'\n');
                continue;
            }
            if line.len() > LINE {
                c.history.truncated = true;
                c.history.api_estimate.partial |= !oversized_response_item(&line);
                c.cursor += n;
                c.discarding = line.last() != Some(&b'\n');
                continue;
            }
            if line.last() != Some(&b'\n') {
                break;
            }
            c.cursor += n;
            match serde_json::from_slice::<Value>(&line) {
                Ok(row) => {
                    c.history.record_session_usage(&row, id);
                    c.history.consume(&row)?;
                }
                Err(_) => {
                    c.history.truncated = true;
                    c.history.api_estimate.partial = true;
                }
            }
        }
        let start = header_len.max(c.cursor.saturating_sub(2048));
        stream.seek(SeekFrom::Start(start))?;
        let mut bytes = vec![];
        stream
            .by_ref()
            .take(c.cursor - start)
            .read_to_end(&mut bytes)?;
        c.anchor = digest(bytes);
        c.mtime = meta.mtime();
        c.mtime_ns = meta.mtime_nsec();
        let report = c.history.report(now);
        let collection = json!({"cache_reused":reused,"bytes_read":consumed,"cursor":c.cursor,"file_size":meta.len(),"catching_up":c.cursor<meta.len()});
        if let Some(dir) = &self.state_dir {
            if !dir.exists() {
                fs::create_dir_all(dir)?;
                fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?;
            }
            no_links(dir)?;
            ensure!(
                fs::metadata(dir)?.uid() == unsafe { libc::getuid() }
                    && fs::metadata(dir)?.mode() & 0o077 == 0,
                "Cache directory must be private"
            );
            let dest = dir.join(format!("history-{}.json", digest(id)));
            let temp = dir.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
            let mut out = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temp)?;
            out.write_all(&serde_json::to_vec(&c)?)?;
            out.sync_all()?;
            fs::rename(temp, dest)?;
        }
        if self.entries.len() >= 16
            && let Some(k) = self.entries.keys().next().cloned()
        {
            self.entries.remove(&k);
        }
        self.entries.insert(id.into(), c);
        Ok((report, collection))
    }
}
