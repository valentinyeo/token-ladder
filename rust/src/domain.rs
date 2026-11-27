use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::str::FromStr;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Orchestrator,
    Workhorse,
    Heavy,
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::Orchestrator => "orchestrator",
            Role::Workhorse => "workhorse",
            Role::Heavy => "heavy",
        }
    }

    pub fn runs_workers(self) -> bool {
        self != Role::Orchestrator
    }
}

impl fmt::Display for Role {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Rung {
    Light,
    Medium,
    Hard,
}

pub const RUNGS: [Rung; 3] = [Rung::Light, Rung::Medium, Rung::Hard];

impl Rung {
    pub fn as_str(self) -> &'static str {
        match self {
            Rung::Light => "light",
            Rung::Medium => "medium",
            Rung::Hard => "hard",
        }
    }
}

impl fmt::Display for Rung {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("brief needs one Rung: light|medium|hard line, got {value:?}")]
pub struct RungError {
    pub value: String,
}

impl RungError {
    pub fn new(value: impl Into<String>) -> Self {
        RungError { value: value.into() }
    }
}

impl FromStr for Rung {
    type Err = RungError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.trim().to_ascii_lowercase().as_str() {
            "light" => Ok(Rung::Light),
            "medium" => Ok(Rung::Medium),
            "hard" => Ok(Rung::Hard),
            other => Err(RungError::new(other)),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "detail", rename_all = "snake_case")]
pub enum SkipReason {
    OrchestratorOnly,
    BelowCreditFloor { remaining_usd: f64, floor_usd: f64 },
    LimitedUntil { until_epoch: i64, note: String },
    PoolOff { note: String },
    AheadOfPace { ratio: f64, warn_ratio: f64 },
    VendorExcluded,
    NotInRung { rung: Rung },
}

pub fn utc_stamp(epoch: i64) -> String {
    match DateTime::<Utc>::from_timestamp(epoch, 0) {
        Some(stamp) => stamp.format("%Y-%m-%d %H:%M UTC").to_string(),
        None => format!("epoch {epoch}"),
    }
}

impl fmt::Display for SkipReason {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SkipReason::OrchestratorOnly => f.write_str("chat-only pool, never workers"),
            SkipReason::BelowCreditFloor {
                remaining_usd,
                floor_usd,
            } => write!(
                f,
                "credit ${remaining_usd:.2} is at or below floor ${}",
                trim_float(*floor_usd)
            ),
            SkipReason::LimitedUntil { until_epoch, note } => {
                let stamp = utc_stamp(*until_epoch);
                if note.is_empty() {
                    write!(f, "limited until {stamp}")
                } else {
                    write!(f, "limited until {stamp} ({note})")
                }
            }
            SkipReason::PoolOff { note } => {
                if note.is_empty() {
                    f.write_str("pool OFF")
                } else {
                    write!(f, "pool OFF: {note}")
                }
            }
            SkipReason::AheadOfPace {
                ratio,
                warn_ratio,
            } => write!(
                f,
                "pace {ratio:.2}x is above {}x",
                trim_float(*warn_ratio)
            ),
            SkipReason::VendorExcluded => f.write_str("vendor excluded"),
            SkipReason::NotInRung { rung } => write!(f, "not in rung {rung}"),
        }
    }
}

impl SkipReason {
    pub fn clause(&self) -> String {
        self.to_string()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "state", content = "detail", rename_all = "snake_case")]
pub enum LaneEligibility {
    Ready { score: f64, reason: String },
    Skipped(SkipReason),
}

impl LaneEligibility {
    pub fn ready(score: f64, reason: impl Into<String>) -> Self {
        LaneEligibility::Ready {
            score,
            reason: reason.into(),
        }
    }

    pub fn skipped(reason: SkipReason) -> Self {
        LaneEligibility::Skipped(reason)
    }

    pub fn is_ready(&self) -> bool {
        matches!(self, LaneEligibility::Ready { .. })
    }

