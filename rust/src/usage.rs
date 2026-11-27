use crate::config::{home_dir, parse_stamp, Pool, PoolGroup, Stampish, STATE_DIR};
use chrono::{DateTime, Datelike, Local, TimeZone, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const WEEK_MINUTES: u64 = 10080;
pub const SHORT_MINUTES: u64 = 300;
pub const HISTORY_SAMPLE_SECONDS: f64 = 300.0;
pub const PACE_MIN_WEEK_ELAPSED_PCT: f64 = 2.0;
pub const PACE_MIN_SHORT_ELAPSED_PCT: f64 = 10.0;
pub const AGY_TTL: f64 = 120.0;
pub const AGY_TIMEOUT_SECONDS: u64 = 60;
pub const CODEX_PROBE_SECONDS: u64 = 20;
pub const CODEX_LOG_FRESH_SECONDS: f64 = 600.0;
pub const GROK_WINDOW_MINUTES: u64 = WEEK_MINUTES;

const CLAUDE_USAGE_URL: &str = "https://api.anthropic.com/api/oauth/usage";
const OPENROUTER_KEY_URL: &str = "https://openrouter.ai/api/v1/key";
const VERCEL_CREDITS_URL: &str = "https://ai-gateway.vercel.sh/v1/credits";
const COMMANDCODE_CREDITS_URL: &str = "https://api.commandcode.ai/alpha/billing/credits";
const COMMANDCODE_SUBSCRIPTIONS_URL: &str = "https://api.commandcode.ai/alpha/billing/subscriptions";
const OPENROUTER_CREDITS_URL: &str = "https://openrouter.ai/api/v1/credits";
pub const OPENCODE_USAGE_URL: &str = "https://opencode.ai/console/api/v2/usage/export?range=30d";
const AGY_ARGS: [&str; 4] = ["-p", "/usage", "--print-timeout", "45s"];
const CODEX_PATHS: [&str; 4] = [
    ".npm-global/bin/codex",
    ".local/bin/codex",
    ".bun/bin/codex",
    "/usr/local/bin/codex",
];
const ONE_PASSWORD_ENV: &str = ".config/yeo-ux-agents/op-service-account.env";
const GOOGLE_LOG: &str = ".gemini/antigravity-cli/cli.log";
const GOOGLE_HISTORY: &str = ".gemini/antigravity-cli/history.jsonl";
const PROCESS_NAMES: [&str; 3] = ["claude", "node", "codex"];

pub fn local_codex_command() -> &'static str {
    "hax --provider=codex --model=gpt-6.1-sol --effort=high --no-session -p <prompt>"
}

#[derive(Clone, Debug)]
pub struct Endpoints {
    pub claude_usage: String,
    pub openrouter_key: String,
    pub openrouter_credits: String,
    pub vercel_credits: String,
    pub commandcode_credits: String,
    pub commandcode_subscriptions: String,
    pub opencode_usage: String,
}

impl Default for Endpoints {
    fn default() -> Self {
        Endpoints {
            claude_usage: CLAUDE_USAGE_URL.to_string(),
            openrouter_key: OPENROUTER_KEY_URL.to_string(),
            openrouter_credits: OPENROUTER_CREDITS_URL.to_string(),
            vercel_credits: VERCEL_CREDITS_URL.to_string(),
            commandcode_credits: COMMANDCODE_CREDITS_URL.to_string(),
            commandcode_subscriptions: COMMANDCODE_SUBSCRIPTIONS_URL.to_string(),
            opencode_usage: OPENCODE_USAGE_URL.to_string(),
        }
    }
}

impl Endpoints {
    pub fn from_process() -> Self {
        let mut endpoints = Endpoints::default();
        let overrides = [
            ("TOKEN_LADDER_CLAUDE_USAGE_URL", 0),
            ("TOKEN_LADDER_OPENROUTER_KEY_URL", 1),
            ("TOKEN_LADDER_OPENROUTER_CREDITS_URL", 2),
            ("TOKEN_LADDER_VERCEL_CREDITS_URL", 3),
            ("TOKEN_LADDER_COMMANDCODE_CREDITS_URL", 4),
            ("TOKEN_LADDER_COMMANDCODE_SUBSCRIPTIONS_URL", 5),
            ("TOKEN_LADDER_OPENCODE_USAGE_URL", 6),
        ];
        for (name, index) in overrides {
            if let Some(value) = env_text(name) {
                match index {
                    0 => endpoints.claude_usage = value,
                    1 => endpoints.openrouter_key = value,
                    2 => endpoints.openrouter_credits = value,
                    3 => endpoints.vercel_credits = value,
                    4 => endpoints.commandcode_credits = value,
                    5 => endpoints.commandcode_subscriptions = value,
                    _ => endpoints.opencode_usage = value,
                }
            }
        }
        endpoints
    }
}

#[derive(Clone, Debug)]
pub struct Env {
    pub home: PathBuf,
    pub state: PathBuf,
    pub ssh: String,
    pub rsync: String,
    pub ps: String,
    pub yeo_key: PathBuf,
    pub agy: Option<PathBuf>,
    pub codex: Option<PathBuf>,
    pub hostname: String,
    pub endpoints: Endpoints,
}

fn env_text(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn command_or(name: &str, fallback: &str) -> String {
    env_text(name).unwrap_or_else(|| fallback.to_string())
}

fn executable(path: &Path) -> Option<PathBuf> {
    match std::fs::metadata(path) {
        Ok(meta) if meta.is_file() && is_executable(&meta) => Some(path.to_path_buf()),
        _ => None,
    }
}

#[cfg(unix)]
fn is_executable(meta: &std::fs::Metadata) -> bool {
    use std::os::unix::fs::PermissionsExt;
    meta.permissions().mode() & 0o111 != 0
}

#[cfg(not(unix))]
fn is_executable(_meta: &std::fs::Metadata) -> bool {
    true
}

fn hostname() -> String {
    env_text("TOKEN_LADDER_HOSTNAME")
        .or_else(|| {
            std::fs::read_to_string("/proc/sys/kernel/hostname")
                .ok()
                .map(|text| text.trim().to_string())
                .filter(|text| !text.is_empty())
        })
        .or_else(|| env_text("HOSTNAME"))
        .unwrap_or_else(|| "localhost".to_string())
}

impl Env {
    pub fn under(home: impl Into<PathBuf>) -> Self {
        let home = home.into();
        let state = home.join(STATE_DIR);
        Env {
            ssh: "ssh".to_string(),
            rsync: "rsync".to_string(),
            ps: "ps".to_string(),
            yeo_key: home.join(".local/bin/yeo-key"),
            agy: executable(&home.join(".local/bin/agy")),
            codex: None,
            hostname: hostname(),
            endpoints: Endpoints::default(),
            home,
            state,
        }
    }

    pub fn from_process() -> Self {
        let home = home_dir();
        let mut env = Env::under(home);
        if let Some(dir) = std::env::var_os("TOKEN_LADDER_STATE_DIR") {
            env.state = PathBuf::from(dir);
        }
        env.ssh = command_or("TOKEN_LADDER_SSH", "ssh");
        env.rsync = command_or("TOKEN_LADDER_RSYNC", "rsync");
        env.ps = command_or("TOKEN_LADDER_PS", "ps");
        env.yeo_key = env_text("TOKEN_LADDER_YEO_KEY")
            .map(PathBuf::from)
            .unwrap_or_else(|| env.home.join(".local/bin/yeo-key"));
        env.agy = match env_text("TOKEN_LADDER_AGY") {
            Some(path) => executable(&PathBuf::from(path)),
            None => executable(&env.home.join(".local/bin/agy")),
        };
        env.codex = match env_text("TOKEN_LADDER_CODEX") {
            Some(path) => executable(&PathBuf::from(path)),
            None => codex_binary(&env.home),
        };
        env.endpoints = Endpoints::from_process();
        env
    }

    pub fn state_file(&self, name: &str) -> PathBuf {
        self.state.join(name)
    }

    pub fn home_file(&self, name: &str) -> PathBuf {
        self.home.join(name)
    }
}

fn codex_binary(home: &Path) -> Option<PathBuf> {
    let mut candidates = Vec::new();
    for path in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        candidates.push(path.join("codex"));
    }
    for path in CODEX_PATHS {
        candidates.push(if path.starts_with('/') {
            PathBuf::from(path)
        } else {
            home.join(path)
        });
    }
    candidates.into_iter().find_map(|path| executable(&path))
}

