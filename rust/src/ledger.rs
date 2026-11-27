use crate::config::{parse_stamp, trim_float, Config, LaneSpec, Pools};
use crate::usage::{
    bearer, http_get_json, http_get_text, iso_utc, round_to, run_with_timeout, Env,
};
use chrono::{DateTime, Datelike, Utc};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

pub const DEFAULT_CREDIT_FLOOR_USD: f64 = 2.0;
pub const DEFAULT_CREDIT_STALE_HOURS: f64 = 6.0;
pub const DEFAULT_CREDIT_REFRESH_MINUTES: f64 = 30.0;
pub const COMMANDCODE_MONTHLY_CREDITS: f64 = 70.0;
pub const COMMANDCODE_CREDITS_TO_USD: f64 = 100.0 / COMMANDCODE_MONTHLY_CREDITS;
const USAGE_EXPORT_ITEM: &str = "OpenCode Check usage";
const PI_SESSIONS: &str = ".pi/agent/sessions";
const PROVIDERS: [(&str, &str); 4] = [
    ("opencode-go", "opencode"),
    ("vercel-ai-gateway", "vercel"),
    ("openrouter-credit", "openrouter"),
    ("openai-codex", "codex"),
];

pub const PACE_LOG_NOTE: &str = "Launches come from the plan-pick launch log; counters come from token-ladder pools. The counter is the plan's own used share and the pace is used share over elapsed share. No tokens are read or printed.";
pub const BURN_NOTE: &str = "Recorded pi message costs, not invoices or extra-usage charges; no token-rate estimates. Missing cost data is unknown. Callers use usage-store cwd or launch caller/cwd; chat names and directories are separate when logs cannot link them.";

pub fn read_credits(path: &Path) -> Value {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

pub fn save_credits(path: &Path, entries: &Map<String, Value>) -> Result<Value, String> {
    let mut data = read_credits(path);
    if !data.is_object() {
        data = json!({});
    }
    let object = data.as_object_mut().ok_or("no credit map")?;
    for (name, entry) in entries {
        object.insert(name.clone(), entry.clone());
    }
    let text = serde_json::to_string(&data).map_err(|error| error.to_string())?;
    crate::collect::write_atomic(path, text.as_bytes())?;
    Ok(data)
}

pub struct CreditSource {
    pub limits: BTreeMap<String, f64>,
    pub reader: Option<Vec<String>>,
}

impl CreditSource {
    pub fn from_config(config: &Config) -> Self {
        CreditSource {
            limits: credit_limits(config),
            reader: config.opencode_reader_command.clone(),
        }
    }

    pub fn from_value(cfg: &Value) -> Self {
        let mut limits = BTreeMap::new();
        for plan in cfg
            .get("plans")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[])
        {
            let (Some(name), Some(limit)) = (
                plan.get("credit").and_then(Value::as_str),
                plan.get("usd_per_cycle").and_then(Value::as_f64),
            ) else {
                continue;
            };
            if limit > 0.0 {
                limits.insert(name.to_string(), limit);
            }
        }
        let reader = cfg
            .get("opencode_reader_command")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            });
        CreditSource { limits, reader }
    }
}

pub fn credit_lane_names_from_value(cfg: &Value) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for lane in cfg
        .get("lanes")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
    {
        let Some(name) = lane.get("credit").and_then(Value::as_str) else {
            continue;
        };
        if !names.iter().any(|known| known == name) {
            names.push(name.to_string());
        }
    }
    names
}

pub fn credit_limits(config: &Config) -> BTreeMap<String, f64> {
    let mut limits = BTreeMap::new();
    for plan in &config.plans {
        let (Some(name), Some(limit)) = (plan.credit.as_deref(), plan.usd_per_cycle) else {
            continue;
        };
        if limit > 0.0 {
            limits.insert(name.to_string(), limit);
        }
    }
    limits
}

pub fn credit_lane_names(config: &Config) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for lane in &config.lanes {
        let Some(name) = lane.credit.as_deref() else {
            continue;
        };
        if !names.iter().any(|known| known == name) {
            names.push(name.to_string());
        }
    }
    names
}

