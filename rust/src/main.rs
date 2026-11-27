#![deny(clippy::all, clippy::unwrap_used, clippy::expect_used)]

use clap::{Args, Parser, Subcommand};
use serde::Serialize;
use serde_json::Value;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use token_ladder::config::{
    iso_stamp, trim_float, Config, Credits, Limits, Pool, Pools, State, StatePaths,
};
use token_ladder::domain::{PickResult, Rung, RungContext};
use token_ladder::hooks;
use token_ladder::picker::{self, LaneContext, PickContext};
use token_ladder::runner::{self, RunRequest};

const ALLOWED_EXIT: u8 = 0;
const REFUSED_EXIT: u8 = 3;
const FAILED_EXIT: u8 = 1;
const USAGE_EXIT: u8 = 2;

#[derive(Parser, Debug)]
#[command(
    name = "token-ladder",
    version,
    about = "Token Ladder: pool state, tier-first lane picker and fast hooks"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[allow(clippy::large_enum_variant)]
#[derive(Subcommand, Debug)]
enum Command {
    Status,
    Pools(PoolsArgs),
    Pick(PickArgs),
    HookBash,
    HookPrompt,
    HookAgent,
}

#[derive(Args, Debug)]
struct PoolsArgs {
    #[arg(long)]
    json: bool,
    #[arg(long, value_name = "FILE")]
    pools: Option<PathBuf>,
    #[arg(long, value_name = "PATH")]
    config: Option<PathBuf>,
}

#[derive(Args, Debug)]
struct PickArgs {
    #[arg(long)]
    json: bool,
    #[arg(long, value_name = "PATH", default_value = "brief.txt")]
    brief: PathBuf,
    #[arg(long, value_name = "light|medium|hard")]
    rung: Option<String>,
    #[arg(long = "plans")]
    plans: bool,
    #[arg(long = "lane-ok", value_name = "LANE_OR_VENDOR")]
    lane_ok: Option<String>,
    #[arg(long = "not-vendor", value_name = "VENDOR")]
    not_vendor: Vec<String>,
    #[arg(long = "loop-interval")]
    loop_interval: bool,
    #[arg(long = "claude-helper")]
    claude_helper: bool,
    #[arg(long = "run")]
    run: bool,
    #[arg(long, value_name = "LANE")]
    limited: Option<String>,
    #[arg(long, value_name = "LANE_USD", num_args = 2)]
    set_credit: Option<Vec<String>>,
    #[arg(long = "credit-at", value_name = "ISO8601")]
    credit_at: Option<String>,
    #[arg(long = "credit-source", value_name = "TEXT")]
    credit_source: Option<String>,
    #[arg(long, value_name = "N")]
    hours: Option<f64>,
    #[arg(long, value_name = "TEXT")]
    reason: Option<String>,
    #[arg(long, value_name = "FILE")]
    pools: Option<PathBuf>,
    #[arg(long, value_name = "PATH")]
    config: Option<PathBuf>,
}

#[derive(Parser, Debug)]
#[command(
    name = "plan-pick",
    version,
    about = "Pick the lane for the next background worker"
)]
struct PlanPickCli {
    #[command(flatten)]
    args: PickArgs,
}

#[derive(Serialize)]
struct PoolsReport {
    claude: PoolReport,
    codex: PoolReport,
    grok: PoolReport,
    google: PoolReport,
}

#[derive(Serialize)]
struct PoolReport {
    state: String,
    used_pct: Option<f64>,
    pace_ratio: Option<f64>,
    resets_at: Option<i64>,
    hours_left: Option<f64>,
    window_minutes: Option<u64>,
    chat_only: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    note: Option<String>,
}

impl PoolReport {
    fn from(pool: Option<&Pool>, chat_only: bool) -> Self {
        match pool {
            Some(pool) => PoolReport {
                state: pool.state_label(),
                used_pct: pool.used_pct,
                pace_ratio: pool.pace_ratio,
                resets_at: pool.reset_epoch(),
                hours_left: pool.hours_left,
                window_minutes: pool.window_minutes,
                chat_only: pool.chat_only || chat_only,
                note: pool.note.clone(),
            },
            None => PoolReport {
                state: "unknown".to_string(),
                used_pct: None,
                pace_ratio: None,
                resets_at: None,
                hours_left: None,
                window_minutes: None,
                chat_only,
                note: Some("no reading".to_string()),
            },
        }
    }