pub fn now_epoch() -> f64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(elapsed) => elapsed.as_secs_f64(),
        Err(_) => 0.0,
    }
}

pub fn round_to(value: f64, places: u32) -> f64 {
    if !value.is_finite() {
        return value;
    }
    let factor = 10_f64.powi(places as i32);
    (value * factor).round() / factor
}

pub fn finite(value: Option<f64>) -> Option<f64> {
    value.filter(|value| value.is_finite())
}

pub fn percentage(value: Option<f64>) -> Option<f64> {
    value.filter(|value| value.is_finite() && (0.0..=100.0).contains(value))
}

#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq)]
pub struct Window {
    pub used: Option<f64>,
    pub resets_at: Option<f64>,
    pub hours_left: Option<f64>,
    pub time_gone: Option<f64>,
}

pub fn window(used: Option<f64>, resets_at: Option<f64>, minutes: u64, now: f64) -> Window {
    match (used, resets_at) {
        (Some(used), Some(mut resets_at)) if used.is_finite() && resets_at.is_finite() => {
            let span = minutes as f64 * 60.0;
            let used = if resets_at <= now {
                resets_at += span;
                0.0
            } else {
                used
            };
            let hours_left = (resets_at - now) / 3600.0;
            let time_gone = (100.0 * (1.0 - hours_left * 60.0 / minutes as f64)).clamp(0.0, 100.0);
            Window {
                used: Some(round_to(used, 1)),
                resets_at: Some(resets_at),
                hours_left: Some(round_to(hours_left, 1)),
                time_gone: Some(round_to(time_gone, 1)),
            }
        }
        _ => Window::default(),
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Sample {
    pub ts: f64,
    pub used: Option<f64>,
    pub resets_at: Option<f64>,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq)]
pub struct Pace {
    pub per_hour: Option<f64>,
    pub per_day: Option<f64>,
    pub ratio: Option<f64>,
    pub verdict: String,
    pub empty_hours: Option<f64>,
}

pub fn window_pace(bar: &Window, samples: &[Sample], now: f64) -> Pace {
    let mut pace = Pace {
        verdict: "unknown".to_string(),
        ..Pace::default()
    };
    let Some(used) = percentage(bar.used) else {
        return pace;
    };
    if let Some(gone) = bar.time_gone.filter(|gone| gone.is_finite()) {
        if gone > 0.0 && gone <= 100.0 {
            let ratio = used / gone;
            pace.ratio = Some(round_to(ratio, 4));
            pace.verdict = if ratio > 1.1 {
                "above".to_string()
            } else if ratio < 0.9 {
                "below".to_string()
            } else {
                "on".to_string()
            };
        }
    }
    let mut points: BTreeMap<i64, Sample> = BTreeMap::new();
    for sample in samples {
        if sample.ts.is_finite() && sample.ts <= now {
            points.insert((sample.ts * 1000.0) as i64, sample.clone());
        }
    }
    points.insert(
        (now * 1000.0) as i64,
        Sample {
            ts: now,
            used: Some(used),
            resets_at: bar.resets_at,
        },
    );
    let ordered: Vec<Sample> = points.into_values().collect();
    let rate = |seconds: f64| -> Option<f64> {
        let start = now - seconds;
        let anchor = ordered.iter().rposition(|sample| sample.ts <= start)?;
        let mut total = 0.0;
        for pair in ordered[anchor..].windows(2) {
            let left = &pair[0];
            let right = &pair[1];
            if right.ts <= start {
                continue;
            }
            let span = right.ts - left.ts;
            let (Some(a), Some(b)) = (percentage(left.used), percentage(right.used)) else {
                return None;
            };
            if span > 2.0 * HISTORY_SAMPLE_SECONDS {
                return None;
            }
            let known = match (finite(left.resets_at), finite(right.resets_at)) {
                (Some(old), Some(new)) => Some((old, new)),
                _ => None,
            };
            let delta = match known {
                Some((old, new)) if new < old => return None,
                Some((old, new)) if new != old => b,
                Some(_) => {
                    if b < a {
                        return None;
                    }
                    b - a
                }
                None => {
                    if b < a {
                        b
                    } else {
                        b - a
                    }
                }
            };
            let overlap = right.ts - start.max(left.ts);
            total += delta * overlap / span;
        }
        Some(total * 3600.0 / seconds)
    };
    let hourly = rate(3600.0);
    pace.per_hour = hourly.map(|value| round_to(value, 4));
    pace.per_day = rate(86400.0).map(|value| round_to(value * 24.0, 4));
    if used == 100.0 {
        pace.empty_hours = Some(0.0);
    } else if let Some(hourly) = hourly.filter(|value| *value > 0.0) {
        pace.empty_hours = Some(round_to((100.0 - used) / hourly, 4));
    }
    pace
}

pub fn agent() -> ureq::Agent {
    let config = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(20)))
        .build();
    ureq::Agent::new_with_config(config)
}

pub fn http_error(error: &ureq::Error) -> String {
    match error {
        ureq::Error::StatusCode(code) => format!("status {code}"),
        ureq::Error::Timeout(_) => "timeout".to_string(),
        ureq::Error::HostNotFound => "host not found".to_string(),
        ureq::Error::BadUri(_) => "bad uri".to_string(),
        ureq::Error::Io(_) => "io error".to_string(),
        ureq::Error::Protocol(_) => "protocol error".to_string(),
        _ => "request failed".to_string(),
    }
}

pub fn http_get_text(url: &str, headers: &[(String, String)]) -> Result<String, String> {
    let mut request = ureq::get(url);
    for (name, value) in headers {
        request = request.header(name.as_str(), value.as_str());
    }
    let mut response = request.call().map_err(|error| http_error(&error))?;
    response
        .body_mut()
        .read_to_string()
        .map_err(|_| "unreadable response".to_string())
}

pub fn http_get_json(url: &str, headers: &[(String, String)]) -> Result<Value, String> {
    let mut request = ureq::get(url);
    for (name, value) in headers {
        request = request.header(name.as_str(), value.as_str());
    }
    let mut response = request.call().map_err(|error| http_error(&error))?;
    response
        .body_mut()
        .read_json::<Value>()
        .map_err(|_| "invalid JSON".to_string())
}

