use crate::config::{
    home_dir, iso_stamp, parse_stamp, Config, Credits, LaneSpec, Limits, Pools, StatePaths,
};
use crate::domain::{Rung, RungContext, RUNGS};
use crate::picker::{self, PickContext};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::ffi::OsString;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub const NO_LANE_EXIT: u8 = 75;
pub const FAILED_EXIT: u8 = 1;
pub const USAGE_EXIT: u8 = 2;
pub const DEFAULT_BRIEF: &str = "brief.txt";
pub const WORKER_SHIMS: &str = "bin/worker-shims";
pub const VSTACK_SHIMS: &str = ".local/state/vstack/worker-shims";
pub const AUTHOR_BRIEF_LINE: &str = "Your PR body must contain exactly: Author model: {}";
pub const WORKER_MARKER: &str = "PLAN_PICK_WORKER";
pub const AUTHOR_MARKER: &str = "PLAN_PICK_AUTHOR_MODEL";
pub const CALLER_MARKER: &str = "MULTIBOT_SESSION";
pub const REMOVED_PREFIXES: [&str; 3] = ["MULTIPROMPT_", "MULTIBOT_", "HYPERTASK_"];
pub const REMOVED_NAMES: [&str; 2] = ["HT_TOKEN", AUTHOR_MARKER];
pub const ERROR_CHARS: usize = 4000;
pub const SUMMARY_CHARS: usize = 300;

#[derive(Debug, thiserror::Error)]
pub enum RunnerError {
    #[error("{0}")]
    Message(String),
    #[error("cannot read {path}: {source}")]
    Read {
        path: String,
        #[source]
        source: std::io::Error,
    },
    #[error("cannot write {path}: {source}")]
    Write {
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
    #[error("cannot run the worker: {0}")]
    Spawn(#[source] std::io::Error),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LimitKind {
    Credit,
    Rate,
    Silent,
}

impl LimitKind {
    pub fn why(self) -> &'static str {
        match self {
            LimitKind::Credit => "is out of credit",
            LimitKind::Rate => "hit a rate or usage limit",
            LimitKind::Silent => "exited with no output",
        }
    }

    pub fn source(self) -> &'static str {
        match self {
            LimitKind::Credit => "quota error",
            LimitKind::Rate => "429",
            LimitKind::Silent => "silent exit",
        }
    }

    pub fn hours(self, cfg: &Config) -> f64 {
        match self {
            LimitKind::Credit => cfg.credit_limited_hours,
            LimitKind::Rate | LimitKind::Silent => cfg.limited_hours,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Finished,
    Limited,
    Environment,
    Work,
    None,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Finished => "finished",
            Outcome::Limited => "limited",
            Outcome::Environment => "environment",
            Outcome::Work => "work",
            Outcome::None => "none",
        }
    }
}

impl std::fmt::Display for Outcome {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct AttemptRecord {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pr_url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub failure: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rung: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub finished_at: Option<f64>,
}

impl AttemptRecord {
    pub fn climbed_before(&self) -> Option<Rung> {
        let rung = self.rung.as_deref()?.parse::<Rung>().ok()?;
        Some(rung)
    }

    pub fn is_work_failure(&self) -> bool {
        self.failure.as_deref() == Some("work")
    }
}

pub struct RunRequest<'a> {
    pub cfg: &'a Config,
    pub pools: &'a Pools,
    pub credits: &'a Credits,
    pub paths: &'a StatePaths,
    pub brief: PathBuf,
    pub excluded: Vec<String>,
    pub now: f64,
}

#[derive(Debug, Clone)]
pub struct Attempt {
    pub code: i32,
    pub output: String,
    pub seconds: f64,
}