pub fn expand(path: &str, env: &Env) -> PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => env.home.join(rest),
        None => PathBuf::from(path),
    }
}

pub fn pi_key(env: &Env, provider: &str) -> Result<String, String> {
    let (program, args) = if provider == "commandcode" {
        if let Ok(key) = std::env::var("COMMANDCODE_API_KEY") {
            if !key.trim().is_empty() {
                return Ok(key.trim().to_string());
            }
        }
        (
            env.yeo_key.clone(),
            vec!["Commando Code API Key".to_string(), "credential".to_string()],
        )
    } else {
        (
            env.home_file(".npm-global/bin/pi"),
            vec![
                "auth".to_string(),
                "print-api-key".to_string(),
                "--provider".to_string(),
                provider.to_string(),
            ],
        )
    };
    let mut command = Command::new(program);
    command.args(&args).stdin(Stdio::null());
    match run_with_timeout(&mut command, None, Duration::from_secs(60)) {
        Some(output) if output.status.success() => {
            let key = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if key.is_empty() {
                Err("credential unavailable".to_string())
            } else {
                Ok(key)
            }
        }
        _ => Err("credential unavailable".to_string()),
    }
}

fn balance_vercel(env: &Env) -> Result<Value, String> {
    let key = pi_key(env, "vercel-ai-gateway")?;
    let body = http_get_json(&env.endpoints.vercel_credits, &bearer(&key))?;
    let pass = body
        .get("token_pass")
        .ok_or_else(|| "unknown Vercel credits".to_string())?;
    let remaining = pass
        .get("remaining")
        .and_then(Value::as_f64)
        .ok_or_else(|| "unknown Vercel credits".to_string())?;
    Ok(json!({
        "usd": round_to(remaining, 2),
        "resets_at": pass.get("resets_at").cloned().unwrap_or(Value::Null),
    }))
}

fn balance_openrouter(env: &Env) -> Result<Value, String> {
    let key = pi_key(env, "openrouter-credit")?;
    let account = http_get_json(&env.endpoints.openrouter_credits, &bearer(&key))?;
    let data = account
        .get("data")
        .ok_or_else(|| "unknown OpenRouter credits".to_string())?;
    let total = data
        .get("total_credits")
        .and_then(Value::as_f64)
        .ok_or_else(|| "unknown OpenRouter credits".to_string())?;
    let used = data
        .get("total_usage")
        .and_then(Value::as_f64)
        .ok_or_else(|| "unknown OpenRouter credits".to_string())?;
    let left = total - used;
    let key_body = http_get_json(&env.endpoints.openrouter_key, &bearer(&key))?;
    let remaining = key_body
        .get("data")
        .and_then(|data| data.get("limit_remaining"))
        .and_then(Value::as_f64);
    Ok(json!({
        "usd": round_to(remaining.map_or(left, |limit| left.min(limit)), 2),
    }))
}