pub fn bearer(token: &str) -> Vec<(String, String)> {
    vec![("Authorization".to_string(), format!("Bearer {token}"))]
}

pub fn claude_token(path: &Path) -> Result<String, String> {
    let text = std::fs::read_to_string(path).map_err(|_| "no credentials".to_string())?;
    let value: Value = serde_json::from_str(&text).map_err(|_| "bad credentials".to_string())?;
    value
        .get("claudeAiOauth")
        .and_then(|oauth| oauth.get("accessToken"))
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| "no access token".to_string())
}

#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq)]
pub struct ClaudeRead {
    pub five_hour: Window,
    pub week: Window,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fable: Option<Value>,
    pub error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
}

fn bar_field(bar: &Value, key: &str) -> Option<f64> {
    bar.get(key).and_then(Value::as_f64)
}

fn bar_reset(bar: &Value) -> Option<f64> {
    bar.get("resets_at")
        .and_then(Value::as_str)
        .and_then(parse_stamp)
        .map(|epoch| epoch as f64)
}

pub fn fable_window(body: &Value, now: f64) -> Option<Value> {
    let limits = body.get("limits")?.as_array()?;
    for limit in limits {
        if limit.get("kind").and_then(Value::as_str) != Some("weekly_scoped") {
            continue;
        }
        let display = limit
            .get("scope")
            .and_then(|scope| scope.get("model"))
            .and_then(|model| model.get("display_name"))
            .and_then(Value::as_str);
        if display != Some("Fable") {
            continue;
        }
        let bar = window(
            percentage(bar_field(limit, "percent")),
            bar_reset(limit),
            WEEK_MINUTES,
            now,
        );
        let share = body
            .get("weekly_scoped_shares")
            .and_then(Value::as_array)
            .and_then(|shares| shares.first())
            .and_then(|share| share.get("allowance_percent_of_weekly"))
            .and_then(Value::as_f64);
        let mut value = serde_json::to_value(&bar).unwrap_or(Value::Null);
        if let Some(object) = value.as_object_mut() {
            object.insert(
                "max_share_of_weekly".to_string(),
                share.map(Value::from).unwrap_or(Value::Null),
            );
        }
        return Some(value);
    }
    None
}

pub fn read_claude(env: &Env, now: f64, credentials: Option<&Path>) -> ClaudeRead {
    let path = match credentials {
        Some(path) => path.to_path_buf(),
        None => env.home_file(".claude/.credentials.json"),
    };
    let empty = |error: Option<String>| ClaudeRead {
        five_hour: window(None, None, SHORT_MINUTES, now),
        week: window(None, None, WEEK_MINUTES, now),
        fable: None,
        error,
        email: None,
    };
    let token = match claude_token(&path) {
        Ok(token) => token,
        Err(error) => return empty(Some(error)),
    };
    let headers = vec![
        ("Authorization".to_string(), format!("Bearer {token}")),
        ("anthropic-beta".to_string(), "oauth-2025-04-20".to_string()),
    ];
    let body = match http_get_json(&env.endpoints.claude_usage, &headers) {
        Ok(body) => body,
        Err(error) => return empty(Some(error)),
    };
    let five = body.get("five_hour").cloned().unwrap_or(Value::Null);
    let week = body.get("seven_day").cloned().unwrap_or(Value::Null);
    ClaudeRead {
        five_hour: window(
            percentage(bar_field(&five, "utilization")),
            bar_reset(&five),
            SHORT_MINUTES,
            now,
        ),
        week: window(
            percentage(bar_field(&week, "utilization")),
            bar_reset(&week),
            WEEK_MINUTES,
            now,
        ),
        fable: fable_window(&body, now),
        error: None,
        email: None,
    }
}

pub fn claude_email(env: &Env) -> Option<String> {
    let text = std::fs::read_to_string(env.home_file(".claude.json")).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    value
        .get("oauthAccount")
        .and_then(|account| account.get("emailAddress"))
        .and_then(Value::as_str)
        .map(str::to_string)
}

fn decode_base64(input: &str) -> Option<Vec<u8>> {
    let table = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut bits: u32 = 0;
    let mut count: u32 = 0;
    let mut out = Vec::new();
    for byte in input.bytes() {
        if byte == b'=' {
            break;
        }
        let value = table.iter().position(|candidate| *candidate == byte)? as u32;
        bits = (bits << 6) | value;
        count += 6;
        if count >= 8 {
            count -= 8;
            out.push(((bits >> count) & 0xff) as u8);
        }
    }
    Some(out)
}

pub fn codex_email(env: &Env) -> Option<String> {
    let text = std::fs::read_to_string(env.home_file(".codex/auth.json")).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    let token = value
        .get("tokens")
        .and_then(|tokens| tokens.get("id_token"))
        .and_then(Value::as_str)?;
    let part = token.split('.').nth(1)?;
    let decoded = decode_base64(part)?;
    let claims: Value = serde_json::from_slice(&decoded).ok()?;
    claims
        .get("email")
        .and_then(Value::as_str)
        .map(str::to_string)
}

#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq)]
pub struct CodexRead {
    pub week: Window,
    pub five_hour: Window,
    pub sampled_at: Option<f64>,
    pub plan: Option<String>,
    pub error: Option<String>,
    pub credits: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
}

pub fn find_key(value: &Value, key: &str) -> Option<Value> {
    match value {
        Value::Object(map) => {
            if let Some(found) = map.get(key) {
                return Some(found.clone());
            }
            map.values().find_map(|value| find_key(value, key))
        }
        Value::Array(items) => items.iter().find_map(|value| find_key(value, key)),
        _ => None,
    }
}

fn rate_limit_sample(value: &Value) -> Option<Value> {
    let limits = find_key(value, "rate_limits")?;
    if !limits.is_object() {
        return None;
    }
    let present = ["primary", "secondary"]
        .iter()
        .any(|slot| limits.get(*slot).is_some_and(|slot| !slot.is_null()));
    if !present {
        return None;
    }
    Some(limits)
}

fn newest_log_sample(home: &Path) -> Option<(f64, Value)> {
    let root = home.join(".codex/sessions");
    let mut files: Vec<(SystemTime, PathBuf)> = Vec::new();
    let mut level = vec![root];
    while let Some(dir) = level.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            match entry.file_type() {
                Ok(kind) if kind.is_dir() => level.push(path),
                Ok(kind) if kind.is_file() && path.extension().is_some_and(|ext| ext == "jsonl") => {
                    let modified = entry
                        .metadata()
                        .and_then(|meta| meta.modified())
                        .unwrap_or(UNIX_EPOCH);
                    files.push((modified, path));
                }
                _ => {}
            }
        }
    }
    files.sort_by_key(|entry| std::cmp::Reverse(entry.0));
    let mut best: Option<(f64, Value)> = None;
    for (_, path) in files.into_iter().take(40) {
        let Ok(mut file) = std::fs::File::open(&path) else {
            continue;
        };
        let size = file.metadata().map(|meta| meta.len()).unwrap_or(0);
        if file.seek(SeekFrom::Start(size.saturating_sub(400_000))).is_err() {
            continue;
        }
        let mut tail = String::new();
        if file.read_to_string(&mut tail).is_err() {
            continue;
        }
        for line in tail.lines().rev() {
            if !line.contains("\"rate_limits\"") {
                continue;
            }
            let Ok(event) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let Some(limits) = rate_limit_sample(&event) else {
                continue;
            };
            let Some(stamp) = event
                .get("timestamp")
                .and_then(Value::as_str)
                .and_then(parse_stamp)
            else {
                continue;
            };
            let stamp = stamp as f64;
            if best.as_ref().is_none_or(|current| stamp > current.0) {
                best = Some((stamp, limits));
            }
            break;
        }
    }
    best
}