    fn line(&self, name: &str) -> String {
        let used = match self.used_pct {
            Some(used) => format!("{used:.0}%"),
            None => "?".to_string(),
        };
        let reset = match self.resets_at {
            Some(epoch) => iso_stamp(epoch),
            None => "unknown".to_string(),
        };
        let mut state = self.state.clone();
        if self.chat_only {
            state.push_str(" (chat-only)");
        }
        match self.note.as_deref().filter(|note| !note.is_empty()) {
            Some(note) => format!("{name}: {used} used, reset {reset}, {state} ({note})"),
            None => format!("{name}: {used} used, reset {reset}, {state}"),
        }
    }
}

fn state_paths() -> StatePaths {
    match std::env::var_os("TOKEN_LADDER_STATE_DIR") {
        Some(dir) => StatePaths::under(PathBuf::from(dir)),
        None => StatePaths::default(),
    }
}

fn load_config(path: Option<&Path>) -> Result<Config, String> {
    let path = match path {
        Some(path) => path.to_path_buf(),
        None => token_ladder::config::config_path(),
    };
    let local = match path {
        _ if std::env::var_os("TOKEN_LADDER_LOCAL_CONFIG").is_some() => {
            token_ladder::config::local_config_path()
        }
        _ => {
            let beside = path.with_file_name("config.local.json");
            if beside.is_file() {
                Some(beside)
            } else {
                None
            }
        }
    };
    Config::load_with_local(&path, local.as_deref()).map_err(|error| error.to_string())
}

fn load_pools(path: Option<&Path>, state: &State, config: &Config) -> Result<Pools, String> {
    match path {
        Some(path) => {
            let text = std::fs::read_to_string(path)
                .map_err(|error| format!("cannot read pools from {}: {error}", path.display()))?;
            parse_pools(&text)
        }
        None => Ok(with_credit_pools(state.pools().clone(), state, config)),
    }
}

fn parse_pools(text: &str) -> Result<Pools, String> {
    let value: Value = serde_json::from_str(text).map_err(|error| format!("bad pool JSON: {error}"))?;
    if let Some(wrapper) = value.as_object().and_then(|object| object.get("pools")) {
        return serde_json::from_value::<Pools>(wrapper.clone())
            .map_err(|error| format!("bad pool JSON: {error}"));
    }
    serde_json::from_value::<Pools>(value).map_err(|error| format!("bad pool JSON: {error}"))
}

fn with_credit_pools(mut pools: Pools, state: &State, config: &Config) -> Pools {
    for (name, entry) in &state.credits {
        let key = format!("credit:{name}");
        if pools.contains_key(&key) {
            continue;
        }
        if let Some(pool) = credit_pool(entry, config) {
            pools.insert(key, pool);
        }
    }
    pools
}

fn credit_pool(entry: &token_ladder::config::CreditEntry, config: &Config) -> Option<Pool> {
    let remaining = entry.remaining_usd()?;
    let floor = config.credit_floor_usd;
    let state = if remaining < floor { "OFF" } else { "ON" };
    let mut pool = Pool {
        state: Some(state.to_string()),
        credits_usd: Some(remaining),
        credit_floor_usd: Some(floor),
        note: Some(format!("${remaining:.2} credit left")),
        resets_at: entry.resets_at.clone(),
        ..Pool::default()
    };
    if let Some(limit) = entry.limit_usd.filter(|limit| *limit > 0.0) {
        pool.used_pct = Some(((limit - remaining) / limit * 100.0).clamp(0.0, 100.0));
    }
    Some(pool)
}

fn now_epoch() -> i64 {
    chrono::Utc::now().timestamp()
}

fn read_stdin() -> String {
    let mut buffer = String::new();
    match std::io::stdin().read_to_string(&mut buffer) {
        Ok(_) => buffer,
        Err(_) => String::new(),
    }
}

fn parse_json(text: &str) -> Option<Value> {
    serde_json::from_str(text).ok()
}

fn deny(reason: &str) -> ExitCode {
    let payload = serde_json::json!({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason
        }
    });
    println!("{payload}");
    ExitCode::from(ALLOWED_EXIT)
}

