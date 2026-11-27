use crate::config::{load_json_or_default, parse_stamp, Pool, Pools, Stampish};
use crate::ledger::{self, CreditSource};
use crate::runner;
use crate::usage::{
    self, cloud_maps, finite, iso_utc, onepassword_note, percentage, read_claude, read_cloud_envs,
    read_codex, read_grok, read_google, read_onepassword, read_openrouter, read_remote_claude,
    record_resources, round_to, run_with_timeout, standalone_note, window, window_pace, ClaudeRead,
    CodexRead, Pace, Sample, Window, SHORT_MINUTES, WEEK_MINUTES,
};
use chrono::{DateTime, Utc};
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

pub const ORCHESTRATOR_HELP: &str = "Orchestrators follow the pstack playbook and spawn their own workers. Spread workers fairly over every plan: read ~/.local/state/pacing/allocation.json; shares.<plan>.runners is the fleet's worker slots per plan; each of the four orchestrators takes an equal part; fill the plan furthest behind pace first (Claude subagents, Pi on DeepSeek via OpenCode Go Plus or Vercel, Codex, Grok via cursor-agent), never Claude alone. Plans reserved for Valentin stay off. No model id containing 'fast', no router or automatic fallback. No CI, builds, tests or AI review on this VPS; they run only on GitHub-hosted runners.";
const USER_AGENT: &str = "token-ladder/0.4 (+https://github.com/valentinyeo/token-ladder)";
const PUSH_TRIED: &str = "push_tried";
const PUSH_EVERY_SECONDS: f64 = 1800.0;
const STALE_SECONDS: f64 = 1800.0;

pub fn cfg_num(cfg: &Value, key: &str, fallback: f64) -> f64 {
    cfg.get(key)
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite())
        .unwrap_or(fallback)
}

pub fn cfg_text(cfg: &Value, key: &str) -> Option<String> {
    cfg.get(key)
        .and_then(Value::as_str)
        .map(|text| text.trim().to_string())
        .filter(|text| !text.is_empty())
}

pub fn cfg_list<'a>(cfg: &'a Value, key: &str) -> &'a [Value] {
    cfg.get(key)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

pub fn write_atomic(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("no parent directory for {}", path.display()))?;
    std::fs::create_dir_all(parent).map_err(|error| format!("cannot create {}: {error}", parent.display()))?;
    let stamp = usage::now_epoch();
    let tmp = parent.join(format!(
        ".{}-{}-{}",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("state"),
        std::process::id(),
        (stamp * 1_000_000.0) as i64
    ));
    std::fs::write(&tmp, bytes).map_err(|error| format!("cannot write {}: {error}", tmp.display()))?;
    match std::fs::rename(&tmp, path) {
        Ok(()) => Ok(()),
        Err(error) => {
            let _ = std::fs::remove_file(&tmp);
            Err(format!("cannot replace {}: {error}", path.display()))
        }
    }
}