pub fn parse_codex_response(response: &Value) -> Result<Value, String> {
    if response.get("error").is_some_and(|error| !error.is_null()) {
        return Err("rate limits request failed".to_string());
    }
    let result = response.get("result").unwrap_or(&Value::Null);
    if !result.is_object() {
        return Err("invalid Codex response".to_string());
    }
    let many = result
        .get("rateLimitsByLimitId")
        .cloned()
        .unwrap_or(Value::Null);
    if !many.is_object() && !many.is_null() {
        return Err("invalid Codex limits map".to_string());
    }
    let limits = match many.get("codex") {
        Some(value) if !value.is_null() => value.clone(),
        _ => result.get("rateLimits").cloned().unwrap_or(Value::Null),
    };
    if !limits.is_object() {
        return Err("no Codex rate limits".to_string());
    }
    if let Some(id) = limits.get("limitId").and_then(Value::as_str) {
        if id != "codex" {
            return Err("no Codex rate limits".to_string());
        }
    }
    let mut out = Map::new();
    out.insert(
        "plan_type".to_string(),
        limits.get("planType").cloned().unwrap_or(Value::Null),
    );
    out.insert(
        "credits".to_string(),
        limits.get("credits").cloned().unwrap_or(Value::Null),
    );
    for slot in ["primary", "secondary"] {
        let Some(window) = limits.get(slot).filter(|value| value.is_object()) else {
            continue;
        };
        let values: Vec<Option<f64>> = ["usedPercent", "resetsAt", "windowDurationMins"]
            .iter()
            .map(|key| window.get(*key).and_then(Value::as_f64))
            .collect();
        let (Some(used), Some(reset), Some(minutes)) = (values[0], values[1], values[2]) else {
            return Err("invalid Codex window".to_string());
        };
        if !used.is_finite() || !reset.is_finite() || !minutes.is_finite() || minutes <= 0.0 {
            return Err("invalid Codex window".to_string());
        }
        out.insert(
            slot.to_string(),
            json!({"used_percent": used, "resets_at": reset, "window_minutes": minutes}),
        );
    }
    let any = ["primary", "secondary"]
        .iter()
        .any(|slot| out.get(*slot).is_some_and(|value| !value.is_null()));
    if !any {
        return Err("no Codex rate limits".to_string());
    }
    Ok(Value::Object(out))
}

fn send_message(child: &mut Child, message: &Value) -> bool {
    let Some(stdin) = child.stdin.as_mut() else {
        return false;
    };
    writeln!(stdin, "{message}").is_ok() && stdin.flush().is_ok()
}