pub fn run(request: &RunRequest<'_>) -> Result<u8, RunnerError> {
    let brief_text = std::fs::read_to_string(&request.brief).unwrap_or_default();
    let job = job_key(&brief_text);
    let attempts = read_attempts(&request.paths.rung_attempts())?;
    let rung_ctx = resolve_rung(&brief_text, &attempts, &job)?;
    let limits_path = request.paths.limits();
    let launches_path = request.paths.launches();
    let attempts_path = request.paths.rung_attempts();
    let caller = caller_name();
    let started_at = request.now;
    let mut now = started_at;
    let mut tried: Vec<String> = Vec::new();
    loop {
        if !tried.is_empty() {
            now = crate::usage::now_epoch();
        }
        let lanes: Vec<LaneSpec> = request
            .cfg
            .lanes
            .iter()
            .filter(|lane| !tried.iter().any(|name| name == &lane.lane))
            .cloned()
            .collect();
        let cfg = Config {
            lanes,
            ..request.cfg.clone()
        };
        let limits: Limits = read_limits(&limits_path)?;
        let stamp = now.max(0.0) as i64;
        let ctx = PickContext::new(&cfg, request.pools, request.credits, &limits, stamp)
            .excluding(request.excluded.clone());
        let result = picker::pick(&ctx, &rung_ctx);
        if !result.picked() {
            let row = launch_row(
                now,
                None,
                &rung_ctx,
                &caller,
                Outcome::None,
                None,
                None,
                &result.reason,
            );
            append_launch(&launches_path, &row)?;
            record_no_lane(&attempts_path, &job, now, &rung_ctx)?;
            eprintln!("plan-pick: no lane left, nothing ran. {}", result.reason);
            return Ok(NO_LANE_EXIT);
        }
        let lane_name = result.plan.clone();
        let command = result.command.clone().unwrap_or_default();
        let model = author_model(&command);
        let (command, brief_copy) = lane_brief(&command, &request.brief, model.as_deref());
        if model.is_none() {
            eprintln!(
                "plan-pick: lane {lane_name} gets no Author model line (no readable --model or unknown family)"
            );
        }
        let shims = worker_shims();
        let attempt = run_worker(&command, &shims);
        drop_brief(&brief_copy);
        let attempt = attempt?;
        let empty = attempt.output.trim().is_empty();
        let pr_url = last_pr_url(&attempt.output);
        if let Some(refused) = shim_refusal(&attempt.output).filter(|_| pr_url.is_none()) {
            let row = launch_row(
                now,
                Some(&lane_name),
                &rung_ctx,
                &caller,
                Outcome::Environment,
                Some(attempt.code),
                None,
                &refused,
            );
            append_launch(&launches_path, &row)?;
            record_attempt(
                &attempts_path,
                &job,
                AttemptRecord {
                    event: Some("finish".to_string()),
                    exit_code: Some(attempt.code),
                    pr_url: None,
                    failure: Some("environment".to_string()),
                    rung: Some(rung_ctx.rung.as_str().to_string()),
                    finished_at: Some(finished_at(started_at, attempt.seconds)),
                },
            )?;
            eprintln!(
                "plan-pick: lane {lane_name} stopped on a worker-shim refusal ({refused}); recorded as environment, no rung climb"
            );
            return Ok(FAILED_EXIT);
        }
        if let Some(kind) = limit_kind(attempt.code, &attempt.output, empty) {
            let hours = kind.hours(request.cfg);
            let why = kind.why();
            let reason = format!("lane {lane_name} {why}");
            let until = mark_limited(
                &limits_path,
                &lane_name,
                hours,
                now,
                why,
                kind.source(),
                &attempt.output,
            )?;
            let row = launch_row(
                now,
                Some(&lane_name),
                &rung_ctx,
                &caller,
                Outcome::Limited,
                Some(attempt.code),
                None,
                why,
            );
            append_launch(&launches_path, &row)?;
            record_attempt(
                &attempts_path,
                &job,
                AttemptRecord {
                    event: Some("finish".to_string()),
                    exit_code: Some(attempt.code),
                    pr_url: None,
                    failure: Some(if kind == LimitKind::Silent {
                        "silent".to_string()
                    } else {
                        "lane".to_string()
                    }),
                    rung: Some(rung_ctx.rung.as_str().to_string()),
                    finished_at: Some(finished_at(started_at, attempt.seconds)),
                },
            )?;
            eprintln!(
                "plan-pick: {reason} (exit {}); skipped until {}, trying the next lane",
                attempt.code,
                until_text(until, now)
            );
            tried.push(lane_name);
            continue;
        }
        let success = attempt.code == 0;
        let failure = if success && pr_url.is_some() {
            None
        } else {
            Some("work".to_string())
        };
        let row = launch_row(
            now,
            Some(&lane_name),
            &rung_ctx,
            &caller,
            if success { Outcome::Finished } else { Outcome::Work },
            Some(attempt.code),
            pr_url.as_deref(),
            last_words(&attempt.output).as_str(),
        );
        append_launch(&launches_path, &row)?;
        record_attempt(
            &attempts_path,
            &job,
            AttemptRecord {
                event: Some("finish".to_string()),
                exit_code: Some(attempt.code),
                pr_url: pr_url.clone(),
                failure,
                rung: Some(rung_ctx.rung.as_str().to_string()),
                finished_at: Some(finished_at(started_at, attempt.seconds)),
            },
        )?;
        println!(
            "plan-pick: {}",
            attempt_line(&lane_name, &attempt, pr_url.as_deref(), empty)
        );
        if success {
            return Ok(0);
        }
        return Ok(exit_code(attempt.code));
    }
}

fn finished_at(now: f64, seconds: f64) -> f64 {
    now + seconds
}

pub fn exit_code(code: i32) -> u8 {
    if code == 0 {
        0
    } else {
        u8::try_from(code.clamp(1, 255)).unwrap_or(FAILED_EXIT)
    }
}

pub fn limit_kind(code: i32, output: &str, empty: bool) -> Option<LimitKind> {
    if empty {
        return Some(LimitKind::Silent);
    }
    if code == 0 {
        return None;
    }
    let text = output.to_ascii_lowercase();
    if credit_text(&text) {
        return Some(LimitKind::Credit);
    }
    if rate_text(&text) {
        return Some(LimitKind::Rate);
    }
    None
}