pub fn write_state(path: &Path, value: &Value) -> Result<(), String> {
    let text = serde_json::to_string_pretty(value).map_err(|error| error.to_string())?;
    write_atomic(path, format!("{text}\n").as_bytes())
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct PoolState {
    pub used_pct: Option<f64>,
    pub hours_left: Option<f64>,
    pub resets_at: Option<f64>,
    pub state: String,
    pub window_minutes: Option<u64>,
    pub elapsed_pct: Option<f64>,
    pub pace_ratio: Option<f64>,
}

impl PoolState {
    fn unknown() -> Self {
        PoolState {
            state: "unknown".to_string(),
            ..PoolState::default()
        }
    }

    pub fn into_pool(self) -> Pool {
        Pool {
            state: Some(self.state),
            used_pct: self.used_pct,
            pace_ratio: self.pace_ratio,
            resets_at: self.resets_at.map(Stampish::Epoch),
            hours_left: self.hours_left,
            window_minutes: self.window_minutes,
            elapsed_pct: self.elapsed_pct,
            ..Pool::default()
        }
    }
}

fn rolled(bar: &Window, minutes: u64, now: f64) -> Option<Window> {
    let mut bar = bar.clone();
    if let Some(reset) = bar.resets_at.filter(|reset| reset.is_finite()) {
        if reset <= now {
            let period = minutes as f64 * 60.0;
            let steps = ((now - reset) / period).floor() + 1.0;
            bar = window(Some(0.0), Some(reset + steps.max(1.0) * period), minutes, now);
        } else {
            bar.hours_left = Some(round_to((reset - now) / 3600.0, 1));
            if bar.time_gone.is_some() {
                bar.time_gone = Some(
                    (100.0 * (1.0 - (reset - now) / (minutes as f64 * 60.0))).clamp(0.0, 100.0),
                );
            }
        }
    }
    bar.used.filter(|used| used.is_finite()).map(|_| bar)
}

pub fn pool_state(
    week: Option<&Window>,
    five_hour: Option<&Window>,
    cfg: &Value,
    now: f64,
    five_off_pct: Option<f64>,
) -> PoolState {
    let mut bars: Vec<(Window, u64)> = Vec::new();
    let mut short_capped = false;
    for (bar, minutes) in [(week, WEEK_MINUTES), (five_hour, SHORT_MINUTES)] {
        let Some(bar) = bar else {
            continue;
        };
        let Some(bar) = rolled(bar, minutes, now) else {
            continue;
        };
        if minutes == SHORT_MINUTES
            && five_off_pct.is_some_and(|off| bar.used.unwrap_or(0.0) >= off)
        {
            short_capped = true;
        }
        bars.push((bar, minutes));
    }
    if bars.is_empty() {
        return PoolState::unknown();
    }
    let warn = cfg_num(cfg, "pool_warn_pct", 80.0);
    let off = cfg_num(cfg, "pool_off_pct", 95.0);
    let mut state = "ON";
    if short_capped || bars.iter().any(|(bar, _)| bar.used.unwrap_or(0.0) >= off) {
        state = "OFF";
    } else if bars.iter().any(|(bar, _)| {
        let used = bar.used.unwrap_or(0.0);
        let gone = bar.time_gone.unwrap_or(0.0);
        used >= warn || (gone >= warn && used >= gone)
    }) {
        state = "WARN";
    }
    let limiting = bars
        .iter()
        .max_by(|left, right| {
            left.0
                .used
                .unwrap_or(0.0)
                .partial_cmp(&right.0.used.unwrap_or(0.0))
                .unwrap_or(std::cmp::Ordering::Equal)
        })
        .map(|(bar, minutes)| (bar.clone(), *minutes));
    let Some((limiting, minutes)) = limiting else {
        return PoolState::unknown();
    };
    let mut ratios: Vec<f64> = Vec::new();
    for (bar, span) in &bars {
        let floor = if *span >= 1440 {
            usage::PACE_MIN_WEEK_ELAPSED_PCT
        } else {
            usage::PACE_MIN_SHORT_ELAPSED_PCT
        };
        if !(*span >= 1440
            || bar.time_gone.unwrap_or(0.0) >= usage::PACE_MIN_SHORT_ELAPSED_PCT)
        {
            continue;
        }
        let probe = Window {
            time_gone: Some(bar.time_gone.unwrap_or(0.0).max(floor)),
            ..bar.clone()
        };
        if let Some(ratio) = window_pace(&probe, &[], now).ratio {
            ratios.push(ratio);
        }
    }
    PoolState {
        used_pct: limiting.used,
        hours_left: limiting.hours_left,
        resets_at: limiting.resets_at,
        state: state.to_string(),
        window_minutes: Some(minutes),
        elapsed_pct: limiting.time_gone,
        pace_ratio: ratios
            .into_iter()
            .fold(None, |best: Option<f64>, ratio| {
                Some(best.map_or(ratio, |best| best.max(ratio)))
            }),
    }
}

fn pace_warning(name: &str, bar: &Window, bonus_resets: u32) -> Option<String> {
    let used = percentage(bar.used)?;
    let hours_left = finite(bar.hours_left)?;
    if used <= 0.0 {
        return None;
    }
    let gone = 168.0 - hours_left;
    if gone <= 0.0 {
        return None;
    }
    let rate = used / gone;
    let empty_in = (100.0 - used + 100.0 * bonus_resets as f64) / rate;
    let early = hours_left - empty_in;
    if early <= 0.0 {
        return None;
    }
    let gap = if early >= 48.0 {
        format!("{:.0} days", early / 24.0)
    } else {
        format!("{early:.0} h")
    };
    Some(format!(
        "{name} over pace: at this rate empty in {empty_in:.0} h, {gap} before reset"
    ))
}

pub fn live_resets(cfg: &Value, key: &str, now: f64) -> Vec<(f64, String)> {
    let mut resets: Vec<(f64, String)> = cfg_list(cfg, key)
        .iter()
        .filter_map(|row| {
            let expires = row.get("expires").and_then(Value::as_str)?;
            let epoch = parse_stamp(expires)? as f64;
            (epoch > now).then(|| (epoch, expires.to_string()))
        })
        .collect();
    resets.sort_by(|left, right| {
        left.0
            .partial_cmp(&right.0)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    resets
}

fn reset_text(epoch: f64) -> String {
    DateTime::<Utc>::from_timestamp(epoch as i64, 0)
        .map(|stamp| stamp.format("%Y-%m-%d %H:%M UTC").to_string())
        .unwrap_or_else(|| "unknown".to_string())
}

fn burn_entry(bar: &Window, cfg: &Value, key: &str, now: f64, label: &str) -> Value {
    let resets = live_resets(cfg, key, now);
    let (epoch, hours) = match resets.first() {
        Some((epoch, _)) => (Some(*epoch), Some((*epoch - now) / 3600.0)),
        None => (None, None),
    };
    let used = finite(bar.used);
    let left = used.map(|used| (100.0 - used).max(0.0));
    let burn_now = hours.is_some_and(|hours| hours <= cfg_num(cfg, "reset_burn_hours", 24.0))
        && used.unwrap_or(0.0) < cfg_num(cfg, "reset_use_at_percent", 95.0);
    json!({
        "left": left,
        "reset_hours": hours,
        "reset": epoch.map(reset_text),
        "burn_now": burn_now,
        "pace": pace_warning(label, bar, resets.len() as u32),
    })
}

pub fn burn_plan(claude: &ClaudeRead, codex: &CodexRead, cfg: &Value, now: f64) -> Value {
    json!({
        "claude": burn_entry(&claude.week, cfg, "claude_resets", now, "Claude"),
        "codex": burn_entry(&codex.week, cfg, "codex_resets", now, "Codex"),
    })
}

pub fn decide(
    claude: &ClaudeRead,
    codex: &CodexRead,
    cfg: &Value,
    now: f64,
    override_mode: Option<&str>,
) -> (String, String, Vec<String>) {
    let floor_base = cfg_num(cfg, "claude_floor_percent", 15.0);
    let cap5 = cfg_num(cfg, "five_hour_max_percent", 80.0);
    let use_at = cfg_num(cfg, "reset_use_at_percent", 95.0);
    let min_before = cfg_num(cfg, "reset_min_hours_before_weekly", 24.0);
    let burn_hours = cfg_num(cfg, "reset_burn_hours", 24.0);
    let cw = claude.week.clone();
    let c5 = claude.five_hour.clone();
    let xw = codex.week.clone();
    let mut advice: Vec<String> = Vec::new();
    let claude_resets = live_resets(cfg, "claude_resets", now);
    let codex_resets = live_resets(cfg, "codex_resets", now);
    let burn = burn_plan(claude, codex, cfg, now);
    let soon = burn
        .get("claude")
        .and_then(|claude| claude.get("reset_hours"))
        .and_then(Value::as_f64)
        .is_some_and(|hours| hours <= burn_hours);
    let claude_used = cw.used;
    if let (Some(first), Some(used)) = (claude_resets.first(), claude_used) {
        let head: String = first.1.chars().take(16).collect();
        if used >= use_at && (soon || cw.hours_left.unwrap_or(0.0) > min_before) {
            advice.push(format!(
                "Use the Claude reset expiring {head}: Claude is {used:.0}% full. Valentin presses it in Claude settings, Usage."
            ));
        } else if soon {
            advice.push(format!(
                "BURN CLAUDE: the Claude reset expires {head} and Claude is only {used:.0}% used. Every department burns Claude now as well: Agent subagents are allowed in any mode, plus one Claude cloud session per runner with a PR for its claimed ticket."
            ));
        }
    } else if soon {
        advice.push(
            "BURN CLAUDE: a Claude reset expires soon and Claude has room. Every department burns Claude now as well."
                .to_string(),
        );
    }
    let mut claude_ok = false;
    let mut claude_room = false;
    let mut claude_urg = 0.0;
    if let Some(used) = claude_used {
        let floor = f64::max(
            1.0,
            floor_base * (finite(cw.hours_left).unwrap_or(0.0) / 48.0).min(1.0),
        );
        let room = (100.0 - floor - used).max(0.0);
        let target = finite(cw.time_gone).unwrap_or(0.0).min(100.0 - floor);
        let behind = target - used;
        let hours = finite(cw.hours_left).unwrap_or(1.0);
        claude_urg = room / f64::max(hours, 1.0);
        claude_room = room > 0.0 && c5.used.unwrap_or(0.0) < cap5;
        claude_ok = claude_room && behind > cfg_num(cfg, "claude_behind_margin", 5.0);
    }
    let mut codex_ok = false;
    let mut codex_urg = 0.0;
    if let Some(used) = finite(xw.used) {
        codex_ok = used < 100.0 && codex.five_hour.used.unwrap_or(0.0) < cap5;
        let hours = finite(xw.hours_left).unwrap_or(1.0);
        codex_urg = (100.0 - used) / f64::max(hours, 1.0);
        if let Some((epoch, _)) = codex_resets.first() {
            let left = (*epoch - now) / 3600.0;
            codex_urg = codex_urg.max(100.0 / f64::max(left, 1.0));
            let head: String = reset_text(*epoch).chars().take(10).collect();
            if used >= use_at && finite(xw.hours_left).unwrap_or(0.0) > min_before {
                advice.push(format!(
                    "Use the Codex reset expiring {head}: Codex is {used:.0}% full and its weekly refill is {:.0} h away. Valentin presses it in ChatGPT settings.",
                    finite(xw.hours_left).unwrap_or(0.0)
                ));
            } else if left < 72.0 && used < 60.0 {
                advice.push(format!(
                    "Codex reset expiring {head} is close and Codex is only {used:.0}% used: push more work to Codex."
                ));
            }
        }
    }
    if override_mode == Some("claude")
        && claude_used.is_some_and(|used| used >= 100.0 - floor_base)
    {
        return (
            "codex".to_string(),
            format!(
                "forced Claude ended: Claude is {:.0}% used and reached its {floor_base:.0}% floor, back to Codex",
                claude_used.unwrap_or(0.0)
            ),
            advice,
        );
    }
    if let Some(forced) = override_mode.filter(|mode| *mode == "claude" || *mode == "codex") {
        return (
            forced.to_string(),
            format!("forced by Valentin ({forced})"),
            advice,
        );
    }
    if claude_ok && (claude_urg > codex_urg || !codex_ok) {
        let why = format!(
            "Claude is behind: {:.0}% used with {:.0}% of its week gone, resets in {:.0} h",
            claude_used.unwrap_or(0.0),
            finite(cw.time_gone).unwrap_or(0.0),
            finite(cw.hours_left).unwrap_or(0.0)
        );
        return ("claude".to_string(), why, advice);
    }
    if codex_ok {
        return (
            "codex".to_string(),
            "Codex has room and Claude is on pace or near its floor".to_string(),
            advice,
        );
    }
    if claude_room {
        return (
            "claude".to_string(),
            "Codex is out, Claude still has room above the floor".to_string(),
            advice,
        );
    }
    advice.push(
        "Both subscriptions are out or capped. OpenRouter needs Valentin's yes; a Codex reset needs Valentin to press it."
            .to_string(),
    );
    (
        "codex".to_string(),
        "both out: slow down and ask Valentin".to_string(),
        advice,
    )
}

fn codex_credits_left(codex: &CodexRead, cfg: &Value) -> Option<f64> {
    let rule = cfg.get("codex_credits").cloned().unwrap_or(Value::Null);
    let approved = rule
        .get("approved")
        .and_then(Value::as_str)
        .map(|text| !text.trim().is_empty())
        .unwrap_or(false);
    let floor = rule
        .get("floor")
        .and_then(Value::as_f64)
        .unwrap_or(10_000.0);
    let balance = finite(codex.credits)?;
    if !approved || balance <= floor {
        return None;
    }
    Some(balance)
}

pub fn current_pools(env: &usage::Env, cfg: &Value, claude: &ClaudeRead, codex: &CodexRead, now: f64) -> Pools {
    let mut pools = Pools::new();
    let claude_state = pool_state(
        Some(&claude.week),
        Some(&claude.five_hour),
        cfg,
        now,
        Some(95.0),
    );
    pools.insert("claude".to_string(), claude_state.into_pool());
    let mut codex_pool = pool_state(
        Some(&codex.week),
        Some(&codex.five_hour),
        cfg,
        now,
        None,
    )
    .into_pool();
    if let Some(credits) = codex_credits_left(codex, cfg) {
        if matches!(codex_pool.state_label().as_str(), "WARN" | "OFF") {
            codex_pool.state = Some("ON".to_string());
            codex_pool.credits = Some(credits);
        }
    }
    pools.insert("codex".to_string(), codex_pool);
    let grok = read_grok(env, cfg, now);
    pools.insert(
        "grok".to_string(),
        pool_state(grok.as_ref(), None, cfg, now, None).into_pool(),
    );
    pools.insert("google".to_string(), read_google(env, cfg, now));
    pools
}

struct AccountRow {
    id: String,
    label: String,
    provider: String,
    week: Window,
    five_hour: Window,
    error: Option<String>,
    sampled_at: Option<f64>,
    email: Option<String>,
    host: String,
    in_ladder: bool,
}

impl AccountRow {
    fn into_value(self, cfg: &Value, now: f64) -> Value {
        let stale = self.error.is_some()
            || self.week.used.is_none()
            || self
                .sampled_at
                .is_some_and(|sampled| now - sampled > 3.0 * 3600.0);
        let cluster = if self.in_ladder {
            cfg_text(cfg, "ladder_name")
        } else {
            Some(self.label.clone())
        };
        json!({
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "email": self.email,
            "host": self.host,
            "in_ladder": self.in_ladder,
            "cluster": cluster,
            "week": self.week,
            "five_hour": self.five_hour,
            "stale": stale,
            "error": self.error,
            "note": Value::Null,
        })
    }
}

fn expanduser(path: &str, home: &Path) -> PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => home.join(rest),
        None => PathBuf::from(path),
    }
}

pub fn build_accounts(env: &usage::Env, cfg: &Value, claude: &ClaudeRead, codex: &CodexRead, now: f64) -> Vec<Value> {
    let mut rows: Vec<Value> = Vec::new();
    rows.push(
        AccountRow {
            id: "claude".to_string(),
            label: cfg_text(cfg, "claude_label").unwrap_or_else(|| "Claude".to_string()),
            provider: "claude".to_string(),
            week: claude.week.clone(),
            five_hour: claude.five_hour.clone(),
            error: claude.error.clone(),
            sampled_at: None,
            email: claude.email.clone(),
            host: "primary+mirror".to_string(),
            in_ladder: true,
        }
        .into_value(cfg, now),
    );
    rows.push(
        AccountRow {
            id: "codex".to_string(),
            label: cfg_text(cfg, "codex_label").unwrap_or_else(|| "Codex".to_string()),
            provider: "codex".to_string(),
            week: codex.week.clone(),
            five_hour: codex.five_hour.clone(),
            error: codex.error.clone(),
            sampled_at: codex.sampled_at,
            email: codex.email.clone(),
            host: "primary+mirror".to_string(),
            in_ladder: true,
        }
        .into_value(cfg, now),
    );
    for extra in cfg_list(cfg, "extra_claude_accounts") {
        let Some(id) = extra.get("id").and_then(Value::as_str) else {
            continue;
        };
        let label = extra
            .get("label")
            .and_then(Value::as_str)
            .unwrap_or(id)
            .to_string();
        let credentials = extra
            .get("credentials")
            .and_then(Value::as_str)
            .map(|path| expanduser(path, &env.home));
        let reading = read_claude(env, now, credentials.as_deref());
        rows.push(
            AccountRow {
                id: id.to_string(),
                label,
                provider: "claude".to_string(),
                week: reading.week,
                five_hour: reading.five_hour,
                error: reading.error,
                sampled_at: None,
                email: reading.email,
                host: "primary+mirror".to_string(),
                in_ladder: false,
            }
            .into_value(cfg, now),
        );
    }
    for remote in cfg_list(cfg, "remote_claude_accounts") {
        let Some(id) = remote.get("id").and_then(Value::as_str) else {
            continue;
        };
        let Some(host) = remote.get("ssh").and_then(Value::as_str) else {
            continue;
        };
        let user = remote.get("user").and_then(Value::as_str);
        let label = remote
            .get("label")
            .and_then(Value::as_str)
            .unwrap_or(id)
            .to_string();
        let reading = read_remote_claude(env, host, user, now);
        let host_label = match user {
            Some(user) => format!("{user}@{host}"),
            None => host.to_string(),
        };
        let mut row = AccountRow {
            id: id.to_string(),
            label,
            provider: "claude".to_string(),
            week: reading.week,
            five_hour: reading.five_hour,
            error: reading.error,
            sampled_at: None,
            email: reading.email,
            host: host_label,
            in_ladder: false,
        }
        .into_value(cfg, now);
        if let Some(object) = row.as_object_mut() {
            object.insert(
                "expected_email".to_string(),
                remote
                    .get("expected_email")
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            object.insert(
                "resets".to_string(),
                remote.get("resets").cloned().unwrap_or_else(|| json!([])),
            );
        }
        let note = standalone_note(&row, &rows, now);
        if let Some(object) = row.as_object_mut() {
            object.insert("note".to_string(), json!(note));
        }
        rows.push(row);
    }
    rows
}

pub fn add_usage_pace(accounts: &mut [Value], history: &[Value], now: f64) {
    for account in accounts.iter_mut() {
        let Some(id) = account.get("id").and_then(Value::as_str).map(str::to_string) else {
            continue;
        };
        let stale = account
            .get("stale")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        for name in ["week", "five_hour"] {
            let mut samples: Vec<Sample> = Vec::new();
            for row in history {
                let previous = row
                    .get("accounts")
                    .and_then(|accounts| accounts.get(&id))
                    .and_then(|account| account.get(name));
                let (used, resets_at) = match previous {
                    Some(values) if values.is_object() => (
                        values.get("used").and_then(Value::as_f64),
                        values.get("resets_at").and_then(Value::as_f64),
                    ),
                    _ => {
                        let legacy = match (id.as_str(), name) {
                            ("claude", "week") => "claude_week",
                            ("claude", _) => "claude_5h",
                            ("codex", _) => "codex_week",
                            _ => "",
                        };
                        (
                            if legacy.is_empty() {
                                None
                            } else {
                                row.get(legacy).and_then(Value::as_f64)
                            },
                            None,
                        )
                    }
                };
                samples.push(Sample {
                    ts: row.get("ts").and_then(Value::as_f64).unwrap_or(f64::NAN),
                    used,
                    resets_at,
                });
            }
            let Some(bar) = account
                .get(name)
                .cloned()
                .and_then(|bar| serde_json::from_value::<Window>(bar).ok())
            else {
                continue;
            };
            let source = if stale { Window::default() } else { bar };
            let pace: Pace = window_pace(&source, &samples, now);
            if let Some(object) = account.get_mut(name).and_then(Value::as_object_mut) {
                object.insert(
                    "pace".to_string(),
                    serde_json::to_value(pace).unwrap_or(Value::Null),
                );
            }
        }
    }
}

pub fn usage_history_record(snapshot: &Value) -> Value {
    let mut accounts = Map::new();
    for account in snapshot
        .get("accounts")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
    {
        let Some(id) = account.get("id").and_then(Value::as_str) else {
            continue;
        };
        let stale = account
            .get("stale")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let mut bars = Map::new();
        for name in ["week", "five_hour"] {
            let bar = account.get(name).cloned().unwrap_or(Value::Null);
            bars.insert(
                name.to_string(),
                json!({
                    "used": if stale { Value::Null } else { bar.get("used").cloned().unwrap_or(Value::Null) },
                    "resets_at": bar.get("resets_at").cloned().unwrap_or(Value::Null),
                }),
            );
        }
        accounts.insert(id.to_string(), Value::Object(bars));
    }
    let used = |name: &str, slot: &str| {
        snapshot
            .get(name)
            .and_then(|account| account.get(slot))
            .and_then(|bar| bar.get("used"))
            .cloned()
            .unwrap_or(Value::Null)
    };
    json!({
        "ts": snapshot.get("ts").cloned().unwrap_or(Value::Null),
        "mode": snapshot.get("mode").cloned().unwrap_or(Value::Null),
        "claude_week": used("claude", "week"),
        "claude_5h": used("claude", "five_hour"),
        "codex_week": used("codex", "week"),
        "accounts": accounts,
    })
}

pub fn admin_dashboard_url(env: &usage::Env) -> String {
    let path = env.state_file("dashboard.html");
    let resolved = std::fs::canonicalize(&path).unwrap_or(path);
    let mut input = resolved.display().to_string();
    input.push('\n');
    let digest = runner::sha256(input.as_bytes());
    let hex: String = digest.iter().take(4).map(|byte| format!("{byte:02x}")).collect();
    format!("https://admin.yeoux.net/p/{hex}/dashboard.html")
}

pub fn mode_line(snapshot: &Value, now: f64) -> String {
    let pools = snapshot.get("pools").cloned().unwrap_or(Value::Null);
    let state_of = |name: &str| {
        pools
            .get(name)
            .and_then(|pool| pool.get("state"))
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string()
    };
    let mut states = Vec::new();
    for name in ["claude", "codex", "grok", "google"] {
        let mut label = name.to_string();
        if let Some(first) = label.get(0..1) {
            label = format!("{}{}", first.to_uppercase(), &label[1..]);
        }
        states.push(format!("{label} {}", state_of(name)));
    }
    let age = snapshot
        .get("ts")
        .and_then(Value::as_f64)
        .map(|ts| now - ts)
        .unwrap_or(0.0);
    let stale = if age > STALE_SECONDS {
        format!(" STALE: last check {:.0} min ago.", age / 60.0)
    } else {
        String::new()
    };
    let mut line = format!("Token ladder: {}.{stale} {ORCHESTRATOR_HELP}", states.join("; "));
    let resources = snapshot.get("resources").cloned().unwrap_or(Value::Null);
    if resources.get("pressure").and_then(Value::as_str) == Some("tight") {
        line.push_str(&format!(
            " Server tight (RAM {:.0} GB free, load {:.1}): pace local activity down.",
            resources
                .get("ram_free_gb")
                .and_then(Value::as_f64)
                .unwrap_or(0.0),
            resources.get("load5").and_then(Value::as_f64).unwrap_or(0.0)
        ));
    }
    line.push(' ');
    line.push_str(&onepassword_note(snapshot));
    line
}

pub fn read_override(env: &usage::Env) -> Option<String> {
    let text = std::fs::read_to_string(env.state_file("override")).ok()?;
    match text.trim() {
        "claude" => Some("claude".to_string()),
        "codex" => Some("codex".to_string()),
        _ => None,
    }
}

fn due(env: &usage::Env, name: &str, every: f64, now: f64) -> bool {
    let path = env.state_file(&format!("{name}_tried"));
    if let Ok(text) = std::fs::read_to_string(&path) {
        if let Ok(previous) = text.trim().parse::<f64>() {
            if now - previous < every {
                return false;
            }
        }
    }
    let _ = std::fs::write(&path, format!("{now}"));
    true
}

fn send_with(agent: &ureq::Agent, method: &str, url: &str, headers: &[(String, String)], body: &str) -> Result<u16, String> {
    let request = match method {
        "POST" => agent.post(url),
        _ => agent.put(url),
    };
    let mut request = request;
    for (name, value) in headers {
        request = request.header(name.as_str(), value.as_str());
    }
    match request.send(body) {
        Ok(response) => Ok(response.status().as_u16()),
        Err(ureq::Error::StatusCode(code)) => Err(format!("status {code}")),
        Err(error) => Err(usage::http_error(&error)),
    }
}

pub fn send_usage(url: &str, token: &str, body: &str) -> Result<u16, String> {
    let headers = vec![
        ("Authorization".to_string(), format!("Bearer {token}")),
        ("Content-Type".to_string(), "application/json".to_string()),
        ("User-Agent".to_string(), USER_AGENT.to_string()),
    ];
    let agent = usage::agent();
    let first = send_with(&agent, "POST", url, &headers, body);
    if let Ok(code) = first {
        if (200..300).contains(&code) {
            return Ok(code);
        }
    }
    send_with(&agent, "PUT", url, &headers, body)
}

pub fn push_status(env: &usage::Env, cfg: &Value, body: &str) -> bool {
    let Some(url) = cfg_text(cfg, "push_url") else {
        return false;
    };
    let args: Vec<String> = match cfg.get("push_token_key") {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect(),
        Some(Value::String(text)) => vec![text.clone()],
        _ => Vec::new(),
    };
    if args.is_empty() {
        return false;
    }
    let mut command = Command::new(&env.yeo_key);
    command.args(&args).stdin(Stdio::null());
    let Some(output) = run_with_timeout(&mut command, None, Duration::from_secs(60)) else {
        return false;
    };
    if !output.status.success() {
        return false;
    }
    let token = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if token.is_empty() {
        return false;
    }
    match send_usage(&url, &token, body) {
        Ok(code) => (200..300).contains(&code),
        Err(_) => false,
    }
}

fn run_quiet(command: &mut Command, timeout: Duration) {
    let _ = run_with_timeout(command, None, timeout);
}

pub fn mirror(env: &usage::Env, cfg: &Value) -> usize {
    let primary = cfg_text(cfg, "primary_hostname").unwrap_or_else(|| env.hostname.clone());
    if primary != env.hostname {
        return 0;
    }
    let repo = runner::repo_root().unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(".."));
    let status = env.state_file("status.json");
    let mut done = 0;
    for target in cfg_list(cfg, "mirrors") {
        let Some(host) = target.get("ssh").and_then(Value::as_str) else {
            continue;
        };
        let mut rsync = Command::new(&env.rsync);
        rsync
            .args(["-a", "--delete", "--exclude", ".git", "--exclude", "__pycache__"])
            .arg(format!("{}/", repo.display()))
            .arg(format!("{host}:projects/token-ladder/"))
            .stdin(Stdio::null());
        run_quiet(&mut rsync, Duration::from_secs(60));
        let mut mkdir = Command::new(&env.ssh);
        mkdir
            .args(["-o", "BatchMode=yes", host, "mkdir -p .local/state/token-ladder"])
            .stdin(Stdio::null());
        run_quiet(&mut mkdir, Duration::from_secs(30));
        let mut copy = Command::new(&env.rsync);
        copy.args(["-a"])
            .arg(&status)
            .arg(format!("{host}:.local/state/token-ladder/status.json"))
            .stdin(Stdio::null());
        run_quiet(&mut copy, Duration::from_secs(60));
        for user in target
            .get("users")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[])
        {
            let Some(user) = user.as_str() else {
                continue;
            };
            let install = format!(
                "sudo -n install -D -o {user} -m 644 .local/state/token-ladder/status.json /home/{user}/.local/state/token-ladder/status.json && sudo -n install -D -o {user} -m 755 projects/token-ladder/bin/token-ladder /home/{user}/.local/bin/token-ladder && sudo -n install -D -o {user} -m 644 projects/token-ladder/config.json /home/{user}/.local/config.json"
            );
            let mut ssh = Command::new(&env.ssh);
            ssh.args(["-o", "BatchMode=yes", host, install.as_str()])
                .stdin(Stdio::null());
            run_quiet(&mut ssh, Duration::from_secs(30));
        }
        done += 1;
    }
    done
}