fn balance_commandcode(env: &Env) -> Result<Value, String> {
    let key = pi_key(env, "commandcode")?;
    let data = http_get_json(&env.endpoints.commandcode_credits, &bearer(&key))?;
    let subscriptions = http_get_json(&env.endpoints.commandcode_subscriptions, &bearer(&key))?;
    let subscription = subscriptions
        .get("data")
        .ok_or_else(|| "unknown CommandCode subscription".to_string())?;
    if subscription.get("planId").and_then(Value::as_str) != Some("individual-goat")
        || subscription.get("status").and_then(Value::as_str) != Some("active")
    {
        return Err("CommandCode GOAT subscription is not active".to_string());
    }
    let credits = data
        .get("credits")
        .and_then(|credits| credits.get("monthlyCredits"))
        .and_then(Value::as_f64)
        .ok_or_else(|| "invalid CommandCode monthly credits".to_string())?;
    if credits < 0.0 {
        return Err("invalid CommandCode monthly credits".to_string());
    }
    let windows = data
        .get("windowLimits")
        .cloned()
        .ok_or_else(|| "invalid CommandCode window".to_string())?;
    for name in ["fiveHour", "weekly"] {
        let window = windows
            .get(name)
            .ok_or_else(|| "invalid CommandCode window".to_string())?;
        let used = window.get("used").and_then(Value::as_f64);
        let cap = window.get("cap").and_then(Value::as_f64);
        let reset = window.get("resetAt").and_then(Value::as_f64);
        let (Some(used), Some(cap), Some(reset)) = (used, cap, reset) else {
            return Err("invalid CommandCode window".to_string());
        };
        if used < 0.0 || cap <= 0.0 || reset < 0.0 {
            return Err("invalid CommandCode window".to_string());
        }
        let exceeded = window
            .get("exceeded")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if reset == 0.0 && (used != 0.0 || exceeded) {
            return Err("invalid CommandCode idle window".to_string());
        }
    }
    let period_start = subscription
        .get("currentPeriodStart")
        .and_then(Value::as_str)
        .and_then(parse_stamp)
        .ok_or_else(|| "invalid CommandCode period".to_string())?;
    let period_end = subscription
        .get("currentPeriodEnd")
        .and_then(Value::as_str)
        .and_then(parse_stamp)
        .ok_or_else(|| "invalid CommandCode period".to_string())?;
    Ok(json!({
        "credits": credits,
        "windows": windows,
        "period_start": period_start,
        "period_end": period_end,
    }))
}

fn balance_opencode(env: &Env, source: &CreditSource) -> Result<Value, String> {
    let command = source
        .reader
        .clone()
        .filter(|command| !command.is_empty())
        .ok_or_else(|| "OpenCode reader command missing".to_string())?;
    let mut child = Command::new(expand(&command[0], env));
    child
        .args(&command[1..])
        .args(["--credits", "--print"])
        .stdin(Stdio::null());
    let output = run_with_timeout(&mut child, None, Duration::from_secs(180))
        .ok_or_else(|| "OpenCode reader failed".to_string())?;
    if !output.status.success() {
        return Err("OpenCode reader failed".to_string());
    }
    let body: Value =
        serde_json::from_slice(&output.stdout).map_err(|_| "invalid OpenCode credits".to_string())?;
    let usd = body
        .get("credits_usd")
        .and_then(Value::as_f64)
        .ok_or_else(|| "invalid OpenCode credits".to_string())?;
    let at = body
        .get("at")
        .and_then(Value::as_f64)
        .ok_or_else(|| "invalid OpenCode credits".to_string())?;
    Ok(json!({"usd": round_to(usd, 2), "at": at, "source": "OpenCode live console"}))
}

pub fn read_balance(env: &Env, source: &CreditSource, name: &str) -> Result<Value, String> {
    match name {
        "vercel" => balance_vercel(env),
        "commandcode" => balance_commandcode(env),
        "openrouter" => balance_openrouter(env),
        "opencode" => balance_opencode(env, source),
        _ => Err("unknown credit lane".to_string()),
    }
}

pub fn refresh_credits(
    env: &Env,
    source: &CreditSource,
    names: &[String],
    now: f64,
) -> (Value, Vec<String>) {
    let mut entries = Map::new();
    let mut failures = Vec::new();
    for name in names {
        match read_balance(env, source, name) {
            Ok(mut entry) => {
                if let Some(object) = entry.as_object_mut() {
                    object.insert("at".to_string(), json!(now));
                    if let Some(limit) = source.limits.get(name) {
                        object.insert("limit_usd".to_string(), json!(limit));
                    }
                }
                entries.insert(name.clone(), entry);
            }
            Err(kind) => failures.push(format!("plan-pick: cannot read {name} credit: {kind}")),
        }
    }
    match save_credits(&env.state_file("lane-credits.json"), &entries) {
        Ok(data) => (data, failures),
        Err(error) => {
            failures.push(format!("plan-pick: cannot store credit readings: {error}"));
            (json!({}), failures)
        }
    }
}

