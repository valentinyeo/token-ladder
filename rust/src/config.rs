use crate::domain::{Role, Rung};
use chrono::{DateTime, NaiveDateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

pub const DEFAULT_PACE_WARN_RATIO: f64 = 1.1;
pub const DEFAULT_CREDIT_FLOOR_USD: f64 = 2.0;
pub const DEFAULT_POOL_WARN_PCT: f64 = 80.0;
pub const DEFAULT_POOL_OFF_PCT: f64 = 95.0;
pub const DEFAULT_LIMITED_HOURS: f64 = 1.0;
pub const DEFAULT_CREDIT_LIMITED_HOURS: f64 = 12.0;
pub const DEFAULT_LOOP_BASE_SECONDS: u64 = 7200;
pub const DEFAULT_LOOP_AHEAD_SECONDS: u64 = 10800;
pub const DEFAULT_LOOP_FAR_AHEAD_SECONDS: u64 = 14400;
pub const DEFAULT_LOOP_AHEAD_RATIO: f64 = 1.0;
pub const DEFAULT_LOOP_FAR_AHEAD_RATIO: f64 = 1.2;
pub const DEFAULT_LOOP_USED_PCT: f64 = 95.0;
pub const DEFAULT_LOOP_MIN_SECONDS: u64 = 1800;
pub const DEFAULT_CREDIT_STALE_HOURS: f64 = 6.0;
pub const DEFAULT_CREDIT_REFRESH_MINUTES: f64 = 30.0;
pub const COMMANDCODE_PLAN_CREDITS: f64 = 70.0;
pub const COMMANDCODE_CREDITS_TO_USD: f64 = 100.0 / COMMANDCODE_PLAN_CREDITS;
pub const STATE_DIR: &str = ".local/state/token-ladder";

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("cannot read {path}: {source}")]
    Read {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("cannot parse {path}: {source}")]
    Parse {
        path: String,
        #[source]
        source: serde_json::Error,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StampishKind {
    Text,
    Epoch,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(untagged)]
pub enum Stampish {
    Text(String),
    Epoch(f64),
}

pub const MILLISECOND_THRESHOLD: f64 = 100_000_000_000.0;

impl Stampish {
    pub fn epoch(&self) -> Option<i64> {
        match self {
            Stampish::Epoch(value) => {
                if !value.is_finite() {
                    return None;
                }
                let seconds = if value.abs() >= MILLISECOND_THRESHOLD {
                    value / 1000.0
                } else {
                    *value
                };
                Some(seconds as i64)
            }
            Stampish::Text(text) => parse_stamp(text),
        }
    }

    pub fn text(&self) -> String {
        match self {
            Stampish::Text(text) => text.clone(),
            Stampish::Epoch(value) => format!("{value}"),
        }
    }
}

pub use crate::domain::trim_float;

pub fn parse_stamp(text: &str) -> Option<i64> {
    let trimmed = text.trim();
    if let Ok(stamp) = DateTime::parse_from_rfc3339(trimmed) {
        return Some(stamp.timestamp());
    }
    for format in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"] {
        if let Ok(stamp) = NaiveDateTime::parse_from_str(trimmed, format) {
            return Some(stamp.and_utc().timestamp());
        }
    }
    None
}

