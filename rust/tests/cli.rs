use serde_json::Value;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

fn binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_token-ladder"))
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "token-ladder-cli-{name}-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join(".local/state/token-ladder")).ok();
    dir
}

fn run(home: &Path, args: &[&str], stdin: &str) -> (i32, String, String) {
    let mut child = Command::new(binary())
        .args(args)
        .env("HOME", home)
        .env_remove("TOKEN_LADDER_LOCAL_CONFIG")
        .env_remove("TOKEN_LADDER_STATE_DIR")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("binary starts");
    if let Some(pipe) = child.stdin.as_mut() {
        pipe.write_all(stdin.as_bytes()).ok();
    }
    let output = child.wait_with_output().expect("binary finishes");
    (
        output.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    )
}

fn run_as(home: &Path, name: &str, args: &[&str]) -> (i32, String, String) {
    let dir = home.join("bin");
    std::fs::create_dir_all(&dir).ok();
    let link = dir.join(name);
    let _ = std::fs::remove_file(&link);
    std::os::unix::fs::symlink(binary(), &link).expect("symlink");
    let output = Command::new(&link)
        .args(args)
        .env("HOME", home)
        .env_remove("TOKEN_LADDER_LOCAL_CONFIG")
        .env_remove("TOKEN_LADDER_STATE_DIR")
        .stdin(Stdio::null())
        .output()
        .expect("binary runs");
    (
        output.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    )
}

fn write_state(home: &Path, status: &str, credits: &str, limits: &str) {
    let dir = home.join(".local/state/token-ladder");
    std::fs::create_dir_all(&dir).ok();
    std::fs::write(dir.join("status.json"), status).ok();
    std::fs::write(dir.join("lane-credits.json"), credits).ok();
    std::fs::write(dir.join("limits.json"), limits).ok();
}