pub fn read_credits_command(
    env: &Env,
    config: &Config,
    names: &[String],
    now: f64,
) -> Result<(String, Vec<String>), String> {
    let lock = env.state_file("lane-credits.json.lock");
    if let Ok(meta) = std::fs::metadata(&lock) {
        if let Ok(modified) = meta.modified() {
            let age = std::time::SystemTime::now()
                .duration_since(modified)
                .map(|age| age.as_secs_f64())
                .unwrap_or(0.0);
            if age < 120.0 {
                return Ok((String::new(), Vec::new()));
            }
        }
    }
    if let Some(parent) = lock.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    std::fs::write(&lock, "").map_err(|error| error.to_string())?;
    let (data, failures) = refresh_credits(env, &CreditSource::from_config(config), names, now);
    let _ = std::fs::remove_file(&lock);
    let mut parts = Vec::new();
    for name in names {
        let Some(entry) = data.get(name) else {
            continue;
        };
        if name == "commandcode" {
            let pool = commandcode_pool(Some(entry), now, config);
            parts.push(format!(
                "{name} {}",
                pool.get("note").and_then(Value::as_str).unwrap_or("")
            ));
        } else if let Some(usd) = entry.get("usd").and_then(Value::as_f64) {
            parts.push(format!("{name} ${usd:.2}"));
        }
    }
    Ok((parts.join(", "), failures))
}

pub fn commandcode_pool(entry: Option<&Value>, now: f64, config: &Config) -> Value {
    let invalid = json!({"state": "OFF", "note": "CommandCode credit reading is invalid"});
    let Some(entry) = entry else {
        return json!({"state": "OFF", "note": "CommandCode credit reading missing or stale"});
    };
    let stale = config.credit_stale_hours.max(0.0) * 3600.0;
    let at = entry.get("at").and_then(Value::as_f64).unwrap_or(0.0);
    let period_end = entry.get("period_end").and_then(Value::as_f64);
    let period_start = entry.get("period_start").and_then(Value::as_f64);
    let credits = entry.get("credits").and_then(Value::as_f64);
    let (Some(period_end), Some(period_start), Some(credits)) = (period_end, period_start, credits)
    else {
        return invalid;
    };
    if now - at > stale || now >= period_end {
        return json!({"state": "OFF", "note": "CommandCode credit reading missing or stale"});
    }
    let used = (COMMANDCODE_MONTHLY_CREDITS - credits).max(0.0);
    let elapsed = if period_end > period_start {
        ((now - period_start) / (period_end - period_start)).clamp(0.0, 1.0)
    } else {
        0.0
    };
    let pace = (elapsed > 0.0).then(|| used / COMMANDCODE_MONTHLY_CREDITS / elapsed);
    let pace_text = pace.map_or_else(|| "unknown".to_string(), |pace| format!("{pace:.2}x"));
    let mut state = if credits > 0.0 { "ON" } else { "OFF" };
    let windows = entry.get("windows").cloned().unwrap_or(Value::Null);
    let mut window_parts = Vec::new();
    for (name, label) in [("fiveHour", "5h"), ("weekly", "week")] {
        let Some(window) = windows.get(name) else {
            return invalid;
        };
        let (Some(cap), Some(reset), Some(window_used)) = (
            window.get("cap").and_then(Value::as_f64),
            window.get("resetAt").and_then(Value::as_f64),
            window.get("used").and_then(Value::as_f64),
        ) else {
            return invalid;
        };
        let reset = reset / 1000.0;
        let spent = if now < reset { window_used } else { 0.0 };
        let exceeded = window
            .get("exceeded")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if spent >= cap || (now < reset && exceeded) {
            state = "OFF";
        }
        window_parts.push(format!(
            "{label} {}/{} credits",
            trim_float(spent),
            trim_float(cap)
        ));
    }
    json!({
        "state": state,
        "used": round_to(used, 2),
        "limit": COMMANDCODE_MONTHLY_CREDITS,
        "unit": "plan credits",
        "pace_ratio": pace,
        "resets_at": period_end,
        "note": format!(
            "{}/70 monthly credits used, pace {pace_text}; {}/70 monthly credits left (~${:.2} Flash usage); {}; extra paid credits excluded",
            trim_float(used),
            trim_float(credits),
            credits * COMMANDCODE_CREDITS_TO_USD,
            window_parts.join("; ")
        ),
    })
}