pub struct CollectRequest<'a> {
    pub env: &'a usage::Env,
    pub cfg: &'a Value,
    pub now: f64,
}

fn refresh_due_credits(env: &usage::Env, cfg: &Value, now: f64) {
    let names = ledger::credit_lane_names_from_value(cfg);
    if names.is_empty() {
        return;
    }
    let path = env.state_file("lane-credits.json");
    let data = ledger::read_credits(&path);
    let every = cfg_num(cfg, "credit_refresh_minutes", 30.0) * 60.0;
    let due: Vec<String> = names
        .into_iter()
        .filter(|name| {
            let at = data
                .get(name)
                .and_then(|entry| entry.get("at"))
                .and_then(Value::as_f64)
                .unwrap_or(0.0);
            now - at > every
        })
        .collect();
    if due.is_empty() {
        return;
    }
    let source = CreditSource::from_value(cfg);
    let _ = ledger::refresh_credits(env, &source, &due, now);
}

pub fn collect(request: &CollectRequest<'_>) -> Result<Value, String> {
    let env = request.env;
    let cfg = request.cfg;
    let now = request.now;
    std::fs::create_dir_all(&env.state)
        .map_err(|error| format!("cannot create {}: {error}", env.state.display()))?;
    let mut claude = read_claude(env, now, None);
    claude.email = usage::claude_email(env);
    let mut codex = read_codex(env, now);
    codex.email = usage::codex_email(env);
    let openrouter = read_openrouter(env);
    let override_mode = read_override(env);
    let (mode, why, advice) = decide(&claude, &codex, cfg, now, override_mode.as_deref());
    let forced = match override_mode {
        Some(ref forced) if *forced == "claude" && mode != "claude" => {
            let _ = std::fs::remove_file(env.state_file("override"));
            None
        }
        other => other,
    };
    let mut accounts = build_accounts(env, cfg, &claude, &codex, now);
    add_usage_pace(&mut accounts, &usage::history_rows(env), now);
    let pools = current_pools(env, cfg, &claude, &codex, now);
    let resources = record_resources(env, cfg, now);
    let pushed_at = std::fs::read_to_string(env.state_file("last_push_ok"))
        .ok()
        .and_then(|text| text.trim().parse::<f64>().ok());
    let mut snapshot = Map::new();
    snapshot.insert("ts".to_string(), json!(now));
    snapshot.insert("mode".to_string(), json!(mode));
    snapshot.insert("why".to_string(), json!(why));
    snapshot.insert("advice".to_string(), json!(advice));
    snapshot.insert(
        "override".to_string(),
        json!(forced.unwrap_or_else(|| "auto".to_string())),
    );
    snapshot.insert("global_resets".to_string(), Value::Null);
    snapshot.insert("claude".to_string(), serde_json::to_value(&claude).unwrap_or(Value::Null));
    snapshot.insert("codex".to_string(), serde_json::to_value(&codex).unwrap_or(Value::Null));
    snapshot.insert("openrouter".to_string(), openrouter);
    snapshot.insert("onepassword".to_string(), read_onepassword(env));
    snapshot.insert("config".to_string(), cfg.clone());
    snapshot.insert("accounts".to_string(), Value::Array(accounts));
    snapshot.insert("dashboard".to_string(), json!(admin_dashboard_url(env)));
    snapshot.insert("resources".to_string(), resources);
    snapshot.insert("burn".to_string(), burn_plan(&claude, &codex, cfg, now));
    snapshot.insert(
        "cloud".to_string(),
        cloud_maps(cfg, &read_cloud_envs(env)),
    );
    snapshot.insert(
        "pools".to_string(),
        serde_json::to_value(&pools).unwrap_or(Value::Null),
    );
    snapshot.insert("pace_alerts".to_string(), json!([]));
    snapshot.insert("pushed_at".to_string(), json!(pushed_at));
    let status_path = env.state_file("status.json");
    let snapshot = Value::Object(snapshot);
    write_state(&status_path, &snapshot)?;
    let record = usage_history_record(&snapshot);
    if let Ok(line) = serde_json::to_string(&record) {
        let history_path = env.state_file("history.jsonl");
        let mut text = std::fs::read_to_string(&history_path).unwrap_or_default();
        text.push_str(&line);
        text.push('\n');
        write_atomic(&history_path, text.as_bytes())?;
    }
    mirror(env, cfg);
    refresh_due_credits(env, cfg, now);
    let every = cfg_num(cfg, "key_lookup_every_s", PUSH_EVERY_SECONDS);
    if due(env, PUSH_TRIED, every, now) {
        let body = std::fs::read_to_string(&status_path).unwrap_or_default();
        if push_status(env, cfg, &body) {
            let _ = std::fs::write(env.state_file("last_push_ok"), format!("{now}"));
        }
    }
    let pushed_at = std::fs::read_to_string(env.state_file("last_push_ok"))
        .ok()
        .and_then(|text| text.trim().parse::<f64>().ok());
    let mut final_snapshot = snapshot;
    if let Some(object) = final_snapshot.as_object_mut() {
        object.insert("pushed_at".to_string(), json!(pushed_at));
    }
    write_state(&status_path, &final_snapshot)?;
    println!("{}", mode_line(&final_snapshot, now));
    Ok(final_snapshot)
}

