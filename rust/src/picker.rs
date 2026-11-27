use crate::config::{
    lane_credit, lane_pool, Config, CreditEntry, Credits, LaneSpec, Limits, Pool, Pools, Status,
};
use crate::domain::{LaneEligibility, PickResult, Role, Rung, RungContext, SkipReason};
use std::cmp::Ordering;

pub const OPENROUTER_LANE: &str = "openrouter";
pub const HEAVY_FALLBACK_PACE: f64 = 0.80;
pub const SECONDS_PER_DAY: f64 = 86400.0;

#[derive(Debug, Clone)]
pub struct LaneContext<'a> {
    pub cfg: &'a Config,
    pub pools: &'a Pools,
    pub credits: &'a Credits,
    pub limits: &'a Limits,
    pub now: i64,
    pub status: Option<&'a Status>,
}

#[derive(Debug, Clone)]
pub struct PickContext<'a> {
    pub lane: LaneContext<'a>,
    pub excluded: Vec<String>,
}

impl<'a> PickContext<'a> {
    pub fn new(
        cfg: &'a Config,
        pools: &'a Pools,
        credits: &'a Credits,
        limits: &'a Limits,
        now: i64,
    ) -> Self {
        PickContext {
            lane: LaneContext {
                cfg,
                pools,
                credits,
                limits,
                now,
                status: None,
            },
            excluded: Vec::new(),
        }
    }

    pub fn with_status(mut self, status: &'a Status) -> Self {
        self.lane.status = Some(status);
        self
    }

    pub fn excluding(mut self, excluded: Vec<String>) -> Self {
        self.excluded = excluded;
        self
    }
}

pub fn vendor_exclusions(values: &[String]) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for value in values {
        for part in value.split(',') {
            let name = part.trim().to_ascii_lowercase();
            if !name.is_empty() && !names.contains(&name) {
                names.push(name);
            }
        }
    }
    names
}

pub fn is_orchestrator(lane: &LaneSpec, pools: &Pools) -> bool {
    lane.role() == Role::Orchestrator || lane_pool(lane, pools).is_some_and(|pool| pool.chat_only)
}

fn burn_key(lane: &LaneSpec) -> Option<&'static str> {
    let lane_name = lane.lane.to_ascii_lowercase();
    let vendor = lane
        .vendor
        .as_deref()
        .map(|vendor| vendor.to_ascii_lowercase());
    if lane_name == "codex" || vendor.as_deref() == Some("openai") {
        return Some("codex");
    }
    if lane_name == "claude" || vendor.as_deref() == Some("anthropic") {
        return Some("claude");
    }
    None
}

pub fn bonus_reset_hours(lane: &LaneSpec, ctx: &LaneContext<'_>) -> Option<f64> {
    let key = burn_key(lane)?;
    if let Some(status) = ctx.status {
        if let Some(entry) = status.burn.as_ref().and_then(|burn| burn.entry(key)) {
            if entry.burn_now {
                return Some(entry.reset_hours.unwrap_or(0.0));
            }
            if entry.reset_hours.is_some() {
                return None;
            }
        }
    }
    ctx.cfg.burn_now(key, ctx.now)
}

fn lane_used_pct(lane: &LaneSpec, ctx: &LaneContext<'_>) -> Option<f64> {
    if let Some(pool) = lane_pool(lane, ctx.pools) {
        if let Some(used) = pool.used_pct {
            return Some(used);
        }
    }
    let key = burn_key(lane)?;
    ctx.status
        .and_then(|status| status.account(key))
        .and_then(|read| read.week.as_ref())
        .and_then(|week| week.used)
}

pub fn burn_lane(
    lane: &LaneSpec,
    ctx: &LaneContext<'_>,
    excluded: &[String],
) -> Option<f64> {
    if !lane.role().runs_workers() || is_orchestrator(lane, ctx.pools) {
        return None;
    }
    if lane
        .names()
        .iter()
        .any(|name| excluded.iter().any(|skip| skip == name))
    {
        return None;
    }
    if limit_skip(ctx.limits.get(&lane.lane), ctx.now).is_some() {
        return None;
    }
    if lane_pool(lane, ctx.pools).is_some_and(Pool::is_off) {
        return None;
    }
    let hours = bonus_reset_hours(lane, ctx)?;
    let used = lane_used_pct(lane, ctx)?;
    (used < ctx.cfg.reset_use_at_percent).then_some(hours)
}

pub fn expiring_value_per_day(
    lane: &LaneSpec,
    credits: &Credits,
    now: i64,
) -> Option<(f64, f64, f64)> {
    let entry: &CreditEntry = lane_credit(lane, credits)?;
    let remaining = entry.remaining_usd()?;
    let reset = entry.reset_epoch()?;
    if reset <= now {
        return None;
    }
    let days = (reset - now) as f64 / SECONDS_PER_DAY;
    if days <= 0.0 {
        return None;
    }
    Some((remaining / days, remaining, days))
}

pub fn lane_balance_score(lane: &LaneSpec, pools: &Pools) -> f64 {
    match lane_pool(lane, pools) {
        Some(pool) => pool
            .pace_ratio
            .or_else(|| pool.used_pct.map(|used| used / 100.0))
            .or_else(|| match (pool.credits_usd, pool.credit_floor_usd) {
                (Some(credits), Some(floor)) if floor > 0.0 => {
                    Some(((floor - credits) / floor).clamp(0.0, 1.0))
                }
                _ => None,
            })
            .unwrap_or(1.0),
        None => 1.0,
    }
}