pub fn utc_day(epoch: f64) -> String {
    DateTime::<Utc>::from_timestamp(epoch as i64, 0)
        .map(|stamp| stamp.format("%Y-%m-%d").to_string())
        .unwrap_or_default()
}


pub fn usage_rows(text: &str) -> Option<Vec<Value>> {
    let mut lines = text.lines();
    let header = lines.next()?;
    let fields: Vec<&str> = header.split(',').map(str::trim).collect();
    let index = |name: &str| fields.iter().position(|field| *field == name);
    let (Some(day_at), Some(provider_at), Some(cost_at)) =
        (index("day"), index("provider"), index("cost_usd"))
    else {
        return None;
    };
    let mut rows = Vec::new();
    for line in lines {
        let values: Vec<&str> = line.split(',').collect();
        let Some(day) = values.get(day_at).map(|value| value.trim()) else {
            continue;
        };
        let Some(provider) = values.get(provider_at).map(|value| value.trim()) else {
            continue;
        };
        let Some(usd) = values
            .get(cost_at)
            .and_then(|value| value.trim().parse::<f64>().ok())
        else {
            continue;
        };
        if day.is_empty() || provider.is_empty() || !is_day(day) {
            continue;
        }
        if !usd.is_finite() || usd < 0.0 {
            continue;
        }
        rows.push(json!({"day": day, "provider": provider, "usd": usd}));
    }
    Some(rows)
}

fn is_day(day: &str) -> bool {
    day.len() == 10
        && day.bytes().enumerate().all(|(index, byte)| {
            if index == 4 || index == 7 {
                byte == b'-'
            } else {
                byte.is_ascii_digit()
            }
        })
}

pub fn fetch_usage_export(env: &Env) -> Result<String, String> {
    let key = std::env::var("OPENCODE_API_KEY")
        .ok()
        .map(|key| key.trim().to_string())
        .filter(|key| !key.is_empty());
    let Some(key) = key else {
        return Err("plan-pick: OPENCODE_API_KEY missing".to_string());
    };
    http_get_text(&env.endpoints.opencode_usage, &bearer(&key)).map_err(|error| {
        format!("plan-pick: cannot read the OpenCode usage export: {error}")
    })
}

pub fn usage_export_command(env: &Env) -> Vec<String> {
    vec![
        env.yeo_key.display().to_string(),
        "--env".to_string(),
        USAGE_EXPORT_ITEM.to_string(),
        "--".to_string(),
        std::env::current_exe()
            .map(|path| path.display().to_string())
            .unwrap_or_else(|_| "token-ladder".to_string()),
        "--plan-pick".to_string(),
        "--opencode-usage-export".to_string(),
    ]
}

pub fn jsonl_rows(path: &Path) -> Vec<Value> {
    std::fs::read_to_string(path)
        .map(|text| {
            text.lines()
                .filter_map(|line| serde_json::from_str::<Value>(line).ok())
                .filter(Value::is_object)
                .collect()
        })
        .unwrap_or_default()
}

pub fn launches_path(env: &Env) -> PathBuf {
    match std::env::var_os("PLAN_PICK_LAUNCHES") {
        Some(path) if !path.is_empty() => PathBuf::from(path),
        _ => env.state_file("launches.jsonl"),
    }
}

fn launch_rows(env: &Env) -> Vec<Value> {
    jsonl_rows(&launches_path(env))
}

fn session_files(env: &Env) -> Vec<PathBuf> {
    let root = env.home_file(PI_SESSIONS);
    let mut out = Vec::new();
    let Ok(dirs) = std::fs::read_dir(&root) else {
        return out;
    };
    for dir in dirs.flatten() {
        let Ok(files) = std::fs::read_dir(dir.path()) else {
            continue;
        };
        for file in files.flatten() {
            let path = file.path();
            if path.extension().is_some_and(|ext| ext == "jsonl") {
                out.push(path);
            }
        }
    }
    out.sort();
    out
}

