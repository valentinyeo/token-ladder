#![deny(clippy::all, clippy::unwrap_used, clippy::expect_used)]

pub mod collect;
pub mod config;
pub mod domain;
pub mod hooks;
pub mod ledger;
pub mod picker;
pub mod runner;
pub mod usage;

pub use config::{
    load_json_or_default, Config, CreditEntry, Credits, LaneSpec, Limits, Pool, Pools, State,
    StatePaths, Status,
};
pub use domain::{
    LaneEligibility, LaneError, LanePin, PickResult, Role, Rung, RungContext, RungError,
    SkipReason, NO_LANE, RUNGS,
};
pub use picker::{
    claude_helper_allowed, lane_ok, loop_interval_answer, loop_interval_seconds, pick,
    vendor_exclusions, LaneContext, LoopAnswer, PickContext,
};
pub use runner::{author_model, job_key, mark_limited, set_credit, RunRequest, RunnerError};
pub use usage::{Env, Window};