pub fn set_override(env: &usage::Env, mode: &str) -> Result<(), String> {
    std::fs::create_dir_all(&env.state)
        .map_err(|error| format!("cannot create {}: {error}", env.state.display()))?;
    let path = env.state_file("override");
    if mode == "auto" {
        if path.exists() {
            std::fs::remove_file(&path).map_err(|error| error.to_string())?;
        }
        return Ok(());
    }
    std::fs::write(&path, mode).map_err(|error| error.to_string())
}

pub fn set_mode(request: &CollectRequest<'_>, mode: &str) -> Result<i32, String> {
    if !matches!(mode, "claude" | "codex" | "auto") {
        return Err("usage: token-ladder mode claude|codex|auto".to_string());
    }
    let primary = cfg_text(request.cfg, "primary_hostname")
        .unwrap_or_else(|| request.env.hostname.clone());
    if primary != request.env.hostname {
        let ssh = cfg_text(request.cfg, "primary_ssh")
            .ok_or_else(|| "no primary_ssh in config".to_string())?;
        let mut command = Command::new(&request.env.ssh);
        command
            .args([
                ssh.as_str(),
                "projects/token-ladder/bin/token-ladder",
                "mode",
                mode,
            ])
            .stdin(Stdio::null());
        return match run_with_timeout(&mut command, None, Duration::from_secs(120)) {
            Some(output) => Ok(output.status.code().unwrap_or(1).clamp(0, 255)),
            None => Err("cannot reach the primary host".to_string()),
        };
    }
    set_override(request.env, mode)?;
    collect(request)?;
    Ok(0)
}