fn credit_text(text: &str) -> bool {
    [
        "402",
        "payment required",
        "insufficient balance",
        "insufficient credit",
        "insufficient funds",
        "insufficient quota",
        "out of credit",
        "credit balance",
        "exceeded your current quota",
        "no credit left",
        "no credits left",
        "no credit remaining",
        "no credits remaining",
    ]
    .iter()
    .any(|needle| text.contains(needle))
}

fn rate_text(text: &str) -> bool {
    [
        "429",
        "too many requests",
        "rate limit",
        "ratelimit",
        "rate-limit",
        "usage limit",
        "quota",
        "resource exhausted",
        "resource_exhausted",
        "limit reached",
        "limit exceeded",
        "hit your limit",
    ]
    .iter()
    .any(|needle| text.contains(needle))
}

pub fn shim_refusal(output: &str) -> Option<String> {
    for line in output.lines() {
        let trimmed = line.trim();
        for marker in [
            "plan-pick worker:",
            "worker shim refused:",
            "BLOCKED by heavy-local-guard:",
        ] {
            if let Some(rest) = trimmed.strip_prefix(marker) {
                let said = first_sentence(rest);
                if !said.is_empty() {
                    return Some(said);
                }
            }
        }
        if let Some(name) = trimmed.strip_suffix("is not installed on this host") {
            let said = first_sentence(name);
            if !said.is_empty() {
                return Some(format!("{said} is not installed on this host"));
            }
        }
    }
    None
}

fn first_sentence(text: &str) -> String {
    let said = text.split('.').next().unwrap_or_default();
    said.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub fn last_words(output: &str) -> String {
    let said = output
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .map(|line| line.split_whitespace().collect::<Vec<_>>().join(" "))
        .unwrap_or_default();
    truncate(&said, SUMMARY_CHARS)
}

fn truncate(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_string();
    }
    text.chars().take(limit).collect()
}

pub fn diagnostic(output: &str) -> String {
    let text = output.trim();
    let tail = if text.chars().count() > ERROR_CHARS {
        let start = text.chars().count() - ERROR_CHARS;
        text.chars().skip(start).collect::<String>()
    } else {
        text.to_string()
    };
    last_words(&tail)
}

pub fn attempt_line(lane: &str, attempt: &Attempt, pr_url: Option<&str>, empty: bool) -> String {
    if let Some(pr_url) = pr_url {
        return format!(
            "lane {lane} finished after {:.0}s: {pr_url}",
            attempt.seconds
        );
    }
    let what = if empty {
        "with no output at all"
    } else {
        "with no pull request"
    };
    let words = last_words(&attempt.output);
    let base = format!(
        "lane {lane} exited {} after {:.0}s {what}",
        attempt.code, attempt.seconds
    );
    if words.is_empty() {
        base
    } else {
        format!("{base}; last output: {words}")
    }
}

pub fn pr_urls(output: &str) -> Vec<String> {
    let mut found = Vec::new();
    let mut rest = output;
    const PREFIX: &str = "https://github.com/";
    while let Some(index) = rest.find(PREFIX) {
        let tail = &rest[index..];
        let end = tail
            .find(|c: char| {
                !(c.is_ascii_alphanumeric() || matches!(c, ':' | '/' | '-' | '.' | '_'))
            })
            .unwrap_or(tail.len());
        let url = &tail[..end];
        if is_pr_url(url) {
            found.push(url.to_string());
        }
        rest = &tail[end..];
    }
    found
}

fn is_pr_url(url: &str) -> bool {
    let parts: Vec<&str> = url.split('/').collect();
    parts.len() == 7
        && parts[5] == "pull"
        && !parts[6].is_empty()
        && parts[6].chars().all(|c| c.is_ascii_digit())
}

pub fn last_pr_url(output: &str) -> Option<String> {
    pr_urls(output).pop()
}

pub fn author_model(command: &str) -> Option<String> {
    let mut found: Option<&str> = None;
    let mut rest = command;
    while let Some(index) = rest.find("--model") {
        let tail = &rest[index..];
        let quoted = tail.starts_with("--model=");
        let after = if quoted { &tail[8..] } else { &tail[7..] };
        if !quoted && !after.starts_with(char::is_whitespace) {
            rest = after;
            continue;
        }
        let value = after
            .trim_start()
            .split(|c: char| !(c.is_ascii_alphanumeric() || matches!(c, '.' | '/' | ':' | '+' | '-' | '_')))
            .next()
            .unwrap_or_default();
        if !value.is_empty() {
            found = Some(value);
        }
        rest = after;
    }
    let model = found?.rsplit('/').next().unwrap_or_default().to_string();
    let family = model_family(&model)?;
    Some(format!("{family}/{model}"))
}