pub fn limit_skip(entry: Option<&crate::config::LimitEntry>, now: i64) -> Option<SkipReason> {
    let entry = entry?;
    let until = entry.epoch()?;
    if until <= now {
        return None;
    }
    let note = entry.reason().unwrap_or_else(|| {
        if until - now > 86400 {
            "manual skip (lane-limits)".to_string()
        } else {
            "limited (lane-limits)".to_string()
        }
    });
    Some(SkipReason::LimitedUntil {
        until_epoch: until,
        note,
    })
}

pub fn pool_skip(pool: Option<&Pool>, cfg: &Config) -> Option<SkipReason> {
    let pool = pool?;
    if pool.credit_unknown {
        return Some(SkipReason::PoolOff {
            note: "credit unknown".to_string(),
        });
    }
    if pool.is_off() {
        return Some(SkipReason::PoolOff {
            note: pool.off_note(),
        });
    }
    let ratio = pool.pace_ratio?;
    if ratio > cfg.pace_warn_ratio {
        return Some(SkipReason::AheadOfPace {
            ratio,
            warn_ratio: cfg.pace_warn_ratio,
        });
    }
    None
}

pub fn credit_floor_skip(lane: &LaneSpec, pool: Option<&Pool>, cfg: &Config) -> Option<SkipReason> {
    lane.credit.as_ref()?;
    let pool = pool?;
    let remaining = pool.credits_usd?;
    let floor = pool.credit_floor_usd.unwrap_or(cfg.credit_floor_usd);
    if remaining <= floor {
        return Some(SkipReason::BelowCreditFloor {
            remaining_usd: remaining,
            floor_usd: floor,
        });
    }
    None
}

pub fn lane_eligibility(
    lane: &LaneSpec,
    ctx: &LaneContext<'_>,
    excluded: &[String],
) -> LaneEligibility {
    if is_orchestrator(lane, ctx.pools) {
        return LaneEligibility::skipped(SkipReason::OrchestratorOnly);
    }
    if lane
        .names()
        .iter()
        .any(|name| excluded.iter().any(|skip| skip == name))
    {
        return LaneEligibility::skipped(SkipReason::VendorExcluded);
    }
    if let Some(reason) = limit_skip(ctx.limits.get(&lane.lane), ctx.now) {
        return LaneEligibility::skipped(reason);
    }
    let pool = lane_pool(lane, ctx.pools);
    if let Some(reason) = pool_skip(pool, ctx.cfg) {
        return LaneEligibility::skipped(reason);
    }
    if let Some(reason) = credit_floor_skip(lane, pool, ctx.cfg) {
        return LaneEligibility::skipped(reason);
    }
    if lane.credit.is_some() && pool.is_none() {
        return LaneEligibility::skipped(SkipReason::PoolOff {
            note: "credit unknown".to_string(),
        });
    }
    match expiring_value_per_day(lane, ctx.credits, ctx.now) {
        Some((per_day, remaining, days)) => LaneEligibility::ready(
            per_day,
            format!("${remaining:.2} left, {days:.1} days to reset = ${per_day:.2}/day"),
        ),
        None => LaneEligibility::ready(
            lane_balance_score(lane, ctx.pools),
            pool_note(pool, ctx.cfg),
        ),
    }
}

pub fn pool_note(pool: Option<&Pool>, cfg: &Config) -> String {
    let Some(pool) = pool else {
        return "no usage reader, a 429 or empty credit marks it limited".to_string();
    };
    if let Some(note) = pool.note.as_ref().filter(|note| !note.is_empty()) {
        return note.clone();
    }
    match pool.pace_ratio {
        Some(ratio) => format!("pace {ratio:.2}x, within {}x", crate::config::trim_float(cfg.pace_warn_ratio)),
        None => "pace unknown, not ahead".to_string(),
    }
}

struct Rated<'a> {
    index: usize,
    lane: &'a LaneSpec,
    eligibility: LaneEligibility,
    value_per_day: Option<f64>,
    pace: Option<f64>,
}

impl Rated<'_> {
    fn ready(&self) -> bool {
        self.eligibility.is_ready()
    }

    fn is_last_resort(&self) -> bool {
        self.lane.lane.eq_ignore_ascii_case(OPENROUTER_LANE)
    }

    fn beats(&self, other: &Rated<'_>) -> Ordering {
        match (self.value_per_day, other.value_per_day) {
            (Some(left), Some(right)) => right
                .partial_cmp(&left)
                .unwrap_or(Ordering::Equal)
                .then_with(|| self.index.cmp(&other.index)),
            (Some(_), None) => Ordering::Less,
            (None, Some(_)) => Ordering::Greater,
            (None, None) => match (self.pace, other.pace) {
                (Some(left), Some(right)) => left
                    .partial_cmp(&right)
                    .unwrap_or(Ordering::Equal)
                    .then_with(|| self.index.cmp(&other.index)),
                _ => self.index.cmp(&other.index),
            },
        }
    }
}