pub fn month_start(now: f64) -> f64 {
    let Some(stamp) = DateTime::<Utc>::from_timestamp(now as i64, 0) else {
        return 0.0;
    };
    stamp
        .date_naive()
        .with_day(1)
        .and_then(|day| day.and_hms_opt(0, 0, 0))
        .map(|naive| naive.and_utc().timestamp() as f64)
        .unwrap_or(0.0)
}

fn empty_burn_row() -> Value {
    json!({"usd": null, "messages": 0, "unpriced_messages": 0, "launches": 0})
}

fn add_message(row: &mut Value, usd: Option<f64>) {
    let Some(object) = row.as_object_mut() else {
        return;
    };
    let messages = object.get("messages").and_then(Value::as_u64).unwrap_or(0) + 1;
    object.insert("messages".to_string(), json!(messages));
    match usd {
        Some(usd) => {
            let current = object.get("usd").and_then(Value::as_f64).unwrap_or(0.0);
            object.insert("usd".to_string(), json!(current + usd));
        }
        None => {
            let unpriced = object
                .get("unpriced_messages")
                .and_then(Value::as_u64)
                .unwrap_or(0)
                + 1;
            object.insert("unpriced_messages".to_string(), json!(unpriced));
        }
    }
}

fn add_launch(row: &mut Value) {
    if let Some(object) = row.as_object_mut() {
        let launches = object.get("launches").and_then(Value::as_u64).unwrap_or(0) + 1;
        object.insert("launches".to_string(), json!(launches));
    }
}

pub fn burn_report(env: &Env, config: &Config, now: f64) -> Value {
    let start = month_start(now);
    let month = utc_day(start);
    let month = month.get(0..7).unwrap_or("").to_string();
    let mut by_lane: BTreeMap<String, Value> = config
        .lanes
        .iter()
        .map(|lane| (lane.lane.clone(), empty_burn_row()))
        .collect();
    let mut by_caller: BTreeMap<String, Value> = BTreeMap::new();
    let mut seen: BTreeSet<(String, String, i64)> = BTreeSet::new();
    for path in session_files(env) {
        let mut caller = "unknown caller".to_string();
        for row in jsonl_rows(&path) {
            if row.get("type").and_then(Value::as_str) == Some("session") {
                caller = row
                    .get("cwd")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown caller")
                    .to_string();
            }
            let Some(message) = row.get("message").filter(|message| message.is_object()) else {
                continue;
            };
            if message.get("role").and_then(Value::as_str) != Some("assistant") {
                continue;
            }
            let Some(stamp) = message.get("timestamp").and_then(Value::as_f64) else {
                continue;
            };
            let at = stamp / 1000.0;
            if at < start || at > now {
                continue;
            }
            let Some(provider) = message.get("provider").and_then(Value::as_str) else {
                continue;
            };
            if let Some(id) = row.get("id").and_then(Value::as_str) {
                let identity = (provider.to_string(), id.to_string(), stamp as i64);
                if !seen.insert(identity) {
                    continue;
                }
            }
            let usd = message
                .get("usage")
                .and_then(|usage| usage.get("cost"))
                .and_then(|cost| cost.get("total"))
                .and_then(Value::as_f64)
                .filter(|usd| usd.is_finite() && *usd >= 0.0);
            let lane = PROVIDERS
                .iter()
                .find(|(name, _)| *name == provider)
                .map(|(_, lane)| (*lane).to_string())
                .unwrap_or_else(|| provider.to_string());
            let lane_row = by_lane.entry(lane).or_insert_with(empty_burn_row);
            add_message(lane_row, usd);
            let caller_row = by_caller
                .entry(caller.clone())
                .or_insert_with(empty_burn_row);
            add_message(caller_row, usd);
        }
    }
    for row in launch_rows(env) {
        let Some(at) = row.get("at").and_then(Value::as_f64) else {
            continue;
        };
        if at < start || at > now {
            continue;
        }
        let Some(lane) = row.get("lane").and_then(Value::as_str) else {
            continue;
        };
        let lane_row = by_lane.entry(lane.to_string()).or_insert_with(empty_burn_row);
        add_launch(lane_row);
        let caller = row
            .get("caller")
            .and_then(Value::as_str)
            .or_else(|| row.get("cwd").and_then(Value::as_str))
            .unwrap_or("unknown caller")
            .to_string();
        let caller_row = by_caller.entry(caller).or_insert_with(empty_burn_row);
        add_launch(caller_row);
    }
    json!({
        "month": month,
        "currency": "USD",
        "by_lane": by_lane,
        "by_caller": by_caller,
        "measured": config.measured_burn.clone().unwrap_or_else(|| json!([])),
        "note": BURN_NOTE,
    })
}