pub fn model_family(model: &str) -> Option<&'static str> {
    let name = model.trim().to_ascii_lowercase();
    let part = name.rsplit('/').next().unwrap_or_default();
    let families: [(&'static str, &[&str]); 7] = [
        ("deepseek", &["deepseek"]),
        (
            "openai",
            &["openai", "gpt", "chatgpt", "codex", "o1", "o3", "o4"],
        ),
        ("moonshotai", &["moonshotai", "moonshot", "kimi"]),
        ("zai", &["zai", "z-ai", "z.ai", "glm"]),
        ("google", &["google", "gemini"]),
        ("anthropic", &["anthropic", "claude"]),
        ("cursor", &["cursor", "composer"]),
    ];
    for (family, prefixes) in families {
        for prefix in prefixes {
            for candidate in [part, name.as_str()] {
                if let Some(rest) = candidate.strip_prefix(prefix) {
                    let boundary = rest.is_empty()
                        || rest.starts_with('-')
                        || rest.starts_with('.')
                        || rest.starts_with('_')
                        || rest.starts_with(':');
                    if boundary {
                        return Some(family);
                    }
                }
            }
        }
    }
    None
}

pub fn lane_brief(command: &str, brief: &Path, value: Option<&str>) -> (String, Option<PathBuf>) {
    let Some(value) = value.filter(|value| !value.is_empty()) else {
        return (command.to_string(), None);
    };
    let command = format!("export {AUTHOR_MARKER}={value}; {command}");
    let name = brief
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    if name.is_empty() || !command.contains(&name) {
        return (command, None);
    }
    let text = std::fs::read_to_string(brief).unwrap_or_default();
    let path = temp_brief(&name);
    let body = format!(
        "{}\n\n{}\n",
        text.trim_end_matches('\n'),
        AUTHOR_BRIEF_LINE.replace("{}", value)
    );
    if std::fs::write(&path, body).is_err() {
        eprintln!(
            "plan-pick: cannot add the Author model line to the brief: {}",
            path.display()
        );
        return (command, None);
    }
    (command.replace(&name, &path.display().to_string()), Some(path))
}

fn temp_brief(name: &str) -> PathBuf {
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or_default();
    std::env::temp_dir().join(format!("{name}.author-{}-{stamp}.txt", std::process::id()))
}

fn drop_brief(path: &Option<PathBuf>) {
    if let Some(path) = path {
        let _ = std::fs::remove_file(path);
    }
}

pub fn repo_root() -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(root) = std::env::var_os("TOKEN_LADDER_ROOT") {
        candidates.push(PathBuf::from(root));
    }
    candidates.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(".."));
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd);
    }
    candidates
        .into_iter()
        .map(|path| path.canonicalize().unwrap_or(path))
        .find(|path| path.join(WORKER_SHIMS).is_dir())
}

pub fn worker_shims() -> Vec<PathBuf> {
    let mut shims: Vec<PathBuf> = Vec::new();
    if let Some(root) = repo_root() {
        shims.push(root.join(WORKER_SHIMS));
    }
    let vstack = home_dir().join(VSTACK_SHIMS);
    if vstack.is_dir() {
        shims.push(vstack);
    }
    shims.into_iter().filter(|path| path.is_dir()).collect()
}

fn worker_path(shims: &[PathBuf]) -> OsString {
    let mut parts: Vec<OsString> = shims.iter().map(|path| path.clone().into()).collect();
    if let Some(path) = std::env::var_os("PATH") {
        parts.extend(std::env::split_paths(&path).map(PathBuf::into_os_string));
    }
    std::env::join_paths(parts).unwrap_or_default()
}

fn worker_command(command: &str, shims: &[PathBuf]) -> Command {
    let mut worker = Command::new("bash");
    worker
        .arg("-c")
        .arg(format!("{{ {command}; }} 2>&1"))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    for name in REMOVED_NAMES {
        worker.env_remove(name);
    }
    for (name, _) in std::env::vars_os() {
        let name = name.to_string_lossy().to_string();
        if REMOVED_PREFIXES
            .iter()
            .any(|prefix| name.starts_with(prefix))
        {
            worker.env_remove(&name);
        }
    }
    worker.env("PATH", worker_path(shims));
    worker.env(WORKER_MARKER, "1");
    worker
}

fn run_worker(command: &str, shims: &[PathBuf]) -> Result<Attempt, RunnerError> {
    let started = std::time::Instant::now();
    let mut child = worker_command(command, shims)
        .spawn()
        .map_err(RunnerError::Spawn)?;
    let mut output = String::new();
    if let Some(stdout) = child.stdout.take() {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => break,
                Ok(_) => {
                    print!("{line}");
                    let _ = std::io::stdout().flush();
                    output.push_str(&line);
                }
                Err(_) => break,
            }
        }
    }
    let status = child.wait().map_err(RunnerError::Spawn)?;
    Ok(Attempt {
        code: status.code().unwrap_or(FAILED_EXIT.into()),
        output,
        seconds: started.elapsed().as_secs_f64(),
    })
}

pub fn caller_name() -> String {
    match std::env::var(CALLER_MARKER).ok().filter(|name| !name.is_empty()) {
        Some(name) => name,
        None => std::env::current_dir()
            .map(|path| path.display().to_string())
            .unwrap_or_else(|_| "unknown".to_string()),
    }
}