pub fn iso_stamp(epoch: i64) -> String {
    match DateTime::<Utc>::from_timestamp(epoch, 0) {
        Some(stamp) => stamp.format("%Y-%m-%dT%H:%M:%SZ").to_string(),
        None => format!("epoch {epoch}"),
    }
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct LaneSpec {
    pub lane: String,
    #[serde(default)]
    pub vendor: Option<String>,
    #[serde(default)]
    pub pool: Option<String>,
    #[serde(default)]
    pub credit: Option<String>,
    #[serde(default)]
    pub command: String,
    #[serde(default)]
    pub chat_only: bool,
    #[serde(default)]
    pub effort: Option<String>,
    #[serde(default)]
    pub first_until: Option<String>,
    #[serde(default)]
    pub shared_with_chats: bool,
}

pub fn role_for(lane: &str, chat_only: bool) -> Role {
    if chat_only {
        return Role::Orchestrator;
    }
    match lane.to_ascii_lowercase().as_str() {
        "claude" | "antigravity" => Role::Orchestrator,
        "codex" | "cursor" => Role::Heavy,
        "opencode" | "commandcode" | "vercel" | "vercel-zai" | "openrouter" => Role::Workhorse,
        _ => Role::Workhorse,
    }
}

impl LaneSpec {
    pub fn role(&self) -> Role {
        role_for(&self.lane, self.chat_only)
    }

    pub fn names(&self) -> Vec<String> {
        [Some(&self.lane), self.vendor.as_ref(), self.credit.as_ref()]
            .into_iter()
            .flatten()
            .map(|name| name.to_ascii_lowercase())
            .collect()
    }

    pub fn first_until_epoch(&self) -> Option<i64> {
        self.first_until.as_deref().and_then(parse_stamp)
    }

    pub fn usable(&self) -> bool {
        !self.lane.is_empty() && !self.command.is_empty()
    }
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct RungLane {
    pub lane: String,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub effort: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct RungTable {
    #[serde(default)]
    pub light: Vec<RungLane>,
    #[serde(default)]
    pub medium: Vec<RungLane>,
    #[serde(default)]
    pub hard: Vec<RungLane>,
}

impl RungTable {
    pub fn lanes(&self, rung: Rung) -> &[RungLane] {
        match rung {
            Rung::Light => &self.light,
            Rung::Medium => &self.medium,
            Rung::Hard => &self.hard,
        }
    }

    pub fn configured(&self) -> bool {
        !(self.light.is_empty() && self.medium.is_empty() && self.hard.is_empty())
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct LoopRule {
    #[serde(default = "default_base_seconds")]
    pub base_seconds: u64,
    #[serde(default = "default_ahead_seconds")]
    pub ahead_seconds: u64,
    #[serde(default = "default_far_ahead_seconds")]
    pub far_ahead_seconds: u64,
    #[serde(default = "default_ahead_ratio")]
    pub ahead_ratio: f64,
    #[serde(default = "default_far_ahead_ratio")]
    pub far_ahead_ratio: f64,
    #[serde(default = "default_loop_used_pct")]
    pub used_pct: f64,
    #[serde(default = "default_loop_min_seconds")]
    pub min_seconds: u64,
}

fn default_base_seconds() -> u64 {
    DEFAULT_LOOP_BASE_SECONDS
}

fn default_ahead_seconds() -> u64 {
    DEFAULT_LOOP_AHEAD_SECONDS
}

fn default_far_ahead_seconds() -> u64 {
    DEFAULT_LOOP_FAR_AHEAD_SECONDS
}

fn default_ahead_ratio() -> f64 {
    DEFAULT_LOOP_AHEAD_RATIO
}

fn default_far_ahead_ratio() -> f64 {
    DEFAULT_LOOP_FAR_AHEAD_RATIO
}

fn default_loop_used_pct() -> f64 {
    DEFAULT_LOOP_USED_PCT
}

fn default_loop_min_seconds() -> u64 {
    DEFAULT_LOOP_MIN_SECONDS
}

impl Default for LoopRule {
    fn default() -> Self {
        LoopRule {
            base_seconds: DEFAULT_LOOP_BASE_SECONDS,
            ahead_seconds: DEFAULT_LOOP_AHEAD_SECONDS,
            far_ahead_seconds: DEFAULT_LOOP_FAR_AHEAD_SECONDS,
            ahead_ratio: DEFAULT_LOOP_AHEAD_RATIO,
            far_ahead_ratio: DEFAULT_LOOP_FAR_AHEAD_RATIO,
            used_pct: DEFAULT_LOOP_USED_PCT,
            min_seconds: DEFAULT_LOOP_MIN_SECONDS,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct PlanSpec {
    pub name: String,
    #[serde(default)]
    pub vendor: Option<String>,
    #[serde(default)]
    pub pool: Option<String>,
    #[serde(default)]
    pub credit: Option<String>,
    #[serde(default)]
    pub chat_only: bool,
    #[serde(default)]
    pub usd_per_cycle: Option<f64>,
    #[serde(default)]
    pub monthly_price_usd: Option<f64>,
    #[serde(default)]
    pub included_allowance_usd: Option<f64>,
    #[serde(default)]
    pub included_models: Vec<String>,
    #[serde(default)]
    pub runs_on: Vec<String>,
    #[serde(default)]
    pub extra_cost: Option<String>,
    #[serde(default)]
    pub source: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct CodexCredits {
    #[serde(default)]
    pub approved: Option<String>,
    #[serde(default)]
    pub floor: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
pub struct Config {
    #[serde(default)]
    pub lanes: Vec<LaneSpec>,
    #[serde(default)]
    pub rungs: RungTable,
    #[serde(default)]
    pub plans: Vec<PlanSpec>,
    #[serde(default = "default_pace_warn_ratio")]
    pub pace_warn_ratio: f64,
    #[serde(default = "default_credit_floor_usd")]
    pub credit_floor_usd: f64,
    #[serde(default = "default_pool_warn_pct")]
    pub pool_warn_pct: f64,
    #[serde(default = "default_pool_off_pct")]
    pub pool_off_pct: f64,
    #[serde(default = "default_limited_hours")]
    pub limited_hours: f64,
    #[serde(default = "default_credit_limited_hours")]
    pub credit_limited_hours: f64,
    #[serde(default)]
    pub loop_interval: LoopRule,
    #[serde(default)]
    pub codex_credits: CodexCredits,
    #[serde(default)]
    pub balance_lanes: bool,
    #[serde(default = "default_credit_stale_hours")]
    pub credit_stale_hours: f64,
    #[serde(default = "default_credit_refresh_minutes")]
    pub credit_refresh_minutes: f64,
    #[serde(default)]
    pub opencode_reader_command: Option<Vec<String>>,
    #[serde(default)]
    pub measured_burn: Option<serde_json::Value>,
}

fn default_credit_stale_hours() -> f64 {
    DEFAULT_CREDIT_STALE_HOURS
}

fn default_credit_refresh_minutes() -> f64 {
    DEFAULT_CREDIT_REFRESH_MINUTES
}

fn default_pace_warn_ratio() -> f64 {
    DEFAULT_PACE_WARN_RATIO
}

fn default_credit_floor_usd() -> f64 {
    DEFAULT_CREDIT_FLOOR_USD
}

fn default_pool_warn_pct() -> f64 {
    DEFAULT_POOL_WARN_PCT
}

fn default_pool_off_pct() -> f64 {
    DEFAULT_POOL_OFF_PCT
}

fn default_limited_hours() -> f64 {
    DEFAULT_LIMITED_HOURS
}

fn default_credit_limited_hours() -> f64 {
    DEFAULT_CREDIT_LIMITED_HOURS
}

impl Default for Config {
    fn default() -> Self {
        Config {
            lanes: Vec::new(),
            rungs: RungTable::default(),
            plans: Vec::new(),
            pace_warn_ratio: DEFAULT_PACE_WARN_RATIO,
            credit_floor_usd: DEFAULT_CREDIT_FLOOR_USD,
            pool_warn_pct: DEFAULT_POOL_WARN_PCT,
            pool_off_pct: DEFAULT_POOL_OFF_PCT,
            limited_hours: DEFAULT_LIMITED_HOURS,
            credit_limited_hours: DEFAULT_CREDIT_LIMITED_HOURS,
            loop_interval: LoopRule::default(),
            codex_credits: CodexCredits::default(),
            balance_lanes: false,
            credit_stale_hours: DEFAULT_CREDIT_STALE_HOURS,
            credit_refresh_minutes: DEFAULT_CREDIT_REFRESH_MINUTES,
            opencode_reader_command: None,
            measured_burn: None,
        }
    }
}

impl Config {
    pub fn parse(text: &str, path: &str) -> Result<Self, ConfigError> {
        serde_json::from_str(text).map_err(|source| ConfigError::Parse {
            path: path.to_string(),
            source,
        })
    }

    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Read {
            path: path.display().to_string(),
            source,
        })?;
        Self::parse(&text, &path.display().to_string())
    }

    pub fn load_with_local(path: &Path, local: Option<&Path>) -> Result<Self, ConfigError> {
        let mut value = read_json(path)?;
        if let Some(local) = local.filter(|local| local.is_file()) {
            let overlay = read_json(local)?;
            merge_objects(&mut value, overlay);
        }
        serde_json::from_value(value).map_err(|source| ConfigError::Parse {
            path: path.display().to_string(),
            source,
        })
    }

    pub fn usable_lanes(&self) -> Vec<LaneSpec> {
        self.lanes
            .iter()
            .filter(|lane| lane.usable())
            .cloned()
            .collect()
    }

    pub fn lane(&self, name: &str) -> Option<LaneSpec> {
        let wanted = name.to_ascii_lowercase();
        self.lanes
            .iter()
            .find(|lane| lane.lane.to_ascii_lowercase() == wanted)
            .cloned()
    }

    pub fn plan(&self, vendor: &str) -> Option<&PlanSpec> {
        self.plans.iter().find(|plan| {
            plan.vendor
                .as_deref()
                .map(|value| value.eq_ignore_ascii_case(vendor))
                .unwrap_or(false)
        })
    }
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct Pool {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub state: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub used_pct: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pace_ratio: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resets_at: Option<Stampish>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hours_left: Option<f64>,
    #[serde(default)]
    pub chat_only: bool,
    #[serde(default)]
    pub shared_with_chats: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub credits_usd: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub credit_floor_usd: Option<f64>,
    #[serde(default)]
    pub credit_unknown: bool,
    #[serde(default)]
    pub estimated: bool,
    #[serde(default)]
    pub used_up: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub window_minutes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub five_hour_used_pct: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gemini: Option<PoolGroup>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub claude_gpt: Option<PoolGroup>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub used: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub limit: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub elapsed_pct: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub credits: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub five_hour_resets_at: Option<Stampish>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub used_up_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub used_up_window: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub used_up_resets_at: Option<Stampish>,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct PoolGroup {
    #[serde(default)]
    pub weekly_remaining_pct: Option<f64>,
    #[serde(default)]
    pub five_hour_remaining_pct: Option<f64>,
    #[serde(default)]
    pub weekly_resets_at: Option<Stampish>,
    #[serde(default)]
    pub five_hour_resets_at: Option<Stampish>,
}

impl Pool {
    pub fn is_off(&self) -> bool {
        let state = self.state.as_deref().unwrap_or("").to_ascii_uppercase();
        self.used_up || state == "OFF" || self.used_pct.is_some_and(|used| used >= 100.0)
    }

    pub fn state_label(&self) -> String {
        match self.state.as_deref() {
            Some(state) if !state.is_empty() => state.to_ascii_uppercase(),
            _ => "unknown".to_string(),
        }
    }

    pub fn off_note(&self) -> String {
        match (&self.note, self.used_pct) {
            (Some(note), _) if !note.is_empty() => note.clone(),
            (_, Some(used)) => format!("{}% used", trim_float(used)),
            _ => String::new(),
        }
    }

    pub fn reset_epoch(&self) -> Option<i64> {
        self.resets_at.as_ref().and_then(Stampish::epoch)
    }
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct CreditWindows {
    #[serde(default)]
    pub weekly: Option<CreditWindow>,
    #[serde(default, rename = "fiveHour")]
    pub five_hour: Option<CreditWindow>,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct CreditWindow {
    #[serde(default)]
    pub used: Option<f64>,
    #[serde(default)]
    pub cap: Option<f64>,
    #[serde(default)]
    pub exceeded: Option<bool>,
    #[serde(default, rename = "resetAt")]
    pub reset_at: Option<Stampish>,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct CreditEntry {
    #[serde(default)]
    pub usd: Option<f64>,
    #[serde(default)]
    pub credits: Option<f64>,
    #[serde(default)]
    pub resets_at: Option<Stampish>,
    #[serde(default)]
    pub at: Option<f64>,
    #[serde(default)]
    pub limit_usd: Option<f64>,
    #[serde(default)]
    pub windows: Option<CreditWindows>,
    #[serde(default)]
    pub period_end: Option<Stampish>,
}

impl CreditEntry {
    pub fn remaining_usd(&self) -> Option<f64> {
        self.usd
            .or_else(|| self.credits.map(|credits| credits * COMMANDCODE_CREDITS_TO_USD))
    }

    pub fn reset_epoch(&self) -> Option<i64> {
        self.resets_at
            .as_ref()
            .and_then(Stampish::epoch)
            .or_else(|| {
                self.windows
                    .as_ref()
                    .and_then(|windows| windows.weekly.as_ref())
                    .and_then(|weekly| weekly.reset_at.as_ref())
                    .and_then(Stampish::epoch)
            })
            .or_else(|| self.period_end.as_ref().and_then(Stampish::epoch))
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(untagged)]
pub enum LimitEntry {
    Epoch(f64),
    Stamp(String),
    Detail(LimitDetail),
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct LimitDetail {
    #[serde(default)]
    pub until: Option<Stampish>,
    #[serde(default)]
    pub reason: Option<String>,
}

impl LimitEntry {
    pub fn epoch(&self) -> Option<i64> {
        match self {
            LimitEntry::Epoch(value) => Stampish::Epoch(*value).epoch(),
            LimitEntry::Stamp(text) => parse_stamp(text),
            LimitEntry::Detail(detail) => detail.until.as_ref().and_then(Stampish::epoch),
        }
    }

    pub fn reason(&self) -> Option<String> {
        match self {
            LimitEntry::Detail(detail) => detail
                .reason
                .as_ref()
                .map(|reason| reason.trim().to_string())
                .filter(|reason| !reason.is_empty()),
            _ => None,
        }
    }
}

pub type Limits = HashMap<String, LimitEntry>;
pub type Credits = HashMap<String, CreditEntry>;
pub type Pools = HashMap<String, Pool>;

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct WeekRead {
    #[serde(default)]
    pub used: Option<f64>,
    #[serde(default)]
    pub resets_at: Option<Stampish>,
    #[serde(default)]
    pub hours_left: Option<f64>,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct AccountRead {
    #[serde(default)]
    pub week: Option<WeekRead>,
    #[serde(default)]
    pub five_hour: Option<WeekRead>,
    #[serde(default)]
    pub email: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize, Serialize)]
pub struct Status {
    #[serde(default)]
    pub ts: Option<f64>,
    #[serde(default)]
    pub mode: Option<String>,
    #[serde(default)]
    pub why: Option<String>,
    #[serde(default)]
    pub pools: Pools,
    #[serde(default)]
    pub claude: Option<AccountRead>,
    #[serde(default)]
    pub codex: Option<AccountRead>,
    #[serde(default)]
    pub config: Option<Config>,
}

impl Status {
    pub fn claude_pool(&self) -> Option<&Pool> {
        self.pool("claude")
    }

    pub fn account(&self, name: &str) -> Option<&AccountRead> {
        match name {
            "claude" => self.claude.as_ref(),
            "codex" => self.codex.as_ref(),
            _ => None,
        }
    }

    pub fn parse(text: &str, path: &str) -> Result<Self, ConfigError> {
        serde_json::from_str(text).map_err(|source| ConfigError::Parse {
            path: path.to_string(),
            source,
        })
    }

    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Read {
            path: path.display().to_string(),
            source,
        })?;
        Self::parse(&text, &path.display().to_string())
    }

    pub fn pool(&self, name: &str) -> Option<&Pool> {
        self.pools.get(name)
    }

    pub fn age_seconds(&self, now: i64) -> Option<f64> {
        self.ts.map(|ts| (now as f64 - ts).max(0.0))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StatePaths {
    pub root: PathBuf,
}

impl Default for StatePaths {
    fn default() -> Self {
        StatePaths {
            root: home_dir().join(STATE_DIR),
        }
    }
}

pub fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/nonexistent"))
}

impl StatePaths {
    pub fn under(root: impl Into<PathBuf>) -> Self {
        StatePaths { root: root.into() }
    }

    pub fn status(&self) -> PathBuf {
        self.root.join("status.json")
    }

    pub fn credits(&self) -> PathBuf {
        self.root.join("lane-credits.json")
    }

    pub fn limits(&self) -> PathBuf {
        let live = self.root.join("limits.json");
        let legacy = self.root.join("lane-limits.json");
        if live.is_file() || !legacy.is_file() {
            live
        } else {
            legacy
        }
    }

    pub fn launches(&self) -> PathBuf {
        match std::env::var_os("PLAN_PICK_LAUNCHES") {
            Some(path) if !path.is_empty() => PathBuf::from(path),
            _ => self.root.join("launches.jsonl"),
        }
    }

    pub fn rung_attempts(&self) -> PathBuf {
        self.root.join("rung-attempts.json")
    }
}

pub fn load_json_or_default<T: for<'de> Deserialize<'de> + Default>(
    path: &Path,
) -> Result<T, ConfigError> {
    if !path.exists() {
        return Ok(T::default());
    }
    let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Read {
        path: path.display().to_string(),
        source,
    })?;
    if text.trim().is_empty() {
        return Ok(T::default());
    }
    serde_json::from_str(&text).map_err(|source| ConfigError::Parse {
        path: path.display().to_string(),
        source,
    })
}

#[derive(Debug, Clone, Default)]
pub struct State {
    pub status: Status,
    pub credits: Credits,
    pub limits: Limits,
}

impl State {
    pub fn load(paths: &StatePaths) -> Self {
        State {
            status: load_json_or_default(&paths.status()).unwrap_or_default(),
            credits: load_json_or_default(&paths.credits()).unwrap_or_default(),
            limits: load_json_or_default(&paths.limits()).unwrap_or_default(),
        }
    }

    pub fn pools(&self) -> &Pools {
        &self.status.pools
    }

    pub fn claude_pool(&self) -> Option<&Pool> {
        self.status.pool("claude")
    }
}

fn read_json(path: &Path) -> Result<serde_json::Value, ConfigError> {
    let text = std::fs::read_to_string(path).map_err(|source| ConfigError::Read {
        path: path.display().to_string(),
        source,
    })?;
    serde_json::from_str(&text).map_err(|source| ConfigError::Parse {
        path: path.display().to_string(),
        source,
    })
}

pub fn merge_objects(base: &mut serde_json::Value, overlay: serde_json::Value) {
    if let (Some(base), Some(overlay)) = (base.as_object_mut(), overlay.as_object()) {
        for (key, value) in overlay {
            base.insert(key.clone(), value.clone());
        }
    }
}

pub fn config_path() -> PathBuf {
    match std::env::var_os("TOKEN_LADDER_CONFIG") {
        Some(value) => PathBuf::from(value),
        None => PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../config.json"),
    }
}

pub fn local_config_path() -> Option<PathBuf> {
    match std::env::var_os("TOKEN_LADDER_LOCAL_CONFIG") {
        Some(value) => Some(PathBuf::from(value)),
        None => Some(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../config.local.json")),
    }
}

pub fn lane_pool<'a>(lane: &LaneSpec, pools: &'a Pools) -> Option<&'a Pool> {
    let pool = lane.pool.as_deref().and_then(|name| pools.get(name));
    pool.or_else(|| {
        lane.credit
            .as_deref()
            .and_then(|name| pools.get(&format!("credit:{name}")))
    })
}

pub fn lane_credit<'a>(lane: &LaneSpec, credits: &'a Credits) -> Option<&'a CreditEntry> {
    lane.credit.as_deref().and_then(|name| credits.get(name))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn repo_config() -> Config {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../config.json");
        Config::load(&path).expect("repo config.json parses")
    }

    #[test]
    fn repo_config_parses_into_typed_lanes() {
        let config = repo_config();
        assert!(config.lanes.len() >= 8);
        let names: Vec<&str> = config.lanes.iter().map(|lane| lane.lane.as_str()).collect();
        for wanted in [
            "opencode",
            "antigravity",
            "vercel",
            "vercel-zai",
            "commandcode",
            "openrouter",
            "codex",
            "cursor",
        ] {
            assert!(names.contains(&wanted), "missing lane {wanted}");
        }
        assert_eq!(config.pace_warn_ratio, 1.1);
        assert_eq!(config.credit_floor_usd, 2.0);
        assert_eq!(config.pool_warn_pct, 80.0);
    }

    #[test]
    fn repo_config_assigns_each_lane_its_role() {
        let config = repo_config();
        let role = |name: &str| config.lane(name).unwrap().role();
        assert_eq!(role("antigravity"), Role::Orchestrator);
        assert_eq!(role("opencode"), Role::Workhorse);
        assert_eq!(role("commandcode"), Role::Workhorse);
        assert_eq!(role("vercel"), Role::Workhorse);
        assert_eq!(role("vercel-zai"), Role::Workhorse);
        assert_eq!(role("openrouter"), Role::Workhorse);
        assert_eq!(role("codex"), Role::Heavy);
        assert_eq!(role("cursor"), Role::Heavy);
    }

    #[test]
    fn chat_only_lane_is_an_orchestrator_whatever_its_name() {
        assert_eq!(role_for("antigravity", true), Role::Orchestrator);
        assert_eq!(role_for("some-chat", true), Role::Orchestrator);
        assert_eq!(role_for("claude", false), Role::Orchestrator);
    }

    #[test]
    fn repo_config_carries_rungs_and_plans() {
        let config = repo_config();
        assert!(config.rungs.configured());
        assert_eq!(config.rungs.lanes(Rung::Hard)[0].lane, "codex");
        assert!(config.rungs.lanes(Rung::Medium).len() >= 5);
        assert!(config.plan("anthropic").is_some());
        assert_eq!(config.loop_interval.base_seconds, 7200);
        assert_eq!(config.codex_credits.floor, Some(10000.0));
    }

    #[test]
    fn missing_state_files_load_as_empty_state() {
        let dir = std::env::temp_dir().join(format!("token-ladder-rust-{}", std::process::id()));
        let paths = StatePaths::under(&dir);
        let state = State::load(&paths);
        assert!(state.status.pools.is_empty());
        assert!(state.credits.is_empty());
        assert!(state.limits.is_empty());
    }

    #[test]
    fn stale_state_file_content_does_not_break_loading() {
        let dir = std::env::temp_dir().join(format!("token-ladder-rust-bad-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("status.json"), "{not json").unwrap();
        let state = State::load(&StatePaths::under(&dir));
        assert!(state.status.pools.is_empty());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn status_pools_and_limits_parse_into_typed_structs() {
        let status: Status = serde_json::from_str(
            r#"{"ts": 1800000000.5, "pools": {"claude": {"state": "OFF", "used_pct": 100.0, "pace_ratio": 1.44},
                "google": {"state": "ON", "chat_only": true, "used_pct": 49.0,
                           "gemini": {"weekly_remaining_pct": 51, "five_hour_remaining_pct": 97}}}}"#,
        )
        .unwrap();
        assert_eq!(status.pools["claude"].state_label(), "OFF");
        assert!(status.pools["claude"].is_off());
        assert!(status.pools["google"].chat_only);
        assert_eq!(
            status.pools["google"]
                .gemini
                .as_ref()
                .unwrap()
                .weekly_remaining_pct,
            Some(51.0)
        );
        let credits: Credits = serde_json::from_str(
            r#"{"vercel": {"usd": 14.24, "resets_at": "2026-12-05T08:00:00.000Z", "limit_usd": 110},
                "commandcode": {"credits": 60.7084, "at": 1795783606.4}}"#,
        )
        .unwrap();
        assert_eq!(credits["vercel"].remaining_usd(), Some(14.24));
        assert_eq!(credits["vercel"].reset_epoch(), Some(1796457600));
        assert!(credits["commandcode"].remaining_usd().unwrap() > 86.0);
        let limits: Limits = serde_json::from_str(
            r#"{"deepseek": 1826919819.4, "opencode": {"until": 1796079670.0, "reason": "credit floor reached"}}"#,
        )
        .unwrap();
        assert_eq!(limits["deepseek"].epoch(), Some(1826919819));
        assert_eq!(limits["opencode"].epoch(), Some(1796079670));
        assert_eq!(
            limits["opencode"].reason(),
            Some("credit floor reached".to_string())
        );
    }

    #[test]
    fn millisecond_reset_stamps_become_seconds() {
        let entry = CreditEntry {
            credits: Some(60.708),
            windows: Some(CreditWindows {
                weekly: Some(CreditWindow {
                    used: Some(9.29),
                    cap: Some(35.0),
                    exceeded: Some(false),
                    reset_at: Some(Stampish::Epoch(1795990113512.0)),
                }),
                five_hour: None,
            }),
            ..CreditEntry::default()
        };
        assert_eq!(entry.reset_epoch(), Some(1795990113));
        assert_eq!(
            Stampish::Epoch(1795988288.0).epoch(),
            Some(1795988288)
        );
    }

    #[test]
    fn credit_unknown_or_off_pool_is_detected() {
        let pool = Pool {
            credit_unknown: true,
            ..Pool::default()
        };
        assert!(!pool.is_off());
        assert_eq!(pool.state_label(), "unknown");
        let off = Pool {
            state: Some("OFF".to_string()),
            note: Some("$0.68 credit left, under $2".to_string()),
            ..Pool::default()
        };
        assert!(off.is_off());
        assert_eq!(off.off_note(), "$0.68 credit left, under $2");
    }

    #[test]
    fn lane_pool_and_credit_lookup_by_pool_and_credit_name() {
        let mut pools = Pools::new();
        pools.insert(
            "codex".to_string(),
            Pool {
                state: Some("ON".to_string()),
                ..Pool::default()
            },
        );
        pools.insert(
            "credit:vercel".to_string(),
            Pool {
                state: Some("ON".to_string()),
                ..Pool::default()
            },
        );
        let mut credits = Credits::new();
        credits.insert(
            "vercel".to_string(),
            CreditEntry {
                usd: Some(14.24),
                ..CreditEntry::default()
            },
        );
        let codex = LaneSpec {
            lane: "codex".to_string(),
            pool: Some("codex".to_string()),
            command: "run codex".to_string(),
            ..LaneSpec::default()
        };
        let vercel = LaneSpec {
            lane: "vercel".to_string(),
            credit: Some("vercel".to_string()),
            command: "run vercel".to_string(),
            ..LaneSpec::default()
        };
        assert_eq!(lane_pool(&codex, &pools).unwrap().state_label(), "ON");
        assert_eq!(lane_pool(&vercel, &pools).unwrap().state_label(), "ON");
        assert_eq!(lane_credit(&vercel, &credits).unwrap().remaining_usd(), Some(14.24));
    }

    #[test]
    fn rung_table_reads_each_rung() {
        let table = RungTable {
            light: vec![RungLane {
                lane: "opencode".to_string(),
                ..RungLane::default()
            }],
            medium: vec![],
            hard: vec![RungLane {
                lane: "codex".to_string(),
                ..RungLane::default()
            }],
        };
        assert_eq!(table.lanes(Rung::Light)[0].lane, "opencode");
        assert!(table.lanes(Rung::Medium).is_empty());
        assert_eq!(table.lanes(Rung::Hard)[0].lane, "codex");
    }

    #[test]
    fn timestamps_parse_in_both_repo_shapes() {
        assert_eq!(parse_stamp("2026-12-05T08:00:00.000Z"), Some(1796457600));
        assert_eq!(parse_stamp("2026-11-24T23:59:00Z"), Some(1795564740));
        assert_eq!(parse_stamp("not a date"), None);
        assert_eq!(iso_stamp(1796457600), "2026-12-05T08:00:00Z");
    }
}