fn hook_bash() -> ExitCode {
    let env = hooks::process_env();
    let payload = parse_json(&read_stdin());
    let Some(payload) = payload else {
        return ExitCode::from(ALLOWED_EXIT);
    };
    match hooks::command_refusal(&payload, &env) {
        Some(reason) => deny(&reason),
        None => ExitCode::from(ALLOWED_EXIT),
    }
}

fn hook_agent() -> ExitCode {
    let state = State::load(&state_paths());
    let Some(payload) = parse_json(&read_stdin()) else {
        return ExitCode::from(ALLOWED_EXIT);
    };
    match hooks::agent_refusal(&payload, &state.status) {
        Some(reason) => deny(&reason),
        None => ExitCode::from(ALLOWED_EXIT),
    }
}

fn hook_prompt() -> ExitCode {
    let state = State::load(&state_paths());
    if let Some(line) = hooks::prompt_usage_line(&state.status, &state.credits, now_epoch()) {
        println!("{line}");
    }
    ExitCode::from(ALLOWED_EXIT)
}

fn status_command() -> ExitCode {
    let state = State::load(&state_paths());
    let pools = state.pools();
    let now = now_epoch();
    let age = state.status.age_seconds(now);
    let mode = state.status.mode.clone().unwrap_or_else(|| "unknown".to_string());
    let states: Vec<String> = ["claude", "codex", "grok", "google"]
        .iter()
        .map(|name| {
            let pool = pools.get(*name);
            format!("{} {}", capitalise(name), PoolReport::from(pool, false).state)
        })
        .collect();
    let stale = match age {
        Some(age) if age > 3.0 * 3600.0 => {
            format!(" STALE: last check {:.0} min ago.", age / 60.0)
        }
        _ => String::new(),
    };
    println!(
        "Token ladder: {}. Mode {mode}.{stale}",
        states.join("; ")
    );
    ExitCode::from(ALLOWED_EXIT)
}

fn capitalise(name: &str) -> String {
    let mut chars = name.chars();
    match chars.next() {
        Some(first) => format!("{}{}", first.to_uppercase(), chars.as_str()),
        None => String::new(),
    }
}

fn pools_command(args: &PoolsArgs) -> Result<ExitCode, String> {
    let config = load_config(args.config.as_deref())?;
    let state = State::load(&state_paths());
    let pools = load_pools(args.pools.as_deref(), &state, &config)?;
    let report = PoolsReport {
        claude: PoolReport::from(pools.get("claude"), false),
        codex: PoolReport::from(pools.get("codex"), false),
        grok: PoolReport::from(pools.get("grok"), false),
        google: PoolReport::from(pools.get("google"), true),
    };
    if args.json {
        let text = serde_json::to_string(&report).map_err(|error| error.to_string())?;
        println!("{text}");
    } else {
        println!("{}", report.claude.line("Claude"));
        println!("{}", report.codex.line("Codex"));
        println!("{}", report.grok.line("Grok"));
        println!("{}", report.google.line("Google"));
    }
    Ok(ExitCode::from(ALLOWED_EXIT))
}

fn rung_for(args: &PickArgs) -> Result<RungContext, String> {
    match args.rung.as_deref() {
        Some(value) => value
            .parse::<Rung>()
            .map(RungContext::fixed)
            .map_err(|error| error.to_string()),
        None => {
            let text = std::fs::read_to_string(&args.brief).unwrap_or_default();
            RungContext::from_brief(&text, Rung::Medium).map_err(|error| error.to_string())
        }
    }
}

fn lane_context<'a>(
    config: &'a Config,
    pools: &'a Pools,
    credits: &'a Credits,
    limits: &'a Limits,
    now: i64,
) -> LaneContext<'a> {
    LaneContext {
        cfg: config,
        pools,
        credits,
        limits,
        now,
    }
}