#[allow(clippy::too_many_arguments)]
fn launch_row(
    at: f64,
    lane: Option<&str>,
    rung: &RungContext,
    caller: &str,
    outcome: Outcome,
    exit: Option<i32>,
    pr_url: Option<&str>,
    reason: &str,
) -> Value {
    let cwd = std::env::current_dir()
        .map(|path| path.display().to_string())
        .unwrap_or_default();
    serde_json::json!({
        "at": at,
        "lane": lane,
        "rung": rung.rung.as_str(),
        "rung_source": rung.source,
        "rung_reason": rung.reason,
        "caller": caller,
        "cwd": cwd,
        "outcome": outcome.as_str(),
        "exit_code": exit,
        "pr_url": pr_url,
        "reason": reason,
    })
}

pub fn append_launch(path: &Path, row: &Value) -> Result<(), RunnerError> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|source| RunnerError::Write {
            path: parent.display().to_string(),
            source,
        })?;
    }
    let text = serde_json::to_string(row).map_err(|source| RunnerError::Parse {
        path: path.display().to_string(),
        source,
    })?;
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|source| RunnerError::Write {
            path: path.display().to_string(),
            source,
        })?;
    file.write_all(format!("{text}\n").as_bytes())
        .map_err(|source| RunnerError::Write {
            path: path.display().to_string(),
            source,
        })
}

fn read_object(path: &Path) -> Result<Map<String, Value>, RunnerError> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Map::new()),
        Err(source) => {
            return Err(RunnerError::Read {
                path: path.display().to_string(),
                source,
            })
        }
    };
    if text.trim().is_empty() {
        return Ok(Map::new());
    }
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(object)) => Ok(object),
        Ok(_) => Ok(Map::new()),
        Err(_) => Ok(Map::new()),
    }
}

fn write_object(path: &Path, object: &Map<String, Value>) -> Result<(), RunnerError> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|source| RunnerError::Write {
            path: parent.display().to_string(),
            source,
        })?;
    }
    let text = serde_json::to_string(&Value::Object(object.clone())).map_err(|source| {
        RunnerError::Parse {
            path: path.display().to_string(),
            source,
        }
    })?;
    let temp = PathBuf::from(format!("{}.tmp", path.display()));
    std::fs::write(&temp, text).map_err(|source| RunnerError::Write {
        path: temp.display().to_string(),
        source,
    })?;
    std::fs::rename(&temp, path).map_err(|source| RunnerError::Write {
        path: path.display().to_string(),
        source,
    })
}

pub fn read_attempts(path: &Path) -> Result<Map<String, Value>, RunnerError> {
    read_object(path)
}

fn read_limits(path: &Path) -> Result<Limits, RunnerError> {
    let object = read_object(path)?;
    Ok(object
        .into_iter()
        .filter_map(|(name, value)| {
            serde_json::from_value::<crate::config::LimitEntry>(value)
                .ok()
                .map(|entry| (name, entry))
        })
        .collect())
}

pub fn record_attempt(
    path: &Path,
    job: &str,
    record: AttemptRecord,
) -> Result<(), RunnerError> {
    let mut object = read_object(path)?;
    let value = serde_json::to_value(&record).map_err(|source| RunnerError::Parse {
        path: path.display().to_string(),
        source,
    })?;
    object.insert(job.to_string(), value);
    write_object(path, &object)
}

fn record_no_lane(
    path: &Path,
    job: &str,
    now: f64,
    rung: &RungContext,
) -> Result<(), RunnerError> {
    let object = read_object(path)?;
    if let Some(prior) = object.get(job).and_then(|value| value.as_object()) {
        if prior.get("failure").and_then(Value::as_str) == Some("work") {
            return Ok(());
        }
    }
    record_attempt(
        path,
        job,
        AttemptRecord {
            event: Some("finish".to_string()),
            exit_code: Some(NO_LANE_EXIT.into()),
            pr_url: None,
            failure: Some("lane".to_string()),
            rung: Some(rung.rung.as_str().to_string()),
            finished_at: Some(now),
        },
    )
}

pub fn mark_limited(
    path: &Path,
    lane: &str,
    hours: f64,
    now: f64,
    reason: &str,
    source: &str,
    error_line: &str,
) -> Result<f64, RunnerError> {
    let until = now + hours.max(0.0) * 3600.0;
    let mut object = read_object(path)?;
    object.remove(&format!("{lane}:reason"));
    object.remove(&format!("{lane}:floor"));
    object.insert(
        lane.to_string(),
        serde_json::json!({
            "until": until,
            "reason": reason.trim(),
            "source": source,
            "error_line": diagnostic(error_line),
        }),
    );
    write_object(path, &object)?;
    Ok(until)
}

pub fn set_credit(
    path: &Path,
    lane: &str,
    usd: f64,
    at: f64,
    source: &str,
) -> Result<(), RunnerError> {
    let mut object = read_object(path)?;
    object.insert(
        lane.to_string(),
        serde_json::json!({
            "usd": usd,
            "at": at,
            "source": source.trim(),
        }),
    );
    write_object(path, &object)
}

pub fn until_text(until: f64, now: f64) -> String {
    let stamp = iso_stamp(until as i64);
    if (until as i64) / 86_400 == (now as i64) / 86_400 {
        return format!("{} UTC", stamp.get(11..16).unwrap_or(&stamp));
    }
    format!("{} UTC", stamp.get(..16).unwrap_or(&stamp).replace('T', " "))
}