pub fn burn_lines(report: &Value) -> Vec<String> {
    let month = report.get("month").and_then(Value::as_str).unwrap_or("");
    let mut lines = vec![format!("Burn {month} UTC (USD)"), BURN_NOTE.to_string()];
    if let Some(measured) = report.get("measured").and_then(Value::as_array) {
        for item in measured {
            let usd = item.get("usd").and_then(Value::as_f64).unwrap_or(0.0);
            let tokens = item.get("tokens").and_then(Value::as_f64).unwrap_or(0.0);
            let requests = item.get("requests").and_then(Value::as_u64).unwrap_or(0);
            let days = item.get("period_days").and_then(Value::as_u64).unwrap_or(0);
            let lane = item.get("lane").and_then(Value::as_str).unwrap_or("");
            let as_of = item.get("as_of").and_then(Value::as_str).unwrap_or("");
            let cached = item.get("cached_pct").and_then(Value::as_f64).unwrap_or(0.0);
            let per_billion = if tokens > 0.0 {
                round_to(usd / (tokens / 1e9), 1)
            } else {
                0.0
            };
            lines.push(format!(
                "Measured {lane} direct, {days} days ending {as_of}: ${usd:.2} for {:.2}B tokens over {requests} requests; about ${per_billion:.2} per 1B tokens; ~{}% cached. Separate account measurement, not added to recorded pi costs.",
                tokens / 1e9,
                trim_float(cached)
            ));
        }
    }
    for (title, key) in [("Per lane", "by_lane"), ("Per caller", "by_caller")] {
        lines.push(title.to_string());
        let table = report.get(key).cloned().unwrap_or(Value::Null);
        let mut names: Vec<String> = table
            .as_object()
            .map(|object| object.keys().cloned().collect())
            .unwrap_or_default();
        names.sort();
        for name in names {
            let item = table.get(&name).cloned().unwrap_or(Value::Null);
            let usd = match item.get("usd").and_then(Value::as_f64) {
                Some(usd) => format!("${usd:.4}"),
                None => "unknown".to_string(),
            };
            let messages = item.get("messages").and_then(Value::as_u64).unwrap_or(0);
            let unpriced = item
                .get("unpriced_messages")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            let launches = item.get("launches").and_then(Value::as_u64).unwrap_or(0);
            lines.push(format!(
                "  {name}: {usd}; {messages} messages; {unpriced} unpriced; {launches} launches"
            ));
        }
    }
    lines
}

pub fn lanes_from(config: &Config, now: f64) -> Vec<LaneSpec> {
    let lanes: Vec<LaneSpec> = config
        .lanes
        .iter()
        .filter(|lane| !lane.lane.is_empty() && !lane.command.is_empty())
        .cloned()
        .collect();
    let (front, rest): (Vec<LaneSpec>, Vec<LaneSpec>) = lanes
        .into_iter()
        .partition(|lane| lane.first_until_epoch().is_some_and(|epoch| epoch as f64 > now));
    front.into_iter().chain(rest).collect()
}