fn plans_report(config: &Config, state: &State, pools: &Pools, json: bool) -> ExitCode {
    let now = now_epoch();
    let mut rows: Vec<Value> = Vec::new();
    for plan in &config.plans {
        let pool = plan.pool.as_deref().and_then(|name| pools.get(name));
        let credit = plan.credit.as_deref().and_then(|name| state.credits.get(name));
        let chat_only = plan.chat_only || pool.is_some_and(|pool| pool.chat_only);
        let mut row = serde_json::json!({
            "name": plan.name,
            "vendor": plan.vendor,
        });
        if let Some(row) = row.as_object_mut() {
            row.insert("pool".to_string(), serde_json::json!(plan.pool));
            row.insert("chat_only".to_string(), serde_json::json!(chat_only));
            row.insert(
                "extra_cost".to_string(),
                serde_json::json!(plan.extra_cost),
            );
        }
        if let Some(pool) = pool {
            if let Some(object) = row.as_object_mut() {
                object.insert("used_pct".to_string(), serde_json::json!(pool.used_pct));
                object.insert("pace_ratio".to_string(), serde_json::json!(pool.pace_ratio));
                object.insert("state".to_string(), serde_json::json!(pool.state_label()));
            }
        }
        if let Some(credit) = credit {
            if let Some(object) = row.as_object_mut() {
                object.insert(
                    "remaining_usd".to_string(),
                    serde_json::json!(credit.remaining_usd()),
                );
                object.insert(
                    "resets_at".to_string(),
                    serde_json::json!(credit.reset_epoch()),
                );
            }
        }
        if let Some(cycle) = plan.usd_per_cycle {
            if let Some(object) = row.as_object_mut() {
                object.insert("usd_per_cycle".to_string(), serde_json::json!(cycle));
            }
        }
        rows.push(row);
    }
    if json {
        let wire = serde_json::json!({
            "ts": now,
            "plans": rows,
            "config_path": token_ladder::config::config_path().display().to_string(),
        });
        println!("{wire}");
        return ExitCode::from(ALLOWED_EXIT);
    }
    for plan in &config.plans {
        let models = plan.included_models.join(", ");
        let places = plan.runs_on.join(" and ");
        let mut line = format!(
            "{} ({}): {models}. Runs on {places}.",
            plan.name,
            plan.vendor.clone().unwrap_or_default()
        );
        let pool = plan.pool.as_deref().and_then(|name| pools.get(name));
        if let Some(name) = plan.pool.as_deref() {
            line.push_str(&format!(" Pool: {name}."));
        }
        if plan.chat_only || pool.is_some_and(|pool| pool.chat_only) {
            line.push_str(" Chat-only / orchestrator-only; chat-only pool, never workers.");
        }
        if let Some(pool) = pool {
            if !plan.chat_only && !pool.chat_only {
                match pool.pace_ratio {
                    Some(ratio) => line.push_str(&format!(" Pace: {ratio:.2}x.")),
                    None => line.push_str(" Pace: unknown."),
                }
            }
        }
        if let Some(credit) = plan.credit.as_deref().and_then(|name| state.credits.get(name)) {
            match (credit.remaining_usd(), plan.usd_per_cycle) {
                (Some(left), Some(cycle)) => {
                    line.push_str(&format!(" ${left:.2} of ${} left this cycle.", trim_float(cycle)))
                }
                (Some(left), None) => line.push_str(&format!(" ${left:.2} left.")),
                _ => {}
            }
        }
        if let Some(extra) = plan.extra_cost.as_deref() {
            line.push_str(&format!(" Extra cost: {extra}."));
        }
        println!("{line}");
    }
    ExitCode::from(ALLOWED_EXIT)
}

fn limited_command(
    args: &PickArgs,
    config: &Config,
    lane: &str,
    now: i64,
) -> Result<ExitCode, String> {
    if config.lane(lane).is_none() {
        let names: Vec<String> = config.lanes.iter().map(|spec| spec.lane.clone()).collect();
        eprintln!(
            "plan-pick: unknown lane {lane}; lanes: {}",
            names.join(", ")
        );
        return Ok(ExitCode::from(USAGE_EXIT));
    }
    let hours = args.hours.unwrap_or(config.limited_hours);
    let reason = args.reason.as_deref().unwrap_or("manual skip");
    let until = runner::mark_limited(
        &state_paths().limits(),
        lane,
        hours,
        now as f64,
        reason,
        "manual",
        "",
    )
    .map_err(|error| error.to_string())?;
    println!(
        "Lane {lane} is skipped until {}.",
        runner::until_text(until, now as f64)
    );
    Ok(ExitCode::from(ALLOWED_EXIT))
}