pub fn resolve_rung(
    brief_text: &str,
    attempts: &Map<String, Value>,
    job: &str,
) -> Result<RungContext, RunnerError> {
    let base = RungContext::from_brief(brief_text, Rung::Medium)
        .map_err(|error| RunnerError::Message(error.to_string()))?;
    let prior = attempts
        .get(job)
        .and_then(|value| serde_json::from_value::<AttemptRecord>(value.clone()).ok())
        .unwrap_or_default();
    if !prior.is_work_failure() {
        return Ok(base);
    }
    let prior_rung = prior.climbed_before().unwrap_or(base.rung);
    let next = |rung: Rung| {
        let index = RUNGS.iter().position(|item| *item == rung).unwrap_or_default();
        RUNGS[(index + 1).min(RUNGS.len() - 1)]
    };
    let climbed = next(prior_rung);
    let chosen = if climbed > base.rung { climbed } else { base.rung };
    Ok(RungContext {
        rung: chosen,
        source: "climb".to_string(),
        reason: format!("rung {prior_rung} -> {chosen} (prior run failed)"),
    })
}

pub fn job_key(brief_text: &str) -> String {
    match ticket_line(brief_text) {
        Some(ticket) => format!("ticket:{ticket}"),
        None => format!("brief:{}", hex(&sha256(brief_text.as_bytes()))),
    }
}

fn ticket_line(text: &str) -> Option<String> {
    text.lines().find_map(|line| {
        let rest = line.strip_prefix("Ticket:")?;
        let ticket = rest.split_whitespace().next().unwrap_or_default();
        Some(ticket.to_string())
    })
}

fn hex(bytes: &[u8]) -> String {
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        text.push_str(&format!("{byte:02x}"));
    }
    text
}

const SHA256_K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

const SHA256_H: [u32; 8] = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
];

