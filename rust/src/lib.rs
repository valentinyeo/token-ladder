#![deny(clippy::all, clippy::unwrap_used, clippy::expect_used)]

pub mod config;
pub mod domain;
pub mod hooks;
pub mod picker;

pub use config::{
    load_json_or_default, Config, CreditEntry, Credits, LaneSpec, Limits, Pool, Pools, State,
    StatePaths, Status,
};
pub use domain::{
    LaneEligibility, PickResult, Role, Rung, RungContext, RungError, SkipReason, NO_LANE, RUNGS,
};
pub use picker::{
    claude_helper_allowed, lane_ok, loop_interval_answer, loop_interval_seconds, pick,
    vendor_exclusions, LaneContext, LoopAnswer, PickContext,
};