fn set_credit_command(
    args: &PickArgs,
    config: &Config,
    values: &[String],
    now: i64,
) -> Result<ExitCode, String> {
    let Some(lane) = values.first() else {
        return Ok(ExitCode::from(USAGE_EXIT));
    };
    let Some(text) = values.get(1) else {
        return Ok(ExitCode::from(USAGE_EXIT));
    };
    let known = runner::credit_names(config);
    if !known.iter().any(|name| name == lane) {
        eprintln!(
            "plan-pick: --set-credit needs a credit lane, one of: {}",
            known.join(", ")
        );
        return Ok(ExitCode::from(USAGE_EXIT));
    }
    let Ok(usd) = text.trim().parse::<f64>() else {
        eprintln!("plan-pick: --set-credit needs a number of USD, got {text}");
        return Ok(ExitCode::from(USAGE_EXIT));
    };
    let at = runner::stamp_or_now(args.credit_at.as_deref(), now as f64)
        .map_err(|error| error.to_string())?;
    let source = args
        .credit_source
        .clone()
        .unwrap_or_else(|| "manual anchor".to_string());
    if !usd.is_finite() || usd < 0.0 || at > now as f64 || source.trim().is_empty() {
        eprintln!(
            "plan-pick: --set-credit needs a finite nonnegative USD balance, a nonfuture time and a source"
        );
        return Ok(ExitCode::from(USAGE_EXIT));
    }
    runner::set_credit(&state_paths().credits(), lane, usd, at, &source)
        .map_err(|error| error.to_string())?;
    println!(
        "Stored {lane} balance anchor at {} ({}).",
        iso_stamp(at as i64),
        source
    );
    Ok(ExitCode::from(ALLOWED_EXIT))
}

fn run_command(
    args: &PickArgs,
    config: &Config,
    state: &State,
    now: i64,
) -> Result<ExitCode, String> {
    if args.brief.as_path() != Path::new(runner::DEFAULT_BRIEF) {
        eprintln!("plan-pick: --run reads brief.txt; use --brief only for dry runs");
        return Ok(ExitCode::from(USAGE_EXIT));
    }
    let paths = state_paths();
    let credits = state.credits.clone();
    let pools = load_pools(args.pools.as_deref(), state, config)?;
    let request = RunRequest {
        cfg: config,
        pools: &pools,
        credits: &credits,
        paths: &paths,
        brief: PathBuf::from(runner::DEFAULT_BRIEF),
        excluded: picker::vendor_exclusions(&args.not_vendor),
        now: now as f64,
    };
    runner::run(&request)
        .map(ExitCode::from)
        .map_err(|error| error.to_string())
}

fn pick_command(args: &PickArgs) -> Result<ExitCode, String> {
    let config = load_config(args.config.as_deref())?;
    let state = State::load(&state_paths());
    let now = now_epoch();
    if let Some(lane) = args.limited.as_deref() {
        return limited_command(args, &config, lane, now);
    }
    if let Some(values) = args.set_credit.as_deref() {
        return set_credit_command(args, &config, values, now);
    }
    if args.run {
        return run_command(args, &config, &state, now);
    }
    let pools = load_pools(args.pools.as_deref(), &state, &config)?;
    let credits = state.credits.clone();
    let limits = state.limits.clone();
    let lane_ctx = lane_context(&config, &pools, &credits, &limits, now);
    if args.plans {
        return Ok(plans_report(&config, &state, &pools, args.json));
    }
    if args.loop_interval {
        let answer = picker::loop_interval_answer(&state.status, &config, now);
        if args.json {
            let wire = serde_json::json!({"seconds": answer.seconds, "reason": answer.reason});
            println!("{wire}");
        } else {
            println!("{} {}", answer.seconds, answer.reason);
        }
        return Ok(ExitCode::from(ALLOWED_EXIT));
    }
    if args.claude_helper {
        let (allowed, reason) = picker::claude_helper_allowed(&state.status, &config, now);
        if args.json {
            let wire = serde_json::json!({"allowed": allowed, "reason": reason});
            println!("{wire}");
        } else {
            println!(
                "Claude helper: {}. Why: {reason}.",
                if allowed { "allowed" } else { "refused" }
            );
        }
        return Ok(ExitCode::from(if allowed {
            ALLOWED_EXIT
        } else {
            REFUSED_EXIT
        }));
    }
    let rung_ctx = rung_for(args)?;
    if let Some(spec) = args.lane_ok.as_deref() {
        let (allowed, reason) = picker::lane_ok(&lane_ctx, spec, Some(&rung_ctx));
        println!("{}", reason.split_whitespace().collect::<Vec<_>>().join(" "));
        return Ok(ExitCode::from(if allowed {
            ALLOWED_EXIT
        } else {
            REFUSED_EXIT
        }));
    }
    let excluded = picker::vendor_exclusions(&args.not_vendor);
    let ctx = PickContext {
        lane: lane_ctx,
        excluded,
    };
    let result = picker::pick(&ctx, &rung_ctx);
    print_pick(&result, args.json);
    Ok(ExitCode::from(ALLOWED_EXIT))
}