pub fn sha256(data: &[u8]) -> [u8; 32] {
    let mut state = SHA256_H;
    let mut message = data.to_vec();
    let bits = (data.len() as u64).wrapping_mul(8);
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bits.to_be_bytes());
    for chunk in message.chunks(64) {
        let mut schedule = [0u32; 64];
        for (index, word) in chunk.chunks(4).enumerate() {
            schedule[index] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for index in 16..64 {
            let a = schedule[index - 15];
            let b = schedule[index - 2];
            let s0 = a.rotate_right(7) ^ a.rotate_right(18) ^ (a >> 3);
            let s1 = b.rotate_right(17) ^ b.rotate_right(19) ^ (b >> 10);
            schedule[index] = schedule[index - 16]
                .wrapping_add(s0)
                .wrapping_add(schedule[index - 7])
                .wrapping_add(s1);
        }
        let mut a = state;
        for index in 0..64 {
            let s1 = a[4].rotate_right(6) ^ a[4].rotate_right(11) ^ a[4].rotate_right(25);
            let ch = (a[4] & a[5]) ^ (!a[4] & a[6]);
            let t1 = a[7]
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(SHA256_K[index])
                .wrapping_add(schedule[index]);
            let s0 = a[0].rotate_right(2) ^ a[0].rotate_right(13) ^ a[0].rotate_right(22);
            let maj = (a[0] & a[1]) ^ (a[0] & a[2]) ^ (a[1] & a[2]);
            let t2 = s0.wrapping_add(maj);
            a[7] = a[6];
            a[6] = a[5];
            a[5] = a[4];
            a[4] = a[3].wrapping_add(t1);
            a[3] = a[2];
            a[2] = a[1];
            a[1] = a[0];
            a[0] = t1.wrapping_add(t2);
        }
        for (slot, value) in state.iter_mut().zip(a.iter()) {
            *slot = (*slot).wrapping_add(*value);
        }
    }
    let mut out = [0u8; 32];
    for (index, word) in state.iter().enumerate() {
        out[index * 4..index * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

pub fn credit_names(cfg: &Config) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for lane in &cfg.lanes {
        for name in [Some(&lane.lane), lane.pool.as_ref(), lane.credit.as_ref()]
            .into_iter()
            .flatten()
        {
            if !names.contains(name) {
                names.push(name.clone());
            }
        }
    }
    names
}

pub fn stamp_or_now(text: Option<&str>, now: f64) -> Result<f64, RunnerError> {
    let Some(text) = text else {
        return Ok(now);
    };
    let trimmed = text.trim();
    let stamp = parse_stamp(trimmed)
        .or_else(|| parse_stamp(&with_seconds(trimmed)))
        .ok_or_else(|| RunnerError::Message(format!("cannot read the time {text}")))?;
    Ok(stamp as f64)
}

fn with_seconds(text: &str) -> String {
    let Some(split) = text.find('T') else {
        return text.to_string();
    };
    let rest = &text[split + 1..];
    let zone = rest
        .find(['Z', '+', '-'])
        .unwrap_or(rest.len());
    let clock = &rest[..zone];
    if clock.matches(':').count() == 1 {
        format!("{}T{clock}:00{}", &text[..split], &rest[zone..])
    } else {
        text.to_string()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn sha256_matches_the_reference_digests() {
        assert_eq!(
            hex(&sha256(b"")),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            hex(&sha256(b"abc")),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn job_key_prefers_the_ticket_line_then_the_brief_digest() {
        assert_eq!(job_key("Ticket: VSTA-343 (link)\n"), "ticket:VSTA-343");
        assert!(job_key("Rung: medium\n").starts_with("brief:"));
        assert_ne!(job_key("Rung: light\n"), job_key("Rung: hard\n"));
    }

    #[test]
    fn author_model_names_the_family_of_each_lane() {
        assert_eq!(
            author_model("pi --provider opencode-go --model deepseek-v4.1-flash").as_deref(),
            Some("deepseek/deepseek-v4.1-flash")
        );
        assert_eq!(
            author_model("pi --provider vercel --model zai/glm-5.3-flash").as_deref(),
            Some("zai/glm-5.3-flash")
        );
        assert_eq!(
            author_model("hax --model=gpt-6.1-sol --effort=high").as_deref(),
            Some("openai/gpt-6.1-sol")
        );
        assert_eq!(
            author_model("cursor-agent --model composer-2.5").as_deref(),
            Some("cursor/composer-2.5")
        );
        assert_eq!(
            author_model("agy --model gemini-4-neon-high").as_deref(),
            Some("google/gemini-4-neon-high")
        );
        assert_eq!(author_model("bash run-nothing"), None);
        assert_eq!(author_model("pi --model mystery-9"), None);
    }

    #[test]
    fn limit_kind_separates_credit_rate_and_silence() {
        assert_eq!(
            limit_kind(1, "402 payment required", false),
            Some(LimitKind::Credit)
        );
        assert_eq!(
            limit_kind(1, "429 rate limit exceeded", false),
            Some(LimitKind::Rate)
        );
        assert_eq!(
            limit_kind(1, "RESOURCE_EXHAUSTED", false),
            Some(LimitKind::Rate)
        );
        assert_eq!(limit_kind(0, "", true), Some(LimitKind::Silent));
        assert_eq!(limit_kind(1, "tests failed", false), None);
        assert_eq!(limit_kind(0, "opened a pr about quota", false), None);
        assert_eq!(LimitKind::Credit.hours(&Config::default()), 12.0);
        assert_eq!(LimitKind::Rate.hours(&Config::default()), 1.0);
    }

    #[test]
    fn shim_refusal_reads_every_refusal_shape() {
        assert_eq!(
            shim_refusal("plan-pick worker: sleep 240 refused (over 30s). more").as_deref(),
            Some("sleep 240 refused (over 30s)")
        );
        assert_eq!(
            shim_refusal("worker shim refused: gh pr checks --watch").as_deref(),
            Some("gh pr checks --watch")
        );
        assert_eq!(
            shim_refusal("BLOCKED by heavy-local-guard: agy is not a worker").as_deref(),
            Some("agy is not a worker")
        );
        assert_eq!(
            shim_refusal("cursor-agent is not installed on this host").as_deref(),
            Some("cursor-agent is not installed on this host")
        );
        assert_eq!(shim_refusal("all good"), None);
    }

    #[test]
    fn pr_urls_reads_the_last_pull_request() {
        let output = "opened https://github.com/valentinyeo/token-ladder/pull/66 and \
                      https://github.com/valentinyeo/token-ladder/pull/67 trailing";
        assert_eq!(
            last_pr_url(output).as_deref(),
            Some("https://github.com/valentinyeo/token-ladder/pull/67")
        );
        assert_eq!(last_pr_url("https://github.com/a/b/issues/3"), None);
        assert_eq!(last_pr_url("no link here"), None);
    }

    #[test]
    fn resolve_rung_climbs_only_after_a_work_failure() {
        let mut attempts = Map::new();
        let brief = "Ticket: VSTA-343\nRung: medium\n";
        assert_eq!(
            resolve_rung(brief, &attempts, "ticket:VSTA-343")
                .unwrap()
                .rung,
            Rung::Medium
        );
        attempts.insert(
            "ticket:VSTA-343".to_string(),
            serde_json::json!({"failure": "environment", "rung": "medium"}),
        );
        assert_eq!(
            resolve_rung(brief, &attempts, "ticket:VSTA-343")
                .unwrap()
                .rung,
            Rung::Medium
        );
        attempts.insert(
            "ticket:VSTA-343".to_string(),
            serde_json::json!({"failure": "silent", "rung": "light"}),
        );
        assert_eq!(
            resolve_rung(brief, &attempts, "ticket:VSTA-343")
                .unwrap()
                .rung,
            Rung::Medium
        );
        attempts.insert(
            "ticket:VSTA-343".to_string(),
            serde_json::json!({"failure": "work", "rung": "medium"}),
        );
        let climbed = resolve_rung(brief, &attempts, "ticket:VSTA-343").unwrap();
        assert_eq!(climbed.rung, Rung::Hard);
        assert_eq!(climbed.source, "climb");
        attempts.insert(
            "ticket:VSTA-343".to_string(),
            serde_json::json!({"failure": "work", "rung": "hard"}),
        );
        assert_eq!(
            resolve_rung(brief, &attempts, "ticket:VSTA-343")
                .unwrap()
                .rung,
            Rung::Hard
        );
        let light = "Ticket: VSTA-343\nRung: light\n";
        attempts.insert(
            "ticket:VSTA-343".to_string(),
            serde_json::json!({"failure": "work", "rung": "light"}),
        );
        assert_eq!(
            resolve_rung(light, &attempts, "ticket:VSTA-343")
                .unwrap()
                .rung,
            Rung::Medium
        );
    }

    #[test]
    fn lane_brief_appends_the_author_line_to_a_copy() {
        let dir = std::env::temp_dir().join(format!("token-ladder-runner-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let brief = dir.join("brief.txt");
        std::fs::write(&brief, "Ticket: VSTA-343\n").unwrap();
        let (command, copy) =
            lane_brief("cat brief.txt", &brief, Some("deepseek/deepseek-v4.1-flash"));
        let copy = copy.unwrap();
        assert!(command.starts_with("export PLAN_PICK_AUTHOR_MODEL=deepseek/deepseek-v4.1-flash; "));
        assert!(command.contains(&copy.display().to_string()));
        let text = std::fs::read_to_string(&copy).unwrap();
        assert!(text.ends_with(
            "Your PR body must contain exactly: Author model: deepseek/deepseek-v4.1-flash\n"
        ));
        drop_brief(&Some(copy.clone()));
        assert!(!copy.exists());
        let (plain, none) = lane_brief("cat brief.txt", &brief, None);
        assert_eq!(plain, "cat brief.txt");
        assert!(none.is_none());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn mark_limited_and_set_credit_write_atomically() {
        let dir = std::env::temp_dir().join(format!("token-ladder-runner-state-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let limits_path = dir.join("limits.json");
        let credits_path = dir.join("lane-credits.json");
        std::fs::write(&limits_path, r#"{"codex": 1.0, "opencode:reason": "old"}"#).unwrap();
        std::fs::write(&credits_path, r#"{"vercel": {"usd": 14.24}}"#).unwrap();
        let until = mark_limited(
            &limits_path,
            "opencode",
            1.0,
            1_795_783_606.0,
            "hit a limit",
            "429",
            "error: 429 too many requests",
        )
        .unwrap();
        assert_eq!(until, 1_795_787_206.0);
        let limits: Limits =
            serde_json::from_str(&std::fs::read_to_string(&limits_path).unwrap()).unwrap();
        assert_eq!(limits["opencode"].epoch(), Some(1_795_787_206));
        let raw: Value =
            serde_json::from_str(&std::fs::read_to_string(&limits_path).unwrap()).unwrap();
        assert_eq!(raw["opencode"]["error_line"], "error: 429 too many requests");
        assert_eq!(limits["opencode"].reason().as_deref(), Some("hit a limit"));
        assert!(!limits.contains_key("opencode:reason"));
        assert!(limits.contains_key("codex"));
        assert!(!dir.join("limits.json.tmp").exists());
        set_credit(&credits_path, "opencode", 24.64, 1_795_783_606.0, "screenshot").unwrap();
        let credits: Value =
            serde_json::from_str(&std::fs::read_to_string(&credits_path).unwrap()).unwrap();
        assert_eq!(credits["opencode"]["usd"], 24.64);
        assert_eq!(credits["opencode"]["source"], "screenshot");
        assert_eq!(credits["vercel"]["usd"], 14.24);
        assert!(!dir.join("lane-credits.json.tmp").exists());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn until_text_stays_short_on_the_same_day() {
        let now = 1_795_783_606.0;
        assert_eq!(until_text(now + 3600.0, now), "13:46 UTC");
        assert_eq!(until_text(now + 172_800.0, now), "2026-11-29 12:46 UTC");
    }

    #[test]
    fn stamp_or_now_accepts_a_time_without_seconds() {
        let short = stamp_or_now(Some("2026-11-22T22:00Z"), 1.0).unwrap();
        let long = stamp_or_now(Some("2026-11-22T22:00:00Z"), 1.0).unwrap();
        assert_eq!(short, long);
        assert_eq!(stamp_or_now(Some("2026-11-22T23:00:00+01:00"), 1.0).unwrap(), long);
        assert_eq!(stamp_or_now(None, 42.0).unwrap(), 42.0);
        assert!(stamp_or_now(Some("not a date"), 1.0).is_err());
    }

    #[test]
    fn credit_names_lists_every_name_a_credit_flag_can_use() {
        let cfg = Config {
            lanes: vec![LaneSpec {
                lane: "vercel".to_string(),
                pool: None,
                credit: Some("vercel".to_string()),
                command: "run".to_string(),
                ..LaneSpec::default()
            }],
            ..Config::default()
        };
        assert_eq!(credit_names(&cfg), vec!["vercel".to_string()]);
    }
}