pub fn route_line(repo: &str, mode: &str, cloud: &Value) -> (String, String) {
    let codex = cloud.get("codex").cloned().unwrap_or(Value::Null);
    let claude = cloud.get("claude").cloned().unwrap_or(Value::Null);
    if mode == "codex" {
        if let Some(env_id) = codex.get(repo).and_then(Value::as_str) {
            return (
                "codex-cloud".to_string(),
                format!("codex cloud exec --env {env_id}"),
            );
        }
    }
    if mode == "claude" {
        if claude.get(repo).and_then(Value::as_str).is_some() {
            return ("claude-cloud".to_string(), "claude-cloud-task".to_string());
        }
        return (
            "local".to_string(),
            "Agent tool in this session".to_string(),
        );
    }
    ("local".to_string(), usage::local_codex_command().to_string())
}

pub fn routes(env: &usage::Env, repo: &str) -> (String, String, String) {
    let status: Value = load_json_or_default(&env.state_file("status.json")).unwrap_or(Value::Null);
    let mode = status
        .get("mode")
        .and_then(Value::as_str)
        .unwrap_or("codex")
        .to_string();
    let cloud = status.get("cloud").cloned().unwrap_or(Value::Null);
    let (destination, command) = route_line(repo, &mode, &cloud);
    (destination, command, mode)
}

pub fn grok_set(env: &usage::Env, pct: f64, reset: Option<&str>, now: f64) -> Result<String, String> {
    if !pct.is_finite() || !(0.0..=100.0).contains(&pct) {
        return Err("grok-set: --pct must be between 0 and 100".to_string());
    }
    let reset_epoch = match reset {
        Some(text) => {
            let epoch = parse_stamp(text)
                .ok_or_else(|| "grok-set: --resets-at must be a future ISO timestamp with a timezone".to_string())?;
            if (epoch as f64) <= now {
                return Err(
                    "grok-set: --resets-at must be a future ISO timestamp with a timezone"
                        .to_string(),
                );
            }
            epoch as f64
        }
        None => now + 7.0 * 86400.0,
    };
    let payload = json!({
        "pct": pct,
        "resets_at": iso_utc(reset_epoch),
        "updated_at": DateTime::<Utc>::from_timestamp(now as i64, 0)
            .map(|stamp| stamp.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string())
            .unwrap_or_else(|| iso_utc(now)),
    });
    write_state(&env.state_file("grok.json"), &payload)?;
    Ok(format!(
        "Grok: {pct}% used, reset {}; report saved",
        iso_utc(reset_epoch)
    ))
}