fn print_pick(result: &PickResult, json: bool) {
    if json {
        match serde_json::to_string(result) {
            Ok(text) => println!("{text}"),
            Err(error) => eprintln!("token-ladder: cannot print result: {error}"),
        }
        return;
    }
    let resolved = format!(
        " Rung: {} ({}).",
        result.rung,
        result.rung_reason
    );
    match result.command.as_deref() {
        Some(command) => println!(
            "Worker plan: {}. Launch: ~/.local/bin/plan-pick --run (reads brief.txt, runs {command}, moves to the next lane on a 429). Why: {}.{resolved}",
            result.plan, result.reason
        ),
        None => println!("Worker plan: none. Why: {}.{resolved}", result.reason),
    }
}

fn run(cli: Cli) -> Result<ExitCode, String> {
    match &cli.command {
        Command::Status => Ok(status_command()),
        Command::Pools(args) => pools_command(args),
        Command::Pick(args) => pick_command(args),
        Command::HookBash => Ok(hook_bash()),
        Command::HookPrompt => Ok(hook_prompt()),
        Command::HookAgent => Ok(hook_agent()),
    }
}

fn plan_pick(rest: Vec<String>) -> ExitCode {
    let args = match PlanPickCli::try_parse_from(
        std::iter::once("plan-pick".to_string()).chain(rest),
    ) {
        Ok(cli) => cli.args,
        Err(error) => {
            let _ = error.print();
            return ExitCode::from(FAILED_EXIT);
        }
    };
    match pick_command(&args) {
        Ok(code) => code,
        Err(message) => {
            eprintln!("plan-pick: {message}");
            ExitCode::from(FAILED_EXIT)
        }
    }
}

fn dispatch() -> ExitCode {
    let mut argv: Vec<String> = std::env::args().collect();
    if argv.is_empty() {
        return ExitCode::from(FAILED_EXIT);
    }
    let invoked = std::path::Path::new(&argv[0])
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("token-ladder")
        .to_string();
    if invoked == "plan-pick" {
        return plan_pick(argv.split_off(1));
    }
    if let Some(index) = argv.iter().position(|arg| arg == "--plan-pick") {
        argv.remove(index);
        return plan_pick(argv.split_off(1));
    }
    match Cli::try_parse_from(&argv) {
        Ok(cli) => match run(cli) {
            Ok(code) => code,
            Err(message) => {
                eprintln!("token-ladder: {message}");
                ExitCode::from(FAILED_EXIT)
            }
        },
        Err(error) => {
            let _ = error.print();
            ExitCode::from(FAILED_EXIT)
        }
    }
}