#[test]
fn hook_bash_denies_nested_antigravity_worker_and_allows_usage() {
    let home = scratch("hook-bash");
    let denied = r#"{"tool_input":{"command":"sh -c -e -- 'agy -p edit'"}}"#;
    let (code, out, _) = run(&home, &["hook-bash"], denied);
    assert_eq!(code, 0);
    let value: Value = serde_json::from_str(&out).expect("json deny payload");
    assert_eq!(
        value["hookSpecificOutput"]["permissionDecision"], "deny"
    );
    assert!(value["hookSpecificOutput"]["permissionDecisionReason"]
        .as_str()
        .unwrap_or_default()
        .contains("orchestrator-only"));
    let usage = r#"{"tool_input":{"command":"agy --print /usage"}}"#;
    let (code, out, _) = run(&home, &["hook-bash"], usage);
    assert_eq!(code, 0);
    assert!(out.trim().is_empty());
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn hook_agent_denies_fast_models_and_reports_the_off_claude_pool() {
    let home = scratch("hook-agent");
    write_state(
        &home,
        r#"{"ts": 1795785009.0, "pools": {"claude": {"state": "OFF", "used_pct": 100.0}}}"#,
        "{}",
        "{}",
    );
    let (code, out, _) = run(&home, &["hook-agent"], r#"{"tool_input":{"model":"x-fast"}}"#);
    assert_eq!(code, 0);
    let value: Value = serde_json::from_str(&out).expect("json deny payload");
    assert!(value["hookSpecificOutput"]["permissionDecisionReason"]
        .as_str()
        .unwrap_or_default()
        .contains("fast mode is off"));
    let (code, out, _) = run(&home, &["hook-agent"], r#"{"tool_input":{"model":"opus"}}"#);
    assert_eq!(code, 0);
    let value: Value = serde_json::from_str(&out).expect("json deny payload");
    assert!(value["hookSpecificOutput"]["permissionDecisionReason"]
        .as_str()
        .unwrap_or_default()
        .contains("Claude pool OFF"));
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn hook_prompt_prints_one_usage_line_tagging_antigravity_chat_only() {
    let home = scratch("hook-prompt");
    let config = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../config.json"),
    )
    .expect("repo config");
    let config_value: Value = serde_json::from_str(&config).expect("config json");
    let status = serde_json::json!({
        "ts": 1795785009.0,
        "config": config_value,
        "pools": {
            "claude": {"state": "OFF", "used_pct": 100.0},
            "codex": {"state": "ON", "used_pct": 56.0},
            "google": {"state": "ON", "used_pct": 49.0, "chat_only": true,
                       "gemini": {"weekly_remaining_pct": 51, "five_hour_remaining_pct": 97}}
        }
    });
    write_state(
        &home,
        &status.to_string(),
        r#"{"vercel": {"usd": 14.24, "limit_usd": 110}, "commandcode": {"credits": 60.708}}"#,
        "{}",
    );
    let (code, out, _) = run(&home, &["hook-prompt"], "{}");
    assert_eq!(code, 0);
    assert!(out.starts_with("Plan usage (token-ladder"), "{out}");
    assert!(out.contains("(chat-only)"), "{out}");
    assert!(out.contains("CommandCode"), "{out}");
    assert!(out.ends_with(". Source: plan-pick --plans.\n"), "{out}");
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn pools_json_reads_typed_state_without_a_network_call() {
    let home = scratch("pools");
    write_state(
        &home,
        r#"{"ts": 1795785009.0, "pools": {"claude": {"state": "OFF", "used_pct": 100.0},
             "codex": {"state": "ON", "used_pct": 56.0, "resets_at": 1795988288},
             "google": {"state": "ON", "used_pct": 49.0, "chat_only": true}}}"#,
        "{}",
        "{}",
    );
    let (code, out, _) = run(&home, &["pools", "--json"], "");
    assert_eq!(code, 0);
    let value: Value = serde_json::from_str(&out).expect("json pools");
    assert_eq!(value["claude"]["state"], "OFF");
    assert_eq!(value["codex"]["used_pct"], 56.0);
    assert_eq!(value["google"]["chat_only"], true);
    assert_eq!(value["grok"]["state"], "unknown");
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn plan_pick_name_dispatches_the_pick_surface() {
    let home = scratch("plan-pick");
    let brief = home.join("brief.txt");
    std::fs::write(&brief, "Ticket: VSTA-342\nRung: hard\n").ok();
    let (code, out, err) = run_as(
        &home,
        "plan-pick",
        &["--json", "--brief", brief.to_str().unwrap_or("brief.txt")],
    );
    assert_eq!(code, 0, "{err}");
    let value: Value = serde_json::from_str(&out).expect("json pick");
    assert_eq!(value["rung"], "hard");
    assert_eq!(value["rung_source"], "brief line");
    assert_eq!(value["rung_reason"], "rung hard");
    assert_eq!(value["plan"], "codex");
    assert!(value["reason"]
        .as_str()
        .unwrap_or_default()
        .contains("chat-only pool, never workers"));
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn plan_pick_lane_ok_refuses_orchestrator_lanes_with_exit_three() {
    let home = scratch("lane-ok");
    let brief = home.join("brief.txt");
    std::fs::write(&brief, "Rung: medium\n").ok();
    let (code, out, _) = run_as(
        &home,
        "plan-pick",
        &["--lane-ok", "antigravity", "--brief", brief.to_str().unwrap_or("brief.txt")],
    );
    assert_eq!(code, 3);
    assert_eq!(out.trim(), "antigravity: chat-only pool, never workers");
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn plan_pick_keeps_the_cloud_lane_check_flag_for_cloud_agent() {
    let home = scratch("lane-ok-cloud");
    let brief = home.join("brief.txt");
    std::fs::write(&brief, "Rung: medium\n").ok();
    let args = [
        "--lane-ok",
        "antigravity",
        "--cloud",
        "--brief",
        brief.to_str().unwrap_or("brief.txt"),
    ];
    let (code, out, err) = run_as(&home, "plan-pick", &args);
    assert_eq!(code, 3, "{err}");
    assert_eq!(out.trim(), "antigravity: chat-only pool, never workers");
    let (code, out, err) = run_as(&home, "plan-pick", &["--cloud"]);
    assert_eq!(code, 2);
    assert!(out.is_empty());
    assert!(
        err.contains("plan-pick: --cloud requires --lane-ok"),
        "{err}"
    );
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn claude_helper_and_loop_interval_answer_from_state() {
    let home = scratch("helper");
    write_state(
        &home,
        r#"{"ts": 1795785009.0, "pools": {"claude": {"state": "ON", "used_pct": 60.0, "pace_ratio": 1.05}}}"#,
        "{}",
        "{}",
    );
    let (code, out, _) = run_as(&home, "plan-pick", &["--loop-interval"]);
    assert_eq!(code, 0);
    assert_eq!(out.trim(), "10800 claude pace 1.05x ahead");
    let (code, out, _) = run_as(&home, "plan-pick", &["--claude-helper"]);
    assert_eq!(code, 3);
    assert!(out.contains("pace 1.05x"), "{out}");
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn plans_lists_every_plan_from_the_repo_config() {
    let home = scratch("plans");
    let (code, out, _) = run_as(&home, "plan-pick", &["--plans"]);
    assert_eq!(code, 0);
    assert!(out.contains("Antigravity"), "{out}");
    assert!(out.contains("chat-only pool, never workers"), "{out}");
    assert!(out.contains("CommandCode"), "{out}");
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn token_ladder_pick_subcommand_matches_the_plan_pick_json() {
    let home = scratch("pick-both");
    let brief = home.join("brief.txt");
    std::fs::write(&brief, "Rung: light\n").ok();
    let (code, as_flag, _) = run(
        &home,
        &["pick", "--json", "--brief", brief.to_str().unwrap_or("brief.txt")],
        "",
    );
    assert_eq!(code, 0);
    let (code, as_name, _) = run_as(
        &home,
        "plan-pick",
        &["--json", "--brief", brief.to_str().unwrap_or("brief.txt")],
    );
    assert_eq!(code, 0);
    let left: Value = serde_json::from_str(&as_flag).expect("json");
    let right: Value = serde_json::from_str(&as_name).expect("json");
    assert_eq!(left["plan"], right["plan"]);
    assert_eq!(left["rung"], right["rung"]);
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn status_reports_stale_state_and_the_forced_mode() {
    let home = scratch("status");
    let stale = chrono::Utc::now().timestamp() - 4 * 3600;
    let status = serde_json::json!({
        "ts": stale,
        "mode": "codex",
        "pools": {
            "claude": {"state": "OFF", "used_pct": 100.0},
            "codex": {"state": "ON", "used_pct": 56.0}
        }
    });
    write_state(&home, &status.to_string(), "{}", "{}");
    let (code, out, _) = run(&home, &["status"], "");
    assert_eq!(code, 0);
    assert!(out.contains("Claude OFF"), "{out}");
    assert!(out.contains("Codex ON"), "{out}");
    assert!(out.contains("Mode codex"), "{out}");
    assert!(out.contains("STALE"), "{out}");
    std::fs::remove_dir_all(&home).ok();
}

fn local_config(home: &Path) -> PathBuf {
    home.join("config.local.json")
}

fn bonus_reset(hours: i64) -> String {
    let at = chrono::Utc::now() + chrono::Duration::hours(hours);
    format!("{}", at.format("%Y-%m-%dT%H:%M:%SZ"))
}

#[test]
fn reset_add_writes_a_local_config_and_rejects_bad_timestamps() {
    let home = scratch("reset-add");
    let config = local_config(&home);
    let (code, out, err) = run(
        &home,
        &[
            "reset-add",
            "codex",
            "2099-12-01T18:00:00Z",
            "--config",
            config.to_str().unwrap_or("config.local.json"),
        ],
        "",
    );
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("Registered codex bonus reset at 2099-12-01T18:00:00Z"), "{out}");
    assert!(out.contains("Codex: bonus reset 2099-12-01T18:00:00Z"), "{out}");
    let written: Value = serde_json::from_str(&std::fs::read_to_string(&config).expect("config"))
        .expect("valid json");
    assert_eq!(written["codex_resets"][0]["expires"], "2099-12-01T18:00:00Z");
    let (code, _, err) = run(
        &home,
        &[
            "reset-add",
            "codex",
            "not a date",
            "--config",
            config.to_str().unwrap_or("config.local.json"),
        ],
        "",
    );
    assert_eq!(code, 2);
    assert!(err.contains("ISO-8601 timestamp"), "{err}");
    let (code, _, err) = run(
        &home,
        &[
            "reset-add",
            "antigravity",
            "2099-12-01T18:00:00Z",
            "--config",
            config.to_str().unwrap_or("config.local.json"),
        ],
        "",
    );
    assert_eq!(code, 2);
    assert!(err.contains("plan is codex or claude"), "{err}");
    let (code, _, err) = run(
        &home,
        &[
            "reset-add",
            "claude",
            "2001-01-01T00:00:00Z",
            "--config",
            config.to_str().unwrap_or("config.local.json"),
        ],
        "",
    );
    assert_eq!(code, 2);
    assert!(err.contains("has expired"), "{err}");
    let (code, out, err) = run(
        &home,
        &[
            "reset-add",
            "claude",
            &bonus_reset(12),
            "--config",
            config.to_str().unwrap_or("config.local.json"),
        ],
        "",
    );
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("Claude: bonus reset"), "{out}");
    std::fs::remove_dir_all(&home).ok();
}

#[test]
fn status_and_hook_prompt_surface_an_expiring_complimentary_reset() {
    let home = scratch("bonus-reset");
    let config = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../config.json"),
    )
    .expect("repo config");
    let mut config_value: Value = serde_json::from_str(&config).expect("config json");
    if let Some(object) = config_value.as_object_mut() {
        object.insert(
            "codex_resets".to_string(),
            serde_json::json!([{ "expires": bonus_reset(14) }]),
        );
        object.insert(
            "claude_resets".to_string(),
            serde_json::json!([{ "expires": bonus_reset(12) }]),
        );
    }
    let status = serde_json::json!({
        "ts": chrono::Utc::now().timestamp(),
        "config": config_value,
        "pools": {
            "claude": {"state": "ON", "used_pct": 40.0},
            "codex": {"state": "ON", "used_pct": 56.0}
        }
    });
    write_state(&home, &status.to_string(), "{}", "{}");
    let (code, out, _) = run(&home, &["status"], "");
    assert_eq!(code, 0);
    assert!(out.contains("Codex bonus reset in 14h (burn now)"), "{out}");
    assert!(out.contains("Claude bonus reset in 12h (burn now)"), "{out}");
    let (code, out, _) = run(&home, &["hook-prompt"], "{}");
    assert_eq!(code, 0);
    assert!(out.contains("Codex 56% wk (Codex bonus reset in 14h (burn now))"), "{out}");
    assert!(out.contains("Claude 40% wk (Claude bonus reset in 12h (burn now))"), "{out}");
    std::fs::remove_dir_all(&home).ok();
}