fn rank(mut group: Vec<Rated<'_>>) -> Vec<Rated<'_>> {
    group.sort_by(Rated::beats);
    group
}

fn refused(reason: String, ctx: &RungContext) -> PickResult {
    PickResult::empty(reason, ctx)
}

fn skipped_clause(skipped: &[(String, String)]) -> String {
    if skipped.is_empty() {
        return String::new();
    }
    let parts: Vec<String> = skipped
        .iter()
        .map(|(lane, why)| format!("{lane} ({why})"))
        .collect();
    format!("; skipped {}", parts.join(", "))
}

pub fn pick(ctx: &PickContext<'_>, rung_ctx: &RungContext) -> PickResult {
    let rung = rung_ctx.rung;
    let mut rated: Vec<Rated<'_>> = Vec::new();
    let mut skipped: Vec<(String, String)> = Vec::new();
    let mut burn: Vec<(&LaneSpec, f64)> = Vec::new();
    for (index, lane) in ctx.lane.cfg.lanes.iter().enumerate() {
        if !lane.usable() {
            continue;
        }
        let eligibility = lane_eligibility(lane, &ctx.lane, &ctx.excluded);
        if !eligibility.is_ready() {
            skipped.push((lane.lane.clone(), eligibility.reason()));
        }
        if let Some(hours) = burn_lane(lane, &ctx.lane, &ctx.excluded) {
            burn.push((lane, hours));
        }
        rated.push(Rated {
            index,
            lane,
            eligibility,
            value_per_day: expiring_value_per_day(lane, ctx.lane.credits, ctx.lane.now)
                .map(|(per_day, _, _)| per_day),
            pace: lane_balance_score(lane, ctx.lane.pools).into(),
        });
    }
    let primary: Vec<Rated<'_>> = rank(
        rated
            .iter()
            .filter(|item| item.ready() && item.lane.role() == Role::Workhorse && !item.is_last_resort())
            .map(clone_rated)
            .collect(),
    );
    let last_resort: Vec<Rated<'_>> = rank(
        rated
            .iter()
            .filter(|item| item.ready() && item.lane.role() == Role::Workhorse && item.is_last_resort())
            .map(clone_rated)
            .collect(),
    );
    let heavy: Vec<Rated<'_>> = rank(
        rated
            .iter()
            .filter(|item| item.ready() && item.lane.role() == Role::Heavy)
            .map(clone_rated)
            .collect(),
    );
    let heavy_under_pace = heavy
        .iter()
        .find(|item| item.pace.is_some_and(|pace| pace < HEAVY_FALLBACK_PACE));
    burn.sort_by(|left, right| left.1.partial_cmp(&right.1).unwrap_or(Ordering::Equal));
    let burn_first: Option<(&LaneSpec, f64)> = burn.first().copied();
    let normal: Option<&Rated<'_>> = match rung {
        Rung::Light => primary.first().or_else(|| last_resort.first()),
        Rung::Medium => primary
            .first()
            .or_else(|| last_resort.first())
            .or(heavy_under_pace),
        Rung::Hard => heavy.first(),
    };
    let chosen: Option<(&LaneSpec, Option<&Rated<'_>>)> = match (&burn_first, normal) {
        (Some((lane, _)), _) => Some((lane, None)),
        (None, Some(item)) => Some((item.lane, Some(item))),
        (None, None) => None,
    };
    let clause = skipped_clause(&skipped);
    match chosen {
        Some((lane, item)) => {
            let tier = lane.role();
            let why = match (&burn_first, item) {
                (Some((_, hours)), _) => format!(
                    "burn complimentary reset in {hours:.0}h before expiry"
                ),
                (None, Some(item)) => item.eligibility.reason(),
                (None, None) => String::new(),
            };
            let reason = format!(
                "rung {rung}: {tier} lane {} ({}){}",
                lane.lane, why, clause
            );
            PickResult::lane(lane.lane.clone(), lane.command.clone(), reason, rung_ctx)
        }
        None => {
            let tier = match rung {
                Rung::Light => "workhorse",
                Rung::Medium => "workhorse or heavy",
                Rung::Hard => "heavy",
            };
            refused(
                format!(
                    "no free {tier} lane for rung {rung}; every eligible lane is off, limited or ahead of pace{clause}"
                ),
                rung_ctx,
            )
        }
    }
}

fn clone_rated<'a>(item: &Rated<'a>) -> Rated<'a> {
    Rated {
        index: item.index,
        lane: item.lane,
        eligibility: item.eligibility.clone(),
        value_per_day: item.value_per_day,
        pace: item.pace,
    }
}

pub fn lane_in_rung(cfg: &Config, lane: &LaneSpec, rung: Rung) -> bool {
    cfg.rungs
        .lanes(rung)
        .iter()
        .any(|item| item.lane.eq_ignore_ascii_case(&lane.lane))
}

pub fn resolve_spec(cfg: &Config, spec: &str) -> Option<LaneSpec> {
    let wanted = spec.trim().to_ascii_lowercase();
    let wanted = match wanted.as_str() {
        "anthropic" => "claude".to_string(),
        "openai" => "codex".to_string(),
        "google" => "antigravity".to_string(),
        other => other.to_string(),
    };
    if let Some(lane) = cfg
        .lanes
        .iter()
        .find(|lane| lane.lane.eq_ignore_ascii_case(&wanted))
    {
        return Some(lane.clone());
    }
    if let Some(lane) = cfg
        .lanes
        .iter()
        .find(|lane| lane.names().iter().any(|name| name == &wanted))
    {
        return Some(lane.clone());
    }
    if wanted == "claude" || wanted == "codex" {
        return Some(LaneSpec {
            lane: wanted.clone(),
            vendor: Some(if wanted == "claude" {
                "anthropic".to_string()
            } else {
                "openai".to_string()
            }),
            pool: Some(wanted),
            command: String::new(),
            shared_with_chats: true,
            ..LaneSpec::default()
        });
    }
    None
}