fn main() -> ExitCode {
    dispatch()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use token_ladder::config::LimitEntry;
    use token_ladder::config::Stampish;
    use token_ladder::config::Status;
    use token_ladder::domain::Role;
    use token_ladder::LaneSpec;

    fn lane(name: &str, pool: Option<&str>, credit: Option<&str>, chat_only: bool) -> LaneSpec {
        LaneSpec {
            lane: name.to_string(),
            pool: pool.map(str::to_string),
            credit: credit.map(str::to_string),
            command: format!("run {name}"),
            chat_only,
            ..LaneSpec::default()
        }
    }

    fn config() -> Config {
        Config {
            lanes: vec![
                lane("antigravity", Some("google"), None, true),
                lane("commandcode", None, Some("commandcode"), false),
                lane("codex", Some("codex"), None, false),
            ],
            ..Config::default()
        }
    }

    fn state(status_json: &str, credits_json: &str) -> State {
        State {
            status: Status::parse(status_json, "status.json").unwrap(),
            credits: serde_json::from_str(credits_json).unwrap(),
            limits: Limits::new(),
        }
    }

    #[test]
    fn credit_pool_turns_a_balance_into_a_pool_state() {
        let entry = token_ladder::CreditEntry {
            usd: Some(14.24),
            limit_usd: Some(110.0),
            resets_at: Some(Stampish::Text("2026-12-05T08:00:00Z".to_string())),
            ..token_ladder::CreditEntry::default()
        };
        let pool = credit_pool(&entry, &Config::default()).unwrap();
        assert_eq!(pool.state_label(), "ON");
        assert_eq!(pool.credits_usd, Some(14.24));
        assert_eq!(pool.reset_epoch(), Some(1796457600));
        assert!(pool.used_pct.unwrap() > 87.0);
        let low = token_ladder::CreditEntry {
            usd: Some(0.68),
            ..token_ladder::CreditEntry::default()
        };
        assert_eq!(
            credit_pool(&low, &Config::default()).unwrap().state_label(),
            "OFF"
        );
    }

    #[test]
    fn with_credit_pools_fills_missing_credit_lanes() {
        let mut pools = Pools::new();
        pools.insert(
            "codex".to_string(),
            Pool {
                state: Some("ON".to_string()),
                ..Pool::default()
            },
        );
        let state = state(
            r#"{"ts": 1795783606.0, "pools": {"codex": {"state": "ON", "used_pct": 56.0}}}"#,
            r#"{"vercel": {"usd": 14.24, "limit_usd": 110}}"#,
        );
        let filled = with_credit_pools(pools, &state, &Config::default());
        assert!(filled.contains_key("credit:vercel"));
        assert_eq!(filled["credit:vercel"].state_label(), "ON");
    }

    #[test]
    fn parse_pools_accepts_a_bare_pool_map_and_a_status_document() {
        let bare = parse_pools(r#"{"codex": {"state": "ON", "used_pct": 56}}"#).unwrap();
        assert_eq!(bare["codex"].state_label(), "ON");
        let document = parse_pools(
            r#"{"ts": 1795783606.0, "pools": {"claude": {"state": "OFF"}}}"#,
        )
        .unwrap();
        assert_eq!(document["claude"].state_label(), "OFF");
        let unstamped = parse_pools(r#"{"pools": {"codex": {"state": "ON"}}}"#).unwrap();
        assert_eq!(unstamped.len(), 1);
        assert_eq!(unstamped["codex"].state_label(), "ON");
        assert!(!unstamped.contains_key("pools"));
        assert!(parse_pools("nope").is_err());
    }

    #[test]
    fn pools_report_marks_google_chat_only_and_reads_missing_pools() {
        let pools = Pools::from([
            (
                "google".to_string(),
                Pool {
                    state: Some("ON".to_string()),
                    used_pct: Some(49.0),
                    chat_only: true,
                    ..Pool::default()
                },
            ),
            (
                "codex".to_string(),
                Pool {
                    state: Some("ON".to_string()),
                    used_pct: Some(56.0),
                    resets_at: Some(Stampish::Epoch(1795988288.0)),
                    ..Pool::default()
                },
            ),
        ]);
        let report = PoolsReport {
            claude: PoolReport::from(pools.get("claude"), false),
            codex: PoolReport::from(pools.get("codex"), false),
            grok: PoolReport::from(pools.get("grok"), false),
            google: PoolReport::from(pools.get("google"), true),
        };
        assert_eq!(report.claude.state, "unknown");
        assert_eq!(report.claude.used_pct, None);
        assert!(report.codex.line("Codex").starts_with("Codex: 56% used, reset 2026-11-29"));
        assert!(report.google.line("Google").contains("(chat-only)"));
        let json = serde_json::to_value(&report).unwrap();
        assert_eq!(json["claude"]["state"], "unknown");
        assert_eq!(json["codex"]["resets_at"], 1795988288);
        assert_eq!(json["google"]["chat_only"], true);
    }

    #[test]
    fn rung_for_prefers_the_flag_then_the_brief() {
        let dir = std::env::temp_dir().join(format!("token-ladder-rust-main-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let brief = dir.join("brief.txt");
        std::fs::write(&brief, "Ticket: VSTA-342\nRung: hard\n").unwrap();
        let args = PickArgs {
            json: false,
            brief: brief.clone(),
            rung: None,
            plans: false,
            lane_ok: None,
            not_vendor: vec![],
            loop_interval: false,
            claude_helper: false,
            run: false,
            limited: None,
            set_credit: None,
            credit_at: None,
            credit_source: None,
            hours: None,
            reason: None,
            pools: None,
            config: None,
        };
        assert_eq!(rung_for(&args).unwrap().rung, Rung::Hard);
        let flagged = PickArgs {
            rung: Some("light".to_string()),
            ..args
        };
        assert_eq!(rung_for(&flagged).unwrap().rung, Rung::Light);
        let broken = PickArgs {
            rung: Some("turbo".to_string()),
            brief,
            ..flagged
        };
        assert!(rung_for(&broken).is_err());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn missing_brief_defaults_to_medium() {
        let args = PickArgs {
            json: false,
            brief: PathBuf::from("/nonexistent/brief.txt"),
            rung: None,
            plans: false,
            lane_ok: None,
            not_vendor: vec![],
            loop_interval: false,
            claude_helper: false,
            run: false,
            limited: None,
            set_credit: None,
            credit_at: None,
            credit_source: None,
            hours: None,
            reason: None,
            pools: None,
            config: None,
        };
        let rung = rung_for(&args).unwrap();
        assert_eq!(rung.rung, Rung::Medium);
        assert_eq!(rung.source, "default");
    }

    #[test]
    fn pick_command_reaches_the_picker_without_touching_the_network() {
        let config = config();
        let pools = Pools::from([
            (
                "google".to_string(),
                Pool {
                    state: Some("ON".to_string()),
                    chat_only: true,
                    ..Pool::default()
                },
            ),
            (
                "credit:commandcode".to_string(),
                Pool {
                    state: Some("ON".to_string()),
                    pace_ratio: Some(0.86),
                    ..Pool::default()
                },
            ),
            (
                "codex".to_string(),
                Pool {
                    state: Some("ON".to_string()),
                    pace_ratio: Some(0.5),
                    ..Pool::default()
                },
            ),
        ]);
        let credits = Credits::new();
        let limits = Limits::new();
        let ctx = lane_context(&config, &pools, &credits, &limits, 1_795_783_606);
        let rung = RungContext::fixed(Rung::Medium);
        let result = picker::pick(&PickContext { lane: ctx, excluded: vec![] }, &rung);
        assert_eq!(result.plan, "commandcode");
        let role = config.lane("antigravity").unwrap().role();
        assert_eq!(role, Role::Orchestrator);
    }

    #[test]
    fn limit_entries_round_trip_through_the_limits_type() {
        let limits: Limits = serde_json::from_str(
            r#"{"opencode": {"until": 1796079670.0, "reason": "credit floor reached"}}"#,
        )
        .unwrap();
        let entry: &LimitEntry = &limits["opencode"];
        assert_eq!(entry.epoch(), Some(1796079670));
        assert_eq!(entry.reason().as_deref(), Some("credit floor reached"));
    }

    #[test]
    fn env_and_command_refusal_agree_on_a_nested_shell() {
        let env = hooks::Env::new();
        let input = serde_json::json!({"tool_input": {"command": "sh -c -e -- 'agy -p edit'"}});
        assert_eq!(
            hooks::command_refusal(&input, &env),
            Some(hooks::AGY_REFUSAL.to_string())
        );
    }

    #[test]
    fn state_paths_honour_the_override_directory() {
        let paths = StatePaths::under("/tmp/token-ladder-state");
        assert_eq!(
            paths.status().display().to_string(),
            "/tmp/token-ladder-state/status.json"
        );
        assert_eq!(
            paths.credits().display().to_string(),
            "/tmp/token-ladder-state/lane-credits.json"
        );
    }
}