fn stop_child(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

fn probe_codex(binary: &Path, now: f64) -> Result<(f64, Value), String> {
    let mut child = Command::new(binary)
        .args(["app-server", "--listen", "stdio://"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "Codex probe: spawn failed".to_string())?;
    let Some(stdout) = child.stdout.take() else {
        stop_child(&mut child);
        return Err("Codex probe: no stdout".to_string());
    };
    let (sender, receiver) = mpsc::channel::<Vec<u8>>();
    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.split(b'\n') {
            match line {
                Ok(line) => {
                    if sender.send(line).is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
    });
    let init = json!({
        "method": "initialize",
        "id": 1,
        "params": {"clientInfo": {"name": "token_ladder", "title": "Token Ladder Telemetry", "version": "1"}}
    });
    if !send_message(&mut child, &init) {
        stop_child(&mut child);
        return Err("Codex probe: initialize failed".to_string());
    }
    let deadline = Instant::now() + Duration::from_secs(CODEX_PROBE_SECONDS);
    let mut initialized = false;
    let mut received = 0usize;
    let outcome = loop {
        let Some(left) = deadline.checked_duration_since(Instant::now()) else {
            break Err("Codex probe: timeout".to_string());
        };
        let Ok(chunk) = receiver.recv_timeout(left) else {
            break Err("Codex probe: timeout".to_string());
        };
        if chunk.is_empty() {
            break Err("Codex probe: app-server closed".to_string());
        }
        received += chunk.len();
        if received > 1_000_000 {
            break Err("Codex probe: response too large".to_string());
        }
        let Ok(message) = serde_json::from_slice::<Value>(&chunk) else {
            continue;
        };
        let id = message.get("id").and_then(Value::as_i64);
        if id == Some(1) && !initialized {
            if message.get("error").is_some_and(|error| !error.is_null()) {
                break Err("Codex probe: initialize failed".to_string());
            }
            initialized = true;
            send_message(&mut child, &json!({"method": "initialized", "params": {}}));
            send_message(
                &mut child,
                &json!({"method": "account/rateLimits/read", "id": 2}),
            );
        } else if id == Some(2) && initialized {
            break parse_codex_response(&message).map(|parsed| (now, parsed));
        }
    };
    stop_child(&mut child);
    outcome
}

pub fn read_codex(env: &Env, now: f64) -> CodexRead {
    let mut best = newest_log_sample(&env.home);
    let mut error = None;
    let stale = match best.as_ref() {
        Some((stamp, _)) => now - stamp >= CODEX_LOG_FRESH_SECONDS,
        None => true,
    };
    if stale {
        let probe = match env.codex.as_deref() {
            Some(binary) => match probe_codex(binary, now_epoch()) {
                Ok(sample) => Some(sample),
                Err(reason) => {
                    error = Some(format!(
                        "{reason}; {}",
                        if best.is_some() {
                            "stale log sample"
                        } else {
                            "no log sample"
                        }
                    ));
                    None
                }
            },
            None => {
                error = Some(format!(
                    "Codex probe: no binary; {}",
                    if best.is_some() {
                        "stale log sample"
                    } else {
                        "no log sample"
                    }
                ));
                None
            }
        };
        if let Some(sample) = probe {
            let newer = best.as_ref().is_none_or(|current| sample.0 > current.0);
            if newer {
                best = Some(sample);
            }
        }
    }
    let (sampled_at, limits) = match best {
        Some((stamp, limits)) => (Some(stamp), limits),
        None => (None, Value::Null),
    };
    let mut out = CodexRead {
        week: window(None, None, WEEK_MINUTES, now),
        five_hour: window(None, None, SHORT_MINUTES, now),
        sampled_at,
        plan: limits
            .get("plan_type")
            .and_then(Value::as_str)
            .map(str::to_string),
        error,
        credits: limits
            .get("credits")
            .and_then(|credits| credits.get("balance"))
            .and_then(Value::as_f64),
        email: None,
    };
    for slot in ["primary", "secondary"] {
        let Some(bar) = limits.get(slot).filter(|bar| bar.is_object()) else {
            continue;
        };
        let minutes = bar
            .get("window_minutes")
            .and_then(Value::as_f64)
            .unwrap_or(0.0);
        let span = if minutes >= 1440.0 {
            "week"
        } else {
            "five_hour"
        };
        let reading = window(
            percentage(bar.get("used_percent").and_then(Value::as_f64)),
            bar.get("resets_at").and_then(Value::as_f64),
            minutes.max(1.0) as u64,
            now,
        );
        if span == "week" {
            out.week = reading;
        } else {
            out.five_hour = reading;
        }
    }
    out
}

pub fn read_grok(env: &Env, cfg: &Value, now: f64) -> Option<Window> {
    let text = std::fs::read_to_string(env.state_file("grok.json")).ok()?;
    let data: Value = serde_json::from_str(&text).ok()?;
    let used = data
        .get("pct")
        .or_else(|| data.get("used_pct"))
        .and_then(Value::as_f64)?;
    if !used.is_finite() || !(0.0..=100.0).contains(&used) {
        return None;
    }
    let reset = data
        .get("resets_at")
        .and_then(Value::as_str)
        .and_then(parse_stamp)? as f64;
    let updated = data
        .get("updated_at")
        .and_then(Value::as_str)
        .and_then(parse_stamp)? as f64;
    if reset <= now {
        return None;
    }
    let stale_hours = crate::collect::cfg_num(cfg, "grok_stale_hours", 3.0);
    let age = now - updated;
    if age < 0.0 || age > stale_hours * 3600.0 {
        return None;
    }
    Some(window(Some(used), Some(reset), GROK_WINDOW_MINUTES, now))
}

#[derive(Clone, Debug, PartialEq)]
pub struct AgyReading {
    pub remaining_pct: i64,
    pub resets_at: i64,
}

pub type AgyGroups = BTreeMap<String, BTreeMap<String, AgyReading>>;

fn agy_group(name: &str) -> Option<&'static str> {
    match name.to_ascii_lowercase().as_str() {
        "gemini models" => Some("gemini"),
        "claude and gpt models" => Some("claude_gpt"),
        _ => None,
    }
}

fn agy_window(name: &str) -> Option<&'static str> {
    match name {
        "weekly limit remaining" => Some("week"),
        "five hour limit remaining" => Some("five_hour"),
        _ => None,
    }
}

fn collapsed(text: &str) -> String {
    text.split_whitespace()
        .collect::<Vec<&str>>()
        .join(" ")
        .to_ascii_lowercase()
}

fn remaining_percent(text: &str) -> Option<i64> {
    let digits = text.strip_suffix('%')?;
    if digits.is_empty() || digits.len() > 3 || !digits.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    digits.parse::<i64>().ok()
}

pub fn parse_agy_usage(text: &str) -> AgyGroups {
    let mut groups: AgyGroups = BTreeMap::new();
    for line in text.lines() {
        let fields: Vec<&str> = line.trim().split('\t').collect();
        if fields.len() != 4 {
            continue;
        }
        let Some(group) = agy_group(fields[0].trim()) else {
            continue;
        };
        let Some(span) = agy_window(&collapsed(fields[1])) else {
            continue;
        };
        let Some(remaining) = remaining_percent(fields[2].trim()) else {
            continue;
        };
        if !(0..=100).contains(&remaining) {
            continue;
        }
        let Some(reset) = parse_stamp(fields[3].trim()) else {
            continue;
        };
        groups.entry(group.to_string()).or_default().insert(
            span.to_string(),
            AgyReading {
                remaining_pct: remaining,
                resets_at: reset,
            },
        );
    }
    groups
}

fn agy_cache(env: &Env) -> PathBuf {
    env.state_file("agy-usage.json")
}

pub fn run_with_timeout(command: &mut Command, input: Option<&str>, timeout: Duration) -> Option<Output> {
    let mut child = command
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .ok()?;
    if let (Some(text), Some(mut stdin)) = (input, child.stdin.take()) {
        let _ = stdin.write_all(text.as_bytes());
        let _ = stdin.flush();
    }
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return child.wait_with_output().ok(),
            Ok(None) => {}
            Err(_) => {
                stop_child(&mut child);
                return None;
            }
        }
        if Instant::now() >= deadline {
            stop_child(&mut child);
            return None;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}

fn run_agy_usage(env: &Env) -> Option<String> {
    let binary = env.agy.as_deref()?;
    let cwd = std::env::temp_dir().join(format!(
        "token-ladder-agy-{}-{}",
        std::process::id(),
        (now_epoch() * 1000.0) as i64
    ));
    std::fs::create_dir_all(&cwd).ok()?;
    let mut command = Command::new(binary);
    command
        .args(AGY_ARGS)
        .current_dir(&cwd)
        .env_remove("TOKEN_LADDER_AGY");
    let output = run_with_timeout(&mut command, None, Duration::from_secs(AGY_TIMEOUT_SECONDS));
    std::fs::remove_dir_all(&cwd).ok();
    match output {
        Some(output) if output.status.success() => {
            String::from_utf8(output.stdout).ok().filter(|text| !text.is_empty())
        }
        _ => None,
    }
}

pub fn agy_usage(env: &Env, now: f64) -> Option<String> {
    let cache = agy_cache(env);
    if let Ok(text) = std::fs::read_to_string(&cache) {
        if let Ok(cached) = serde_json::from_str::<Value>(&text) {
            let at = cached.get("at").and_then(Value::as_f64);
            let cached_text = cached.get("text").and_then(Value::as_str);
            if let (Some(at), Some(cached_text)) = (at, cached_text) {
                if at.is_finite() && !cached_text.is_empty() && (0.0..=AGY_TTL).contains(&(now - at)) {
                    return Some(cached_text.to_string());
                }
            }
        }
    }
    let text = run_agy_usage(env)?;
    if !text.is_empty() {
        let payload = json!({"at": now, "text": text});
        let _ = crate::collect::write_atomic(
            &cache,
            format!("{payload}\n").as_bytes(),
        );
    }
    Some(text)
}

fn agy_readout(group: &BTreeMap<String, AgyReading>) -> Value {
    let week = group.get("week");
    let five = group.get("five_hour");
    json!({
        "weekly_remaining_pct": week.map(|reading| reading.remaining_pct),
        "weekly_resets_at": week.map(|reading| reading.resets_at),
        "five_hour_remaining_pct": five.map(|reading| reading.remaining_pct),
        "five_hour_resets_at": five.map(|reading| reading.resets_at),
    })
}

pub fn agy_pool(groups: &AgyGroups, now: f64, cfg: &Value) -> Pool {
    let gemini = groups.get("gemini");
    let week = gemini.and_then(|group| group.get("week"));
    let five = gemini.and_then(|group| group.get("five_hour"));
    let mut pool = Pool {
        state: Some("unknown".to_string()),
        window_minutes: Some(WEEK_MINUTES),
        chat_only: true,
        shared_with_chats: false,
        estimated: false,
        source: Some("agy /usage".to_string()),
        ..Pool::default()
    };
    for (key, group) in [("gemini", gemini), ("claude_gpt", groups.get("claude_gpt"))] {
        let Some(group) = group else {
            continue;
        };
        let readout = agy_readout(group);
        let typed: PoolGroup = serde_json::from_value(readout).unwrap_or_default();
        if key == "gemini" {
            pool.gemini = Some(typed.clone());
        } else {
            pool.claude_gpt = Some(typed);
        }
    }
    let Some(week) = week else {
        return pool;
    };
    let week_used = (100 - week.remaining_pct) as f64;
    let week_bar = window(
        Some(week_used),
        Some(week.resets_at as f64),
        WEEK_MINUTES,
        now,
    );
    let five_bar = five.map(|reading| {
        window(
            Some((100 - reading.remaining_pct) as f64),
            Some(reading.resets_at as f64),
            SHORT_MINUTES,
            now,
        )
    });
    let bars = crate::collect::pool_state(Some(&week_bar), five_bar.as_ref(), cfg, now, None);
    pool.used_pct = Some(week_used);
    pool.resets_at = week_bar.resets_at.map(Stampish::Epoch);
    pool.hours_left = week_bar.hours_left;
    pool.state = Some(bars.state);
    pool.pace_ratio = bars.pace_ratio;
    pool.elapsed_pct = week_bar.time_gone;
    if let Some(reading) = five {
        pool.five_hour_used_pct = Some((100 - reading.remaining_pct) as f64);
        pool.five_hour_resets_at = Some(Stampish::Epoch(reading.resets_at as f64));
    }
    let zero = gemini.and_then(|group| {
        group
            .iter()
            .find(|(_, reading)| reading.remaining_pct == 0)
            .map(|(span, reading)| (span.clone(), reading.resets_at))
    });
    if let Some((span, resets_at)) = zero {
        pool.state = Some("OFF".to_string());
        pool.used_up = true;
        pool.used_up_window = Some(span);
        pool.used_up_resets_at = Some(Stampish::Epoch(resets_at as f64));
        pool.used_up_at = Some(iso_utc(now));
    }
    pool
}

pub fn iso_utc(epoch: f64) -> String {
    DateTime::<Utc>::from_timestamp(epoch as i64, 0)
        .map(|stamp| stamp.format("%Y-%m-%dT%H:%M:%SZ").to_string())
        .unwrap_or_else(|| "1970-01-01T00:00:00Z".to_string())
}

fn google_history_today(env: &Env, now: f64) -> Option<i64> {
    let text = std::fs::read_to_string(env.home_file(GOOGLE_HISTORY)).ok()?;
    let start = DateTime::<Utc>::from_timestamp(now as i64, 0)?
        .date_naive()
        .and_hms_opt(0, 0, 0)?
        .and_utc()
        .timestamp() as f64;
    let mut used = 0i64;
    let mut seen = false;
    for line in text.lines() {
        let Ok(row) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let Some(stamp) = row.get("timestamp").and_then(Value::as_f64) else {
            continue;
        };
        if !stamp.is_finite() || stamp < 0.0 || stamp > now * 1000.0 {
            continue;
        }
        seen = true;
        if stamp >= start * 1000.0 {
            used += 1;
        }
    }
    if seen {
        Some(used)
    } else {
        None
    }
}

fn log_line_stamp(line: &str, today: (u32, u32), year: i32) -> Option<f64> {
    let bytes = line.as_bytes();
    if bytes.len() < 15 || !b"IWEF".contains(&bytes[0]) {
        return None;
    }
    let digits = |start: usize, end: usize| -> Option<u32> {
        line.get(start..end)?.parse::<u32>().ok()
    };
    let month = digits(1, 3)?;
    let day = digits(3, 5)?;
    if (month, day) != today {
        return None;
    }
    let hour = digits(6, 8)?;
    let minute = digits(9, 11)?;
    let second = digits(12, 14)?;
    let stamp = Local
        .with_ymd_and_hms(year, month, day, hour, minute, second)
        .single()?;
    Some(stamp.timestamp() as f64)
}

pub fn google_quota_used_up(env: &Env, now: f64) -> Option<Value> {
    let text = std::fs::read_to_string(env.home_file(GOOGLE_LOG)).ok()?;
    let local = DateTime::<Utc>::from_timestamp(now as i64, 0)?.with_timezone(&Local);
    let today = (local.month(), local.day());
    let mut newest: Option<(f64, &str)> = None;
    for line in text.lines() {
        let Some(stamp) = log_line_stamp(line, today, local.year()) else {
            continue;
        };
        let lower = line.to_ascii_lowercase();
        let quota = lower.contains("quota_manager.go")
            || lower.contains("dorefreshquota")
            || lower.contains("retrieveuserquotasummary")
            || lower.contains("quota summary")
            || lower.contains("quota_summary")
            || lower.contains("quota-summary");
        let error = lower.contains("resource_exhausted")
            || lower.contains("resource exhausted")
            || lower.contains("rate limit")
            || lower.contains("rate_limit")
            || lower.contains("too many requests")
            || lower.contains("quota exceeded")
            || lower.contains("quota_exceeded")
            || lower.contains("quota exhausted")
            || lower.contains("429");
        if !quota && !error {
            continue;
        }
        if stamp <= now && newest.is_none_or(|current| stamp >= current.0) {
            newest = Some((stamp, line));
        }
    }
    let (stamp, line) = newest?;
    let lower = line.to_ascii_lowercase();
    let failed = lower.contains("resource_exhausted")
        || lower.contains("resource exhausted")
        || lower.contains("rate limit")
        || lower.contains("rate_limit")
        || lower.contains("too many requests")
        || lower.contains("quota exceeded")
        || lower.contains("quota_exceeded")
        || lower.contains("quota exhausted")
        || lower.contains("429");
    if !failed {
        return None;
    }
    Some(json!({"used_up_at": iso_utc(stamp), "state": "OFF", "used_up": true}))
}

pub fn read_google(env: &Env, cfg: &Value, now: f64) -> Pool {
    let mut pool = Pool {
        state: Some("unknown".to_string()),
        chat_only: true,
        shared_with_chats: false,
        unit: Some("local history entries today (UTC)".to_string()),
        source: Some("agy local history timestamps; Antigravity quota unavailable".to_string()),
        ..Pool::default()
    };
    if let Some(used) = google_history_today(env, now) {
        pool.used = Some(used as f64);
    }
    let groups = agy_usage(env, now)
        .map(|text| parse_agy_usage(&text))
        .unwrap_or_default();
    if groups.contains_key("gemini") {
        let read = agy_pool(&groups, now, cfg);
        return Pool {
            used: pool.used,
            ..read
        };
    }
    pool.estimated = true;
    if let Some(quota) = google_quota_used_up(env, now) {
        if let Some(object) = quota.as_object() {
            if let Some(state) = object.get("state").and_then(Value::as_str) {
                pool.state = Some(state.to_string());
            }
            pool.used_up = object.get("used_up").and_then(Value::as_bool).unwrap_or(false);
            pool.used_up_at = object
                .get("used_up_at")
                .and_then(Value::as_str)
                .map(str::to_string);
        }
    }
    pool
}

pub fn parse_onepassword(text: &str) -> Option<Value> {
    for line in text.lines() {
        let fields: Vec<&str> = line.splitn(6, char::is_whitespace).collect();
        let fields: Vec<&str> = fields
            .into_iter()
            .filter(|field| !field.is_empty())
            .collect();
        if fields.len() != 6 || fields[0] != "account" || fields[1] != "read_write" {
            continue;
        }
        let (Ok(limit), Ok(used), Ok(remaining)) = (
            fields[2].parse::<i64>(),
            fields[3].parse::<i64>(),
            fields[4].parse::<i64>(),
        ) else {
            continue;
        };
        if limit > 0 && used >= 0 && remaining >= 0 {
            return Some(json!({"used": used, "limit": limit, "reset": fields[5]}));
        }
    }
    None
}

pub fn read_onepassword(env: &Env) -> Value {
    let path = env.home_file(ONE_PASSWORD_ENV);
    let unavailable = |kind: &str| json!({"error": format!("ratelimit unavailable ({kind})")});
    let Ok(text) = std::fs::read_to_string(&path) else {
        return unavailable("OSError");
    };
    let Some((name, token)) = text.trim().split_once('=') else {
        return unavailable("ValueError");
    };
    if name != "OP_SERVICE_ACCOUNT_TOKEN" || token.is_empty() {
        return unavailable("ValueError");
    }
    let mut command = Command::new("op");
    command.args(["service-account", "ratelimit"]).env(
        "OP_SERVICE_ACCOUNT_TOKEN",
        token,
    );
    let output = run_with_timeout(&mut command, None, Duration::from_secs(20));
    match output {
        Some(output) if output.status.success() => String::from_utf8(output.stdout)
            .ok()
            .and_then(|text| parse_onepassword(&text))
            .unwrap_or_else(|| unavailable("ValueError")),
        Some(_) => unavailable("CalledProcessError"),
        None => unavailable("TimeoutExpired"),
    }
}

pub fn onepassword_note(snapshot: &Value) -> String {
    let budget = snapshot.get("onepassword").cloned().unwrap_or(Value::Null);
    let used = budget.get("used").and_then(Value::as_i64);
    let limit = budget.get("limit").and_then(Value::as_i64);
    let failed = budget.get("error").is_some_and(|error| !error.is_null());
    let (Some(used), Some(limit)) = (used, limit) else {
        return "1Password: count unknown.".to_string();
    };
    if failed || used < 0 || limit <= 0 {
        return "1Password: count unknown.".to_string();
    }
    let mut note = format!("1Password: {used}/{limit} reads today.");
    if used as f64 >= limit as f64 * 0.5 {
        note.push_str(" Back off: no new key lookups unless the job needs one; read each key once per job.");
    }
    if used as f64 >= limit as f64 * 0.8 {
        note.push_str(" Frozen: key lookups only for live production work; tell Valentin.");
    }
    note
}

pub fn pressure_step(previous: &Value, tight_now: bool, runs: u32) -> Value {
    let state = previous
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or("ok");
    let on = previous.get("on_streak").and_then(Value::as_u64).unwrap_or(0);
    let off = previous
        .get("off_streak")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let (on, off) = if tight_now {
        (on + 1, 0)
    } else {
        (0, off + 1)
    };
    let next = match state {
        "ok" if on >= runs as u64 => "tight",
        "tight" if off >= runs as u64 => "ok",
        other => other,
    };
    json!({"state": next, "on_streak": on, "off_streak": off})
}

fn process_totals(env: &Env) -> BTreeMap<String, (u64, f64)> {
    let mut totals: BTreeMap<String, (u64, f64)> = PROCESS_NAMES
        .iter()
        .map(|name| ((*name).to_string(), (0, 0.0)))
        .collect();
    let mut command = Command::new(&env.ps);
    command.args(["-eo", "comm=,rss="]);
    let Some(output) = run_with_timeout(&mut command, None, Duration::from_secs(20)) else {
        return totals;
    };
    let text = String::from_utf8_lossy(&output.stdout);
    for line in text.lines() {
        let Some((name, rss)) = line.rsplit_once(char::is_whitespace) else {
            continue;
        };
        let name = name.trim();
        let Ok(rss) = rss.trim().parse::<u64>() else {
            continue;
        };
        if let Some(entry) = totals.get_mut(name) {
            entry.0 += 1;
            entry.1 += rss as f64 / 1_048_576.0;
        }
    }
    totals
}

fn meminfo() -> Option<(f64, f64)> {
    let text = std::fs::read_to_string("/proc/meminfo").ok()?;
    let mut total = 0.0;
    let mut available = 0.0;
    for line in text.lines() {
        let Some((key, rest)) = line.split_once(':') else {
            continue;
        };
        let value = rest
            .split_whitespace()
            .next()
            .and_then(|value| value.parse::<f64>().ok())
            .unwrap_or(0.0);
        match key {
            "MemAvailable" => available = value,
            "MemTotal" => total = value,
            _ => {}
        }
    }
    Some((available, total))
}

fn load_average() -> (f64, f64) {
    let text = std::fs::read_to_string("/proc/loadavg").unwrap_or_default();
    let mut parts = text.split_whitespace();
    let load1 = parts
        .next()
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or(0.0);
    let load5 = parts
        .next()
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or(0.0);
    (load1, load5)
}

pub fn read_resources(env: &Env) -> Value {
    let (available, total) = meminfo().unwrap_or((0.0, 0.0));
    let (load1, load5) = load_average();
    let cores = std::thread::available_parallelism()
        .map(|cores| cores.get())
        .unwrap_or(1);
    let totals = process_totals(env);
    let mut row = Map::new();
    row.insert("ram_free_gb".to_string(), json!(round_to(available / 1_048_576.0, 1)));
    row.insert("ram_total_gb".to_string(), json!(round_to(total / 1_048_576.0, 1)));
    row.insert("load1".to_string(), json!(round_to(load1, 2)));
    row.insert("load5".to_string(), json!(round_to(load5, 2)));
    row.insert("cores".to_string(), json!(cores));
    for name in PROCESS_NAMES {
        let (count, rss) = totals
            .get(name)
            .copied()
            .unwrap_or((0, 0.0));
        row.insert(
            name.to_string(),
            json!({"count": count, "rss_gb": round_to(rss, 2)}),
        );
    }
    Value::Object(row)
}

pub fn raw_tight(resources: &Value, cfg: &Value) -> bool {
    let free = resources
        .get("ram_free_gb")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    let load5 = resources
        .get("load5")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    let cores = resources
        .get("cores")
        .and_then(Value::as_f64)
        .unwrap_or(1.0);
    free < crate::collect::cfg_num(cfg, "tight_ram_free_gb", 16.0)
        || load5 > crate::collect::cfg_num(cfg, "tight_load_per_core", 2.0) * cores
}

fn read_lines(path: &Path) -> Vec<String> {
    std::fs::read_to_string(path)
        .map(|text| {
            text.lines()
                .filter(|line| !line.trim().is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

pub fn record_resources(env: &Env, cfg: &Value, now: f64) -> Value {
    let mut resources = read_resources(env);
    let pressure_path = env.state_file("pressure.json");
    let previous: Value = std::fs::read_to_string(&pressure_path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or(Value::Null);
    let tight = raw_tight(&resources, cfg);
    let step = pressure_step(&previous, tight, 2);
    if let Ok(text) = serde_json::to_string(&step) {
        let _ = crate::collect::write_atomic(&pressure_path, text.as_bytes());
    }
    let state = step
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or("ok")
        .to_string();
    if let Some(object) = resources.as_object_mut() {
        object.insert("pressure".to_string(), json!(state));
    }
    let history_path = env.state_file("resources.jsonl");
    let cutoff = now - 7.0 * 86400.0;
    let mut keep: Vec<String> = read_lines(&history_path)
        .into_iter()
        .filter(|line| {
            serde_json::from_str::<Value>(line)
                .ok()
                .and_then(|row| row.get("ts").and_then(Value::as_f64))
                .is_some_and(|ts| ts > cutoff)
        })
        .collect();
    let mut row = match resources.clone() {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    row.insert("ts".to_string(), json!(now));
    let record = Value::Object(row.clone());
    keep.push(serde_json::to_string(&record).unwrap_or_default());
    let _ = crate::collect::write_atomic(
        &history_path,
        format!("{}\n", keep.join("\n")).as_bytes(),
    );
    resources
}

const REMOTE_CLAUDE: &str = r#"
import json, os, urllib.request
h = os.path.expanduser("~")
out = {"email": json.load(open(h + "/.claude.json")).get("oauthAccount", {}).get("emailAddress")}
tok = json.load(open(h + "/.claude/.credentials.json"))["claudeAiOauth"]["accessToken"]
r = urllib.request.Request("https://api.anthropic.com/api/oauth/usage",
                           headers={"Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20"})
d = json.load(urllib.request.urlopen(r, timeout=20))
out.update({k: d.get(k) for k in ("five_hour", "seven_day")})
print(json.dumps(out))
"#;

#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq)]
pub struct RemoteRead {
    pub five_hour: Window,
    pub week: Window,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    pub error: Option<String>,
}

pub fn read_remote_claude(env: &Env, host: &str, user: Option<&str>, now: f64) -> RemoteRead {
    let empty = |error: Option<String>| RemoteRead {
        five_hour: window(None, None, SHORT_MINUTES, now),
        week: window(None, None, WEEK_MINUTES, now),
        email: None,
        error,
    };
    let mut command = Command::new(&env.ssh);
    command.args(["-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host]);
    match user {
        Some(user) => command.args(["sudo", "-n", "-u", user, "python3", "-"]),
        None => command.args(["python3", "-"]),
    };
    let Some(output) = run_with_timeout(&mut command, Some(REMOTE_CLAUDE), Duration::from_secs(40))
    else {
        return empty(Some(format!("ssh {host}: timeout")));
    };
    let text = String::from_utf8_lossy(&output.stdout);
    let last = text.lines().rev().find(|line| !line.trim().is_empty());
    let Some(last) = last else {
        return empty(Some(format!("ssh {host}: no reading")));
    };
    let Ok(data) = serde_json::from_str::<Value>(last.trim()) else {
        return empty(Some(format!("ssh {host}: bad reading")));
    };
    let five = data.get("five_hour").cloned().unwrap_or(Value::Null);
    let week = data.get("seven_day").cloned().unwrap_or(Value::Null);
    RemoteRead {
        five_hour: window(
            percentage(bar_field(&five, "utilization")),
            bar_reset(&five),
            SHORT_MINUTES,
            now,
        ),
        week: window(
            percentage(bar_field(&week, "utilization")),
            bar_reset(&week),
            WEEK_MINUTES,
            now,
        ),
        email: data
            .get("email")
            .and_then(Value::as_str)
            .map(str::to_string),
        error: None,
    }
}

pub fn read_openrouter(env: &Env) -> Value {
    let Some(key) = env_text("OPENROUTER_API_KEY") else {
        return json!({"usage": null, "limit": null, "error": "OPENROUTER_API_KEY not set"});
    };
    match http_get_json(&env.endpoints.openrouter_key, &bearer(&key)) {
        Ok(body) => {
            let data = body.get("data").cloned().unwrap_or(Value::Null);
            json!({
                "usage": data.get("usage").cloned().unwrap_or(Value::Null),
                "limit": data.get("limit").cloned().unwrap_or(Value::Null),
                "error": null,
            })
        }
        Err(error) => json!({"usage": null, "limit": null, "error": error}),
    }
}

pub fn standalone_note(account: &Value, others: &[Value], now: f64) -> String {
    if let Some(error) = account.get("error").and_then(Value::as_str) {
        return error.to_string();
    }
    let email = account.get("email").and_then(Value::as_str);
    let provider = account.get("provider").and_then(Value::as_str);
    if let Some(email) = email {
        let same = others.iter().find(|other| {
            other.get("provider").and_then(Value::as_str) == provider
                && other.get("email").and_then(Value::as_str) == Some(email)
        });
        if let Some(same) = same {
            let label = same.get("label").and_then(Value::as_str).unwrap_or("");
            let mut note = format!("Logged in as {email}, the same plan as '{label}', so it shares that bar.");
            let expected = account.get("expected_email").and_then(Value::as_str);
            if let Some(expected) = expected.filter(|expected| *expected != email) {
                note.push_str(&format!(
                    " Expected {expected}: that plan is not being used at all."
                ));
            }
            return note;
        }
    }
    let week = account.get("week").cloned().unwrap_or(Value::Null);
    let used = week.get("used").and_then(Value::as_f64).unwrap_or(0.0);
    let gone = week.get("time_gone").and_then(Value::as_f64).unwrap_or(0.0);
    let hours_left = week.get("hours_left").and_then(Value::as_f64).unwrap_or(0.0);
    let behind = gone - used;
    let resets: Vec<String> = account
        .get("resets")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| row.get("expires").and_then(Value::as_str))
                .filter(|expires| parse_stamp(expires).is_some_and(|epoch| epoch as f64 > now))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    if !resets.is_empty() && used >= 95.0 && hours_left > 24.0 {
        let first = resets.iter().min().cloned().unwrap_or_default();
        let head: String = first.chars().take(10).collect();
        let email = email.unwrap_or("");
        return format!(
            "{used:.0}% full with {hours_left:.0} h to the refill: press the free reset expiring {head} in Claude settings, Usage ({email})."
        );
    }
    let host = account.get("host").and_then(Value::as_str).unwrap_or("");
    if behind > 10.0 {
        return format!("{behind:.0} points behind pace: give sessions on {host} more work.");
    }
    if behind < -10.0 {
        return format!("{:.0} points ahead of pace: slow down on {host} or it runs out early.", -behind);
    }
    "On pace.".to_string()
}

pub fn cloud_maps(cfg: &Value, stored: &Value) -> Value {
    let declared = cfg.get("cloud").cloned().unwrap_or(Value::Null);
    let map = |side: &str| -> Map<String, Value> {
        let repos = match declared.get(side) {
            Some(Value::Array(items)) => items.clone(),
            Some(Value::Object(items)) => items.keys().cloned().map(Value::String).collect(),
            _ => Vec::new(),
        };
        let ids = stored.get(side).cloned().unwrap_or(Value::Null);
        let mut out = Map::new();
        for repo in repos {
            let Some(name) = repo.as_str() else {
                continue;
            };
            let id = ids.get(name).cloned().unwrap_or(Value::Null);
            if id.is_null() {
                continue;
            }
            out.insert(name.to_string(), id);
        }
        out
    };
    json!({"codex": map("codex"), "claude": map("claude")})
}

pub fn read_cloud_envs(env: &Env) -> Value {
    std::fs::read_to_string(env.state_file("cloud-envs.json"))
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .filter(Value::is_object)
        .unwrap_or(Value::Null)
}

pub fn history_rows(env: &Env) -> Vec<Value> {
    read_lines(&env.state_file("history.jsonl"))
        .into_iter()
        .filter_map(|line| serde_json::from_str::<Value>(&line).ok())
        .collect()
}