pub fn pace_log(pools: &Pools, config: &Config, now: f64, path: &Path) -> Value {
    let launches: Vec<(f64, String)> = jsonl_rows(path)
        .into_iter()
        .filter_map(|row| {
            let at = row.get("at").and_then(Value::as_f64)?;
            let lane = row.get("lane").and_then(Value::as_str)?.to_string();
            Some((at, lane))
        })
        .collect();
    let period = month_start(now);
    let mut rows = Map::new();
    for lane in lanes_from(config, now) {
        let name = lane.lane.clone();
        let pool = lane
            .pool
            .as_deref()
            .and_then(|name| pools.get(name))
            .or_else(|| {
                lane.credit
                    .as_deref()
                    .and_then(|name| pools.get(&format!("credit:{name}")))
            })
            .cloned()
            .unwrap_or_default();
        let reset = pool.reset_epoch().map(|epoch| epoch as f64);
        let used = pool.used_pct.filter(|used| used.is_finite());
        let span = pool.window_minutes.unwrap_or(0) as f64 * 60.0;
        let start = match (reset, span) {
            (Some(reset), span) if span > 0.0 => Some(reset - span),
            _ => None,
        };
        let since = start.unwrap_or(period);
        let count = launches
            .iter()
            .filter(|(at, lane_name)| lane_name == &name && *at >= since)
            .count();
        rows.insert(
            name.clone(),
            json!({
                "lane": name,
                "since": since,
                "since_reset": start.is_some(),
                "launches": count,
                "used_pct": used,
                "limit": pool.limit.filter(|limit| limit.is_finite()),
                "unit": pool.unit,
                "elapsed_pct": pool.elapsed_pct.filter(|value| value.is_finite()),
                "window_minutes": pool.window_minutes,
                "pace_ratio": pool.pace_ratio.filter(|value| value.is_finite()),
                "state": pool.state,
                "credit": lane.credit.is_some(),
                "note": pool.note,
                "chat_only": pool.chat_only,
            }),
        );
    }
    json!({
        "now": now,
        "launch_log": path.display().to_string(),
        "note": PACE_LOG_NOTE,
        "lanes": rows,
    })
}

pub fn pace_log_lines(report: &Value) -> Vec<String> {
    let mut lines = vec![
        "Pace log: plan-pick launches since each plan reset, beside the plan's own counter".to_string(),
        PACE_LOG_NOTE.to_string(),
    ];
    let lanes = report.get("lanes").cloned().unwrap_or(Value::Null);
    let mut names: Vec<String> = lanes
        .as_object()
        .map(|object| object.keys().cloned().collect())
        .unwrap_or_default();
    names.sort();
    for name in names {
        let row = lanes.get(&name).cloned().unwrap_or(Value::Null);
        let since = row.get("since").and_then(Value::as_f64).unwrap_or(0.0);
        let since_text = iso_utc(since);
        let launches = row.get("launches").and_then(Value::as_u64).unwrap_or(0);
        let plural = if launches == 1 { "" } else { "es" };
        let reset = row
            .get("since_reset")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let launch = format!(
            "{launches} launch{plural} since {since_text}{}",
            if reset { "" } else { " (month start)" }
        );
        if row
            .get("chat_only")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            lines.push(format!(
                "{name}: {launch}; chat-only pool, no worker counter"
            ));
            continue;
        }
        let Some(used) = row.get("used_pct").and_then(Value::as_f64) else {
            let credit = row.get("credit").and_then(Value::as_bool).unwrap_or(false);
            lines.push(format!(
                "{name}: {launch}; {}",
                if credit {
                    "credit lane, no plan counter in this reading"
                } else {
                    "counter unknown"
                }
            ));
            continue;
        };
        let mut counter = match row.get("limit").and_then(Value::as_f64) {
            Some(limit) => format!(
                "{} of {} {} used",
                trim_float(used),
                trim_float(limit),
                row.get("unit").and_then(Value::as_str).unwrap_or("credits")
            ),
            None => format!("{}% used", trim_float(used)),
        };
        if let Some(elapsed) = row.get("elapsed_pct").and_then(Value::as_f64) {
            let window = match row.get("window_minutes").and_then(Value::as_f64) {
                Some(minutes) => format!(" of {} h", trim_float(minutes / 60.0)),
                None => String::new(),
            };
            counter.push_str(&format!(" with {elapsed:.1}%{window} gone"));
        }
        match row.get("pace_ratio").and_then(Value::as_f64) {
            Some(pace) => counter.push_str(&format!("; pace {pace:.2}x")),
            None => counter.push_str("; pace unknown"),
        }
        lines.push(format!("{name}: {launch}; {counter}"));
    }
    lines
}





