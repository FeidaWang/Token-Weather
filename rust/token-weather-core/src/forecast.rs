//! Pure numeric projection. Context history is distinct from lifetime billing.
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Sample {
    pub at: f64,
    pub tokens: u64,
    #[serde(default)]
    pub input: u64,
    #[serde(default)]
    pub output: u64,
    #[serde(default)]
    pub cached_input: u64,
    #[serde(default)]
    pub reasoning_output: u64,
    pub turn: Option<String>,
    pub epoch: u64,
    #[serde(default)]
    pub is_compaction: bool,
    #[serde(default)]
    pub is_turn_end: bool,
}
#[derive(Clone)]
struct Turn {
    id: String,
    epoch: u64,
    tokens: u64,
    at: f64,
    compact: bool,
    completed: bool,
    requests: u64,
    output: u64,
    last_index: usize,
}
fn fresh(value: &Value, binding: &str, now: f64) -> bool {
    value["thread_id"] == binding
        && value["observed_at"]
            .as_f64()
            .is_some_and(|at| (0.0..=5.0).contains(&(now - at)))
}
fn mean(values: &[f64]) -> Option<f64> {
    if values.is_empty() || values.iter().any(|v| *v < 0.0) {
        None
    } else {
        Some(values.iter().sum::<f64>() / values.len() as f64)
    }
}

pub fn compute(report: &Value, reading: &Value, now: f64) -> Result<Value> {
    ensure!(now.is_finite() && now >= 0.0, "Invalid clock");
    let samples: Vec<Sample> =
        serde_json::from_value(report.get("samples").cloned().unwrap_or(json!([])))?;
    ensure!(
        samples.len() <= 400,
        "History exceeds numeric retention bound"
    );
    let mut turns: Vec<Turn> = Vec::new();
    for (index, sample) in samples.iter().enumerate() {
        ensure!(
            sample.at.is_finite() && sample.at >= 0.0,
            "Invalid sample timestamp"
        );
        let Some(id) = sample.turn.as_ref().filter(|id| !id.is_empty()) else {
            continue;
        };
        if turns
            .last()
            .is_none_or(|p| p.epoch != sample.epoch || p.id != *id)
        {
            turns.push(Turn {
                id: id.clone(),
                epoch: sample.epoch,
                tokens: sample.tokens,
                at: sample.at,
                compact: false,
                completed: false,
                requests: 0,
                output: 0,
                last_index: index,
            });
        }
        let p = turns.last_mut().unwrap();
        p.tokens = sample.tokens;
        p.at = sample.at;
        p.last_index = index;
        p.compact |= sample.is_compaction;
        p.completed = sample.is_turn_end;
        p.requests += 1;
        p.output = p
            .output
            .checked_add(sample.output)
            .ok_or_else(|| anyhow::anyhow!("Output overflow"))?;
    }
    let binding = reading["thread_id"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("Missing binding"))?;
    let tokens = reading["tokens"].as_u64();
    let runtime = &reading["runtime"];
    let budget = &runtime["auto_compact"];
    let sampling = budget["kind"] == "sampling_runtime_remaining";
    let boundary = (budget["kind"] == "effective_runtime_remaining" || sampling)
        && budget["source"] == "same_owner_engine_compaction_trace"
        && fresh(budget, binding, now)
        && fresh(runtime, binding, now)
        && tokens.is_some_and(|n| {
            n > 0
                && budget["active_context_tokens"].as_u64() == Some(n)
                && (budget["scope_tokens"].as_u64() == Some(n)
                    || (sampling
                        && budget["scope"] == "body_after_prefix"
                        && budget["prefill_tokens"]
                            .as_u64()
                            .and_then(|p| n.checked_sub(p))
                            == budget["scope_tokens"].as_u64()))
        })
        && budget["scope_limit"].as_u64().is_some()
        && budget["full_context_limit"].as_u64().is_some()
        && runtime["awaiting_usage"] != true;
    let limit = if boundary {
        if sampling {
            tokens.and_then(|n| {
                budget["remaining_tokens"]
                    .as_u64()
                    .and_then(|r| n.checked_add(r))
            })
        } else {
            Some(
                budget["scope_limit"]
                    .as_u64()
                    .unwrap()
                    .min(budget["full_context_limit"].as_u64().unwrap()),
            )
        }
    } else {
        reading["observed_window"].as_u64()
    }
    .filter(|n| *n > 0);
    let epoch = report["last_request"]["epoch"].as_u64();
    let mut completed: Vec<&Turn> = turns
        .iter()
        .filter(|p| Some(p.epoch) == epoch && p.completed)
        .collect();
    if completed.len() > 6 {
        completed.drain(..completed.len() - 6);
    }
    let deltas: Vec<f64> = completed
        .windows(2)
        .map(|p| p[1].tokens as f64 - p[0].tokens as f64)
        .collect();
    let growth = mean(&deltas);
    let request_deltas: Vec<f64> = if completed.len() > 1 {
        samples[completed.first().unwrap().last_index..=completed.last().unwrap().last_index]
            .windows(2)
            .map(|p| p[1].tokens as f64 - p[0].tokens as f64)
            .collect()
    } else {
        Vec::new()
    };
    let usable = tokens.is_some()
        && reading["stale"] == false
        && reading["collection"]["catching_up"] != true
        && runtime["awaiting_usage"] != true;
    let outlook: Vec<Value> = if usable && limit.is_some() && growth.is_some() {
        (1..=5).map(|step|json!({"step":step,"tokens":tokens.unwrap() as f64+growth.unwrap()*step as f64})).collect()
    } else {
        vec![]
    };
    let mut points: Vec<Value>=turns.iter().skip(turns.len().saturating_sub(41)).map(|p| json!({"tokens":p.tokens,"at":p.at,"epoch":p.epoch,"compaction":p.compact,"completed":p.completed})).collect();
    if usable && !points.is_empty() && points.last().unwrap()["tokens"].as_u64() != tokens {
        if runtime["turn_id"]
            .as_str()
            .is_some_and(|id| id != turns.last().unwrap().id)
        {
            points.push(json!({"tokens":tokens,"at":reading["last_request"]["at"].as_f64().unwrap_or(now),"epoch":epoch,"compaction":false,"completed":false}));
            if points.len() > 41 {
                points.remove(0);
            }
        } else {
            let p = points.last_mut().unwrap();
            p["tokens"] = json!(tokens);
            p["completed"] = json!(false);
        }
    }
    let turns_left = match (boundary && usable, limit, tokens, growth) {
        (true, Some(limit), Some(tokens), Some(rate)) if rate > 0.0 => {
            Some(((limit as f64 - tokens as f64) / rate).max(0.0).ceil() as u64)
        }
        _ => None,
    };
    let last = completed.last();
    Ok(
        json!({"kind":"turn_forecast","thread_id":binding,"observed_at":now,"points":points,
        "history_truncated":report["history_truncated"].as_bool().unwrap_or(false),"reference_limit":limit,
        "reference_kind":if boundary {if sampling {"sampling_auto_compact"} else {"auto_compact"}} else {"observed_window"},
        "growth_per_turn":growth,"growth_per_request":mean(&request_deltas),"rate_basis":deltas.len(),"request_rate_basis":request_deltas.len(),
        "last_turn_delta":deltas.last(),"last_turn_requests":last.map(|p|p.requests),"last_turn_output":last.map(|p|p.output),
        "remaining_tokens":if boundary&&usable {limit.map(|l|l.saturating_sub(tokens.unwrap()))}else{None},
        "turns_left":turns_left,"outlook":outlook,"paused":!usable}),
    )
}