pub fn lane_ok(ctx: &LaneContext<'_>, spec: &str, rung_ctx: Option<&RungContext>) -> (bool, String) {
    let Some(lane) = resolve_spec(ctx.cfg, spec) else {
        return (false, format!("{spec}: unknown lane or vendor {spec}"));
    };
    let eligibility = lane_eligibility(&lane, ctx, &[]);
    if let LaneEligibility::Skipped(SkipReason::OrchestratorOnly) = eligibility {
        return (false, format!("{}: chat-only pool, never workers", lane.lane));
    }
    let configured = ctx
        .cfg
        .lanes
        .iter()
        .any(|item| item.lane.eq_ignore_ascii_case(&lane.lane));
    if let Some(rung_ctx) = rung_ctx {
        if ctx.cfg.rungs.configured() && configured && !lane_in_rung(ctx.cfg, &lane, rung_ctx.rung) {
            return (
                false,
                format!(
                    "{}: {} ({})",
                    lane.lane,
                    SkipReason::NotInRung {
                        rung: rung_ctx.rung
                    },
                    rung_ctx.reason
                ),
            );
        }
    }
    match eligibility {
        LaneEligibility::Ready { .. } => (true, format!("{}: allowed", lane.lane)),
        LaneEligibility::Skipped(reason) => (false, format!("{}: {reason}", lane.lane)),
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct LoopAnswer {
    pub seconds: u64,
    pub reason: String,
}

pub fn loop_interval_answer(status: &Status, cfg: &Config, _now: i64) -> LoopAnswer {
    let rule = &cfg.loop_interval;
    let answer = |seconds: u64, reason: String| LoopAnswer {
        seconds: seconds.max(rule.min_seconds),
        reason,
    };
    let Some(pool) = status.claude_pool() else {
        return answer(rule.base_seconds, "claude pool unknown".to_string());
    };
    let ratio = pool.pace_ratio;
    let used = pool.used_pct;
    if used.is_some_and(|used| used >= rule.used_pct) {
        let used = used.unwrap_or(rule.used_pct);
        return answer(rule.far_ahead_seconds, format!("claude {used:.0}% used"));
    }
    if ratio.is_some_and(|ratio| ratio > rule.far_ahead_ratio) {
        let ratio = ratio.unwrap_or(rule.far_ahead_ratio);
        return answer(
            rule.far_ahead_seconds,
            format!("claude pace {ratio:.2}x far ahead"),
        );
    }
    match ratio {
        None => answer(rule.base_seconds, "claude pace unknown".to_string()),
        Some(ratio) if ratio > rule.ahead_ratio => {
            answer(rule.ahead_seconds, format!("claude pace {ratio:.2}x ahead"))
        }
        Some(ratio) => answer(
            rule.base_seconds,
            format!("claude pace {ratio:.2}x on pace"),
        ),
    }
}

pub fn loop_interval_seconds(status: &Status, cfg: &Config, now: i64) -> u64 {
    loop_interval_answer(status, cfg, now).seconds
}

pub fn claude_helper_allowed(status: &Status, cfg: &Config, now: i64) -> (bool, String) {
    let rule = &cfg.loop_interval;
    let Some(pool) = status.claude_pool() else {
        return (
            true,
            "Claude is under its pace: no Claude reading, assumed under pace".to_string(),
        );
    };
    if pool.is_off() {
        let note = pool.off_note();
        let why = if note.is_empty() {
            SkipReason::PoolOff {
                note: String::new(),
            }
        } else {
            SkipReason::PoolOff { note }
        };
        return (false, format!("Claude is {why}, so no Claude helper"));
    }
    if let Some(ratio) = pool.pace_ratio {
        if ratio > rule.ahead_ratio {
            let why = SkipReason::AheadOfPace {
                ratio,
                warn_ratio: rule.ahead_ratio,
            };
            return (false, format!("Claude is {why}, so no Claude helper"));
        }
        return (
            true,
            format!(
                "Claude is under its pace: pace {ratio:.2}x, within {}x",
                crate::config::trim_float(rule.ahead_ratio)
            ),
        );
    }
    let _ = now;
    (
        true,
        "Claude is under its pace: pace unknown, not ahead".to_string(),
    )
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use crate::config::{BonusReset, Config, CreditEntry, LaneSpec, LimitEntry, Pool, Stampish};
    use std::collections::HashMap;

    const NOW: i64 = 1_795_783_606;
    const DAY: i64 = 86_400;

    fn lane(name: &str, role_hint: &str) -> LaneSpec {
        let (pool, credit, chat_only) = match role_hint {
            "orchestrator" => (Some("google".to_string()), None, true),
            "heavy" => (Some(name.to_string()), None, false),
            _ => (None, Some(name.to_string()), false),
        };
        LaneSpec {
            lane: name.to_string(),
            vendor: Some(if name.starts_with("vercel") {
                "deepseek".to_string()
            } else {
                name.to_string()
            }),
            pool,
            credit,
            command: format!("run {name}"),
            chat_only,
            ..LaneSpec::default()
        }
    }

    fn config_with(lanes: Vec<LaneSpec>) -> Config {
        Config {
            lanes,
            ..Config::default()
        }
    }

    fn context<'a>(
        cfg: &'a Config,
        pools: &'a Pools,
        credits: &'a Credits,
        limits: &'a Limits,
    ) -> PickContext<'a> {
        PickContext::new(cfg, pools, credits, limits, NOW)
    }

    fn empty_limits() -> Limits {
        HashMap::new()
    }

    const HOUR: i64 = 3_600;

    fn with_bonus(cfg: &mut Config, plan: &str, hours: i64) {
        let reset = BonusReset {
            expires: Some(crate::config::iso_stamp(NOW + hours * HOUR)),
        };
        if plan == "codex" {
            cfg.codex_resets.push(reset);
        } else {
            cfg.claude_resets.push(reset);
        }
    }

    fn quiet_codex_pool(pace: f64) -> Pool {
        Pool {
            state: Some("ON".to_string()),
            pace_ratio: Some(pace),
            used_pct: Some(56.0),
            ..Pool::default()
        }
    }

    fn credit_lane_pool(pace: f64) -> Pool {
        Pool {
            state: Some("ON".to_string()),
            pace_ratio: Some(pace),
            used_pct: Some(pace * 60.0),
            ..Pool::default()
        }
    }

    fn medium_stack_with_bonus(hours: i64) -> (Config, Pools, Credits) {
        let (mut cfg, mut pools, credits) = workhorse_stack();
        cfg.rungs.medium = vec![
            crate::config::RungLane {
                lane: "commandcode".to_string(),
                ..crate::config::RungLane::default()
            },
            crate::config::RungLane {
                lane: "vercel".to_string(),
                ..crate::config::RungLane::default()
            },
            crate::config::RungLane {
                lane: "codex".to_string(),
                ..crate::config::RungLane::default()
            },
        ];
        with_bonus(&mut cfg, "codex", hours);
        pools.insert("codex".to_string(), quiet_codex_pool(1.30));
        (cfg, pools, credits)
    }

    #[test]
    fn codex_bonus_reset_inside_the_window_is_promoted_first_on_medium() {
        let (cfg, pools, credits) = medium_stack_with_bonus(18);
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Medium));
        assert_eq!(result.plan, "codex");
        assert!(
            result.reason.contains("burn complimentary reset in 18h before expiry"),
            "{}",
            result.reason
        );
        assert!(result.reason.starts_with("rung medium: heavy lane codex"));
    }

    #[test]
    fn codex_bonus_reset_is_promoted_on_hard_too() {
        let (cfg, pools, credits) = medium_stack_with_bonus(18);
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Hard));
        assert_eq!(result.plan, "codex");
        assert!(result.reason.contains("burn complimentary reset in 18h"));
    }

    #[test]
    fn codex_bonus_reset_beyond_the_window_leaves_the_deepseek_stack_alone() {
        let (cfg, pools, credits) = medium_stack_with_bonus(72);
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Medium));
        assert!(
            result.plan == "commandcode" || result.plan == "vercel",
            "{}",
            result.plan
        );
        assert!(!result.reason.contains("burn complimentary reset"));
    }

    #[test]
    fn orchestrator_bonus_reset_never_becomes_a_worker_lane() {
        let (cfg, mut pools, credits) = workhorse_stack();
        let mut cfg = cfg;
        with_bonus(&mut cfg, "claude", 10);
        pools.insert(
            "claude".to_string(),
            Pool {
                state: Some("ON".to_string()),
                used_pct: Some(40.0),
                ..Pool::default()
            },
        );
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        for rung in [Rung::Light, Rung::Medium, Rung::Hard] {
            let result = pick(&ctx, &RungContext::fixed(rung));
            assert_ne!(result.plan, "claude");
        }
        let claude = crate::config::LaneSpec {
            lane: "claude".to_string(),
            vendor: Some("anthropic".to_string()),
            chat_only: true,
            command: "run claude".to_string(),
            ..crate::config::LaneSpec::default()
        };
        assert!(burn_lane(&claude, &ctx.lane, &[]).is_none());
        assert_eq!(bonus_reset_hours(&claude, &ctx.lane), Some(10.0));
    }

    #[test]
    fn a_used_up_pool_keeps_its_bonus_reset_out_of_the_pick() {
        let (cfg, mut pools, credits) = medium_stack_with_bonus(18);
        pools.insert(
            "codex".to_string(),
            Pool {
                state: Some("ON".to_string()),
                pace_ratio: Some(0.50),
                used_pct: Some(96.0),
                ..Pool::default()
            },
        );
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Medium));
        assert_ne!(result.plan, "codex");
    }

    #[test]
    fn status_burn_marks_the_lane_before_the_config_window_does() {
        let (cfg, pools, credits) = medium_stack_with_bonus(72);
        let limits = empty_limits();
        let status = Status {
            burn: Some(crate::config::BurnPlan {
                codex: crate::config::BurnEntry {
                    reset_hours: Some(18.0),
                    burn_now: true,
                    ..crate::config::BurnEntry::default()
                },
                ..crate::config::BurnPlan::default()
            }),
            ..Status::default()
        };
        let ctx = context(&cfg, &pools, &credits, &limits).with_status(&status);
        let result = pick(&ctx, &RungContext::fixed(Rung::Medium));
        assert_eq!(result.plan, "codex");
        assert!(result.reason.contains("burn complimentary reset in 18h"));
    }

    fn workhorse_stack() -> (Config, Pools, Credits) {
        let cfg = config_with(vec![
            lane("antigravity", "orchestrator"),
            lane("commandcode", "workhorse"),
            lane("vercel", "workhorse"),
            lane("openrouter", "workhorse"),
            lane("codex", "heavy"),
        ]);
        let mut pools = Pools::new();
        pools.insert(
            "google".to_string(),
            Pool {
                state: Some("ON".to_string()),
                chat_only: true,
                used_pct: Some(49.0),
                ..Pool::default()
            },
        );
        pools.insert("codex".to_string(), credit_lane_pool(0.50));
        pools.insert(
            "credit:commandcode".to_string(),
            credit_lane_pool(0.86),
        );
        pools.insert("credit:vercel".to_string(), credit_lane_pool(0.40));
        pools.insert(
            "credit:openrouter".to_string(),
            credit_lane_pool(0.95),
        );
        let mut credits = Credits::new();
        credits.insert(
            "commandcode".to_string(),
            CreditEntry {
                usd: Some(87.0),
                resets_at: Some(Stampish::Text("2026-12-05T08:00:00Z".to_string())),
                ..CreditEntry::default()
            },
        );
        credits.insert(
            "vercel".to_string(),
            CreditEntry {
                usd: Some(14.0),
                resets_at: Some(Stampish::Text("2026-12-05T08:00:00Z".to_string())),
                ..CreditEntry::default()
            },
        );
        (cfg, pools, credits)
    }

    #[test]
    fn orchestrator_lanes_are_never_picked_on_any_rung() {
        let (cfg, pools, credits) = workhorse_stack();
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        for rung in [Rung::Light, Rung::Medium, Rung::Hard] {
            let result = pick(&ctx, &RungContext::fixed(rung));
            assert_ne!(result.plan, "antigravity");
            assert_ne!(result.plan, "claude");
            assert!(
                result.reason.contains("antigravity (chat-only pool, never workers)"),
                "rung {rung} reason {}",
                result.reason
            );
        }
    }

    #[test]
    fn lane_ok_refuses_orchestrator_lanes_with_chat_only() {
        let (cfg, pools, credits) = workhorse_stack();
        let limits = empty_limits();
        let ctx = LaneContext {
            cfg: &cfg,
            pools: &pools,
            credits: &credits,
            limits: &limits,
            now: NOW,
            status: None,
        };
        let (allowed, why) = lane_ok(&ctx, "antigravity", None);
        assert!(!allowed);
        assert_eq!(why, "antigravity: chat-only pool, never workers");
        let (allowed, why) = lane_ok(&ctx, "google", None);
        assert!(!allowed);
        assert!(why.contains("chat-only"));
        assert!(lane_ok(&ctx, "commandcode", None).0);
        assert!(lane_ok(&ctx, "codex", None).0);
        assert_eq!(
            lane_ok(&ctx, "nope", None),
            (false, "nope: unknown lane or vendor nope".to_string())
        );
    }

    #[test]
    fn expiring_value_ranks_commandcode_over_vercel() {
        let (mut cfg, mut pools, mut credits) = workhorse_stack();
        cfg.lanes.retain(|lane| lane.lane != "openrouter");
        pools.remove("credit:openrouter");
        credits.insert(
            "vercel".to_string(),
            CreditEntry {
                usd: Some(14.0),
                resets_at: Some(Stampish::Epoch((NOW + 8 * DAY) as f64)),
                ..CreditEntry::default()
            },
        );
        credits.insert(
            "commandcode".to_string(),
            CreditEntry {
                usd: Some(87.0),
                resets_at: Some(Stampish::Epoch((NOW + 4 * DAY) as f64)),
                ..CreditEntry::default()
            },
        );
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Light));
        assert_eq!(result.plan, "commandcode");
        assert!(result.reason.contains("$21.75/day"), "{}", result.reason);
        let vercel = lane_eligibility(&cfg.lanes[2], &ctx.lane, &[]);
        assert!(vercel.reason().contains("$1.75/day"), "{}", vercel.reason());
    }

    #[test]
    fn pace_breaks_ties_when_no_credit_reading_is_known() {
        let cfg = config_with(vec![
            lane("opencode", "workhorse"),
            lane("commandcode", "workhorse"),
            lane("codex", "heavy"),
        ]);
        let mut pools = Pools::new();
        pools.insert(
            "credit:opencode".to_string(),
            credit_lane_pool(0.95),
        );
        pools.insert(
            "credit:commandcode".to_string(),
            credit_lane_pool(0.30),
        );
        pools.insert("codex".to_string(), credit_lane_pool(0.50));
        let credits = Credits::new();
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Light));
        assert_eq!(result.plan, "commandcode");
        assert!(result.reason.contains("pace 0.30x"), "{}", result.reason);
    }

    #[test]
    fn medium_never_lets_a_calm_heavy_lane_steal_from_a_workhorse() {
        let (cfg, pools, credits) = workhorse_stack();
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Medium));
        assert_eq!(result.plan, "commandcode");
        assert_eq!(result.rung, Rung::Medium);
        assert!(result.reason.starts_with("rung medium: workhorse lane commandcode"));
    }

    #[test]
    fn medium_falls_back_to_heavy_only_when_every_workhorse_lane_is_unavailable() {
        let (cfg, mut pools, credits) = workhorse_stack();
        for name in ["opencode", "commandcode", "vercel", "vercel-zai", "openrouter"] {
            pools.insert(
                format!("credit:{name}"),
                Pool {
                    state: Some("OFF".to_string()),
                    note: Some("used up".to_string()),
                    ..Pool::default()
                },
            );
        }
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Medium));
        assert_eq!(result.plan, "codex");
        assert!(result.reason.contains("heavy lane codex"), "{}", result.reason);
    }

    #[test]
    fn medium_refuses_heavy_when_it_is_above_eighty_percent_pace() {
        let (cfg, mut pools, credits) = workhorse_stack();
        for name in ["opencode", "commandcode", "vercel", "vercel-zai", "openrouter"] {
            pools.insert(
                format!("credit:{name}"),
                Pool {
                    state: Some("OFF".to_string()),
                    note: Some("used up".to_string()),
                    ..Pool::default()
                },
            );
        }
        pools.insert("codex".to_string(), credit_lane_pool(0.85));
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Medium));
        assert_eq!(result.plan, "none");
        assert!(result.command.is_none());
        assert!(result.reason.contains("no free"), "{}", result.reason);
    }

    #[test]
    fn hard_picks_heavy_directly() {
        let (cfg, pools, credits) = workhorse_stack();
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Hard));
        assert_eq!(result.plan, "codex");
        assert!(result.reason.starts_with("rung hard: heavy lane codex"));
    }

    #[test]
    fn light_falls_back_to_openrouter_only_when_primary_workhorse_lanes_are_gone() {
        let (cfg, mut pools, credits) = workhorse_stack();
        for name in ["commandcode", "vercel"] {
            pools.insert(
                format!("credit:{name}"),
                Pool {
                    state: Some("OFF".to_string()),
                    ..Pool::default()
                },
            );
        }
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Light));
        assert_eq!(result.plan, "openrouter");
        let mut wide = pools.clone();
        wide.insert(
            "credit:commandcode".to_string(),
            credit_lane_pool(0.86),
        );
        let ctx = context(&cfg, &wide, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Light));
        assert_eq!(result.plan, "commandcode");
    }

    #[test]
    fn not_vendor_exclusions_skip_matching_lanes() {
        let (cfg, pools, credits) = workhorse_stack();
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits).excluding(vendor_exclusions(&[
            "commandcode".to_string(),
            "zai, vercel".to_string(),
        ]));
        let result = pick(&ctx, &RungContext::fixed(Rung::Light));
        assert_eq!(result.plan, "openrouter");
        assert!(result.reason.contains("commandcode (vendor excluded)"));
        assert_eq!(
            vendor_exclusions(&["zai, vercel".to_string()]),
            vec!["zai".to_string(), "vercel".to_string()]
        );
    }

    #[test]
    fn limited_lane_is_skipped_until_its_until_stamp() {
        let (cfg, pools, credits) = workhorse_stack();
        let mut limits = Limits::new();
        limits.insert(
            "commandcode".to_string(),
            LimitEntry::Detail(crate::config::LimitDetail {
                until: Some(Stampish::Epoch((NOW + 3600) as f64)),
                reason: Some("quota error".to_string()),
            }),
        );
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Medium));
        assert_eq!(result.plan, "vercel");
        assert!(result.reason.contains("limited until"), "{}", result.reason);
    }

    #[test]
    fn pool_off_and_credit_floor_are_both_refused() {
        let (cfg, mut pools, credits) = workhorse_stack();
        pools.insert(
            "credit:commandcode".to_string(),
            Pool {
                state: Some("ON".to_string()),
                credits_usd: Some(1.5),
                credit_floor_usd: Some(2.0),
                pace_ratio: Some(0.5),
                ..Pool::default()
            },
        );
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let eligibility = lane_eligibility(&cfg.lanes[1], &ctx.lane, &[]);
        assert_eq!(
            eligibility,
            LaneEligibility::skipped(SkipReason::BelowCreditFloor {
                remaining_usd: 1.5,
                floor_usd: 2.0
            })
        );
        let result = pick(&ctx, &RungContext::fixed(Rung::Light));
        assert_eq!(result.plan, "vercel");
    }

    #[test]
    fn ahead_of_pace_lane_is_skipped() {
        let (cfg, mut pools, credits) = workhorse_stack();
        pools.insert(
            "credit:commandcode".to_string(),
            credit_lane_pool(1.40),
        );
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let eligibility = lane_eligibility(&cfg.lanes[1], &ctx.lane, &[]);
        assert_eq!(
            eligibility,
            LaneEligibility::skipped(SkipReason::AheadOfPace {
                ratio: 1.40,
                warn_ratio: 1.1
            })
        );
    }

    #[test]
    fn loop_interval_follows_claude_pace_and_floors_at_the_minimum() {
        let cfg = Config::default();
        let status = |ratio: Option<f64>, used: Option<f64>| Status {
            pools: Pools::from([(
                "claude".to_string(),
                Pool {
                    state: Some("ON".to_string()),
                    used_pct: used,
                    pace_ratio: ratio,
                    ..Pool::default()
                },
            )]),
            ..Status::default()
        };
        assert_eq!(
            loop_interval_seconds(&status(None, None), &cfg, NOW),
            cfg.loop_interval.base_seconds
        );
        assert_eq!(
            loop_interval_seconds(&status(Some(0.8), Some(50.0)), &cfg, NOW),
            cfg.loop_interval.base_seconds
        );
        assert_eq!(
            loop_interval_seconds(&status(Some(1.05), Some(60.0)), &cfg, NOW),
            cfg.loop_interval.ahead_seconds
        );
        assert_eq!(
            loop_interval_seconds(&status(Some(1.5), Some(60.0)), &cfg, NOW),
            cfg.loop_interval.far_ahead_seconds
        );
        assert_eq!(
            loop_interval_seconds(&status(Some(0.5), Some(99.0)), &cfg, NOW),
            cfg.loop_interval.far_ahead_seconds
        );
        assert_eq!(
            loop_interval_seconds(&Status::default(), &cfg, NOW),
            cfg.loop_interval.base_seconds
        );
        let mut tiny = Config::default();
        tiny.loop_interval.base_seconds = 60;
        assert_eq!(
            loop_interval_seconds(&Status::default(), &tiny, NOW),
            tiny.loop_interval.min_seconds
        );
        assert_eq!(
            loop_interval_answer(&status(Some(1.05), Some(60.0)), &cfg, NOW).reason,
            "claude pace 1.05x ahead"
        );
    }

    #[test]
    fn claude_helper_is_refused_when_the_pool_is_off_or_ahead() {
        let cfg = Config::default();
        let status = |state: &str, ratio: Option<f64>| Status {
            pools: Pools::from([(
                "claude".to_string(),
                Pool {
                    state: Some(state.to_string()),
                    used_pct: Some(100.0),
                    pace_ratio: ratio,
                    note: Some("100% used".to_string()),
                    ..Pool::default()
                },
            )]),
            ..Status::default()
        };
        let (allowed, why) = claude_helper_allowed(&status("OFF", Some(1.44)), &cfg, NOW);
        assert!(!allowed, "{why}");
        assert!(why.contains("pool OFF"));
        let (allowed, why) = claude_helper_allowed(&cfg_ok("ON", Some(1.30)), &cfg, NOW);
        assert!(!allowed);
        assert!(why.contains("pace 1.30x"), "{why}");
        let (allowed, why) = claude_helper_allowed(&cfg_ok("ON", Some(0.40)), &cfg, NOW);
        assert!(allowed);
        assert!(why.contains("under its pace"), "{why}");
        let (allowed, _) = claude_helper_allowed(&Status::default(), &cfg, NOW);
        assert!(allowed);
    }

    fn cfg_ok(state: &str, ratio: Option<f64>) -> Status {
        Status {
            pools: Pools::from([(
                "claude".to_string(),
                Pool {
                    state: Some(state.to_string()),
                    used_pct: Some(40.0),
                    pace_ratio: ratio,
                    ..Pool::default()
                },
            )]),
            ..Status::default()
        }
    }

    #[test]
    fn lane_ok_refuses_a_lane_outside_the_brief_rung() {
        let mut cfg = Config::default();
        cfg.rungs.light = vec![crate::config::RungLane {
            lane: "commandcode".to_string(),
            ..crate::config::RungLane::default()
        }];
        cfg.lanes = vec![lane("commandcode", "workhorse"), lane("codex", "heavy")];
        let mut pools = Pools::new();
        pools.insert(
            "credit:commandcode".to_string(),
            credit_lane_pool(0.5),
        );
        pools.insert("codex".to_string(), credit_lane_pool(0.5));
        let credits = Credits::new();
        let limits = empty_limits();
        let ctx = LaneContext {
            cfg: &cfg,
            pools: &pools,
            credits: &credits,
            limits: &limits,
            now: NOW,
            status: None,
        };
        let rung = RungContext::fixed(Rung::Light);
        assert!(lane_ok(&ctx, "commandcode", Some(&rung)).0);
        let (allowed, why) = lane_ok(&ctx, "codex", Some(&rung));
        assert!(!allowed);
        assert!(why.contains("not in rung light"), "{why}");
    }

    #[test]
    fn repo_config_drives_a_pick_against_typed_state() {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../config.json");
        let cfg = Config::load(&path).unwrap();
        let mut pools = Pools::new();
        pools.insert(
            "codex".to_string(),
            Pool {
                state: Some("ON".to_string()),
                pace_ratio: Some(0.84),
                used_pct: Some(56.0),
                ..Pool::default()
            },
        );
        pools.insert(
            "credit:commandcode".to_string(),
            Pool {
                state: Some("ON".to_string()),
                pace_ratio: Some(0.86),
                ..Pool::default()
            },
        );
        pools.insert(
            "credit:vercel".to_string(),
            Pool {
                state: Some("ON".to_string()),
                pace_ratio: Some(0.40),
                ..Pool::default()
            },
        );
        pools.insert(
            "google".to_string(),
            Pool {
                state: Some("ON".to_string()),
                chat_only: true,
                ..Pool::default()
            },
        );
        pools.insert(
            "claude".to_string(),
            Pool {
                state: Some("OFF".to_string()),
                used_pct: Some(100.0),
                ..Pool::default()
            },
        );
        let mut credits = Credits::new();
        credits.insert(
            "commandcode".to_string(),
            CreditEntry {
                credits: Some(60.7084),
                resets_at: Some(Stampish::Text("2026-12-05T08:00:00Z".to_string())),
                ..CreditEntry::default()
            },
        );
        credits.insert(
            "vercel".to_string(),
            CreditEntry {
                usd: Some(14.24),
                resets_at: Some(Stampish::Text("2026-12-05T08:00:00Z".to_string())),
                ..CreditEntry::default()
            },
        );
        let limits = empty_limits();
        let ctx = context(&cfg, &pools, &credits, &limits);
        let result = pick(&ctx, &RungContext::fixed(Rung::Medium));
        assert_eq!(result.plan, "commandcode");
        assert!(!result.reason.contains("claude ("));
        assert!(result.reason.contains("antigravity (chat-only pool, never workers)"));
        let _ = DAY;
    }
}