    pub fn reason(&self) -> String {
        match self {
            LaneEligibility::Ready { reason, .. } => reason.clone(),
            LaneEligibility::Skipped(reason) => reason.clause(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PickResult {
    pub plan: String,
    pub command: Option<String>,
    pub reason: String,
    pub rung: Rung,
    pub rung_source: String,
    pub rung_reason: String,
}

pub const NO_LANE: &str = "none";

impl PickResult {
    pub fn lane(plan: impl Into<String>, command: impl Into<String>, reason: impl Into<String>, rung: &RungContext) -> Self {
        PickResult {
            plan: plan.into(),
            command: Some(command.into()),
            reason: reason.into(),
            rung: rung.rung,
            rung_source: rung.source.clone(),
            rung_reason: rung.reason.clone(),
        }
    }

    pub fn empty(reason: impl Into<String>, rung: &RungContext) -> Self {
        PickResult {
            plan: NO_LANE.to_string(),
            command: None,
            reason: reason.into(),
            rung: rung.rung,
            rung_source: rung.source.clone(),
            rung_reason: rung.reason.clone(),
        }
    }

    pub fn picked(&self) -> bool {
        self.command.is_some()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RungContext {
    pub rung: Rung,
    pub source: String,
    pub reason: String,
}

impl RungContext {
    pub fn from_brief(text: &str, default: Rung) -> Result<Self, RungError> {
        let matches: Vec<&str> = text
            .lines()
            .filter_map(|line| {
                let trimmed = line.trim_end_matches('\r');
                trimmed
                    .strip_prefix("Rung:")
                    .map(|rest| rest.trim_matches([' ', '\t']))
            })
            .collect();
        match matches.as_slice() {
            [] => Ok(RungContext {
                rung: default,
                source: "default".to_string(),
                reason: format!("rung {default}"),
            }),
            [only] => {
                let rung = Rung::from_str(only)?;
                Ok(RungContext {
                    rung,
                    source: "brief line".to_string(),
                    reason: format!("rung {rung}"),
                })
            }
            [_, ..] => Err(RungError::new(format!("{} Rung lines", matches.len()))),
        }
    }

    pub fn fixed(rung: Rung) -> Self {
        RungContext {
            rung,
            source: "flag".to_string(),
            reason: format!("rung {rung}"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("lane needs one lane name, got {value:?}")]
pub struct LaneError {
    pub value: String,
}

impl LaneError {
    pub fn new(value: impl Into<String>) -> Self {
        LaneError {
            value: value.into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LanePin {
    pub spec: String,
    pub source: String,
    pub reason: String,
}

impl LanePin {
    pub fn from_brief(text: &str) -> Result<Option<Self>, LaneError> {
        let matches: Vec<&str> = text
            .lines()
            .filter_map(|line| {
                let trimmed = line.trim_end_matches('\r');
                trimmed
                    .strip_prefix("Lane:")
                    .map(|rest| rest.trim_matches([' ', '\t']))
            })
            .collect();
        match matches.as_slice() {
            [] => Ok(None),
            [only] => Ok(Some(LanePin::from_line(only)?)),
            [_, ..] => Err(LaneError::new(format!("{} Lane lines", matches.len()))),
        }
    }

    pub fn from_line(spec: &str) -> Result<Self, LaneError> {
        let spec = spec.trim().to_ascii_lowercase();
        if spec.is_empty() {
            return Err(LaneError::new("(empty)"));
        }
        Ok(LanePin {
            reason: format!("lane {spec}"),
            spec,
            source: "brief line".to_string(),
        })
    }

    pub fn fixed(spec: &str) -> Result<Self, LaneError> {
        let spec = spec.trim().to_ascii_lowercase();
        if spec.is_empty() {
            return Err(LaneError::new("(empty)"));
        }
        Ok(LanePin {
            reason: format!("lane {spec}"),
            spec,
            source: "flag".to_string(),
        })
    }
}

pub fn trim_float(value: f64) -> String {
    if value.is_finite() && value.fract() == 0.0 && value.abs() < 1e15 {
        format!("{value:.0}")
    } else {
        let text = format!("{value}");
        text
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn rung_parses_case_insensitively_and_rejects_noise() {
        assert_eq!(Rung::from_str("Light").unwrap(), Rung::Light);
        assert_eq!(Rung::from_str(" medium ").unwrap(), Rung::Medium);
        assert_eq!(Rung::from_str("HARD").unwrap(), Rung::Hard);
        assert!(Rung::from_str("extreme").is_err());
    }

    #[test]
    fn brief_rung_line_is_read_once() {
        let ctx = RungContext::from_brief("Ticket: X\nRung: hard\n", Rung::Medium).unwrap();
        assert_eq!(ctx.rung, Rung::Hard);
        assert_eq!(ctx.source, "brief line");
        assert_eq!(ctx.reason, "rung hard");
    }

    #[test]
    fn brief_without_a_rung_line_defaults_to_medium() {
        let ctx = RungContext::from_brief("Ticket: X\n", Rung::Medium).unwrap();
        assert_eq!(ctx.rung, Rung::Medium);
        assert_eq!(ctx.source, "default");
    }

    #[test]
    fn brief_with_two_rung_lines_is_refused() {
        assert!(RungContext::from_brief("Rung: light\nRung: hard\n", Rung::Medium).is_err());
    }

    #[test]
    fn brief_with_unknown_rung_is_refused() {
        assert!(RungContext::from_brief("Rung: turbo\n", Rung::Medium).is_err());
    }

    #[test]
    fn brief_lane_line_is_read_once_and_normalised() {
        assert_eq!(LanePin::from_brief("Ticket: X\n").unwrap(), None);
        let pin = LanePin::from_brief("Lane:  Codex \n").unwrap().unwrap();
        assert_eq!(pin.spec, "codex");
        assert_eq!(pin.source, "brief line");
        assert_eq!(pin.reason, "lane codex");
        assert!(LanePin::from_brief("Lane: codex\nLane: vercel\n").is_err());
        assert!(LanePin::from_brief("Lane: \n").is_err());
    }

    #[test]
    fn a_lane_flag_pin_trims_its_spec() {
        let pin = LanePin::fixed(" OpenAI ").unwrap();
        assert_eq!(pin.spec, "openai");
        assert_eq!(pin.source, "flag");
        assert!(LanePin::fixed("  ").is_err());
    }

    #[test]
    fn orchestrator_role_is_the_only_role_that_never_runs_workers() {
        assert!(!Role::Orchestrator.runs_workers());
        assert!(Role::Workhorse.runs_workers());
        assert!(Role::Heavy.runs_workers());
    }

    #[test]
    fn skip_reasons_display_one_clause_each() {
        assert_eq!(
            SkipReason::OrchestratorOnly.to_string(),
            "chat-only pool, never workers"
        );
        assert_eq!(
            SkipReason::NotInRung { rung: Rung::Light }.to_string(),
            "not in rung light"
        );
        assert_eq!(
            SkipReason::BelowCreditFloor {
                remaining_usd: 1.5,
                floor_usd: 2.0
            }
            .to_string(),
            "credit $1.50 is at or below floor $2"
        );
        assert_eq!(
            SkipReason::AheadOfPace {
                ratio: 1.25,
                warn_ratio: 1.1
            }
            .to_string(),
            "pace 1.25x is above 1.1x"
        );
        assert!(SkipReason::PoolOff {
            note: "used up".to_string()
        }
        .to_string()
        .starts_with("pool OFF"));
    }

    #[test]
    fn pick_result_serializes_the_plan_pick_json_shape() {
        let rung = RungContext::fixed(Rung::Medium);
        let result = PickResult::lane("codex", "run codex", "why", &rung);
        let json = serde_json::to_value(&result).unwrap();
        assert_eq!(json["plan"], "codex");
        assert_eq!(json["command"], "run codex");
        assert_eq!(json["reason"], "why");
        assert_eq!(json["rung"], "medium");
        assert_eq!(json["rung_source"], "flag");
        assert_eq!(json["rung_reason"], "rung medium");
        let empty = PickResult::empty("nothing free", &rung);
        let json = serde_json::to_value(&empty).unwrap();
        assert_eq!(json["plan"], "none");
        assert!(json["command"].is_null());
    }

    #[test]
    fn eligibility_carries_a_score_and_a_reason() {
        let ready = LaneEligibility::ready(21.75, "$21.75/day");
        assert!(ready.is_ready());
        assert_eq!(ready.reason(), "$21.75/day");
        let skipped = LaneEligibility::skipped(SkipReason::VendorExcluded);
        assert!(!skipped.is_ready());
        assert_eq!(skipped.reason(), "vendor excluded");
    }
}
