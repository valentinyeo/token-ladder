use serde_json::{json, Value};
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, Stdio};

fn binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_token-ladder"))
}

struct Sandbox {
    root: PathBuf,
    home: PathBuf,
    worktree: PathBuf,
    config: PathBuf,
}

impl Sandbox {
    fn new(name: &str) -> Self {
        let root = std::env::temp_dir().join(format!(
            "token-ladder-runner-{name}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        let home = root.join("home");
        let worktree = root.join("worktree");
        std::fs::create_dir_all(home.join(".local/state/token-ladder")).ok();
        std::fs::create_dir_all(worktree.join("bin/worker-shims")).ok();
        std::fs::write(worktree.join("brief.txt"), "Ticket: VSTA-343\nRung: medium\n").ok();
        std::fs::write(worktree.join("bin/worker-shims/sleep"), "#!/bin/sh\nexit 1\n").ok();
        Sandbox {
            config: root.join("config.json"),
            root,
            home,
            worktree,
        }
    }

    fn with_config(self, config: Value) -> Self {
        std::fs::write(&self.config, config.to_string()).ok();
        self
    }

    fn state(&self, name: &str) -> PathBuf {
        self.home.join(".local/state/token-ladder").join(name)
    }

    fn write_state(&self, name: &str, text: &str) {
        std::fs::write(self.state(name), text).ok();
    }

    fn state_text(&self, name: &str) -> String {
        std::fs::read_to_string(self.state(name)).unwrap_or_default()
    }

    fn command(&self, args: &[&str]) -> Command {
        let mut command = Command::new(binary());
        command
            .args(args)
            .current_dir(&self.worktree)
            .env("HOME", &self.home)
            .env("TOKEN_LADDER_CONFIG", &self.config)
            .env("TOKEN_LADDER_ROOT", &self.worktree)
            .env_remove("TOKEN_LADDER_STATE_DIR")
            .env_remove("TOKEN_LADDER_LOCAL_CONFIG")
            .env_remove("PLAN_PICK_LAUNCHES")
            .env_remove("MULTIBOT_SESSION")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        command
    }

    fn run(&self, args: &[&str]) -> (i32, String, String) {
        let output = self.command(args).output().expect("binary runs");
        (
            output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&output.stdout).to_string(),
            String::from_utf8_lossy(&output.stderr).to_string(),
        )
    }

    fn run_as_plan_pick(&self, args: &[&str]) -> (i32, String, String) {
        let link = self.root.join("plan-pick");
        let _ = std::fs::remove_file(&link);
        std::os::unix::fs::symlink(binary(), &link).expect("symlink");
        let output = self
            .command(args)
            .arg0(&link)
            .output()
            .expect("binary runs");
        (
            output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&output.stdout).to_string(),
            String::from_utf8_lossy(&output.stderr).to_string(),
        )
    }

    fn launches(&self) -> Vec<Value> {
        self.state_text("launches.jsonl")
            .lines()
            .filter(|line| !line.trim().is_empty())
            .map(|line| serde_json::from_str(line).expect("launch row"))
            .collect()
    }

    fn attempts(&self) -> Value {
        serde_json::from_str(&self.state_text("rung-attempts.json")).unwrap_or_else(|_| json!({}))
    }

    fn limits(&self) -> Value {
        serde_json::from_str(&self.state_text("limits.json")).unwrap_or_else(|_| json!({}))
    }

    fn worktree_text(&self, name: &str) -> String {
        std::fs::read_to_string(self.worktree.join(name)).unwrap_or_default()
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn lane(name: &str, command: &str) -> Value {
    json!({"lane": name, "command": command})
}

fn config(lanes: Vec<Value>) -> Value {
    json!({"lanes": lanes, "limited_hours": 1, "credit_limited_hours": 12})
}

fn now_epoch() -> f64 {
    chrono::Utc::now().timestamp() as f64
}

#[test]
fn run_unsets_multiprompt_tab_and_prepends_the_worker_shims() {
    let sandbox = Sandbox::new("env").with_config(config(vec![lane(
        "stub-a",
        "printf '%s' \"${MULTIPROMPT_TAB-unset}\" > tab.txt; printf '%s' \"$PATH\" > path.txt; \
         echo https://github.com/valentinyeo/token-ladder/pull/7",
    )]));
    let output = sandbox
        .command(&["pick", "--run"])
        .env("MULTIPROMPT_TAB", "tab-123")
        .env("MULTIBOT_SESSION", "chat-1")
        .output()
        .expect("binary runs");
    assert_eq!(output.status.code(), Some(0));
    assert_eq!(sandbox.worktree_text("tab.txt"), "unset");
    let shims = sandbox.worktree.join("bin/worker-shims");
    assert!(
        sandbox
            .worktree_text("path.txt")
            .starts_with(&shims.display().to_string()),
        "PATH does not start with the worker shims: {}",
        sandbox.worktree_text("path.txt")
    );
    let launches = sandbox.launches();
    assert_eq!(launches.len(), 1);
    assert_eq!(launches[0]["lane"], "stub-a");
    assert_eq!(launches[0]["rung"], "medium");
    assert_eq!(launches[0]["outcome"], "finished");
    assert_eq!(launches[0]["caller"], "chat-1");
    assert_eq!(
        launches[0]["pr_url"],
        "https://github.com/valentinyeo/token-ladder/pull/7"
    );
    assert!(launches[0]["at"].as_f64().unwrap_or_default() > 0.0);
}

#[test]
fn run_marks_a_quota_lane_limited_and_retries_the_next_lane() {
    let sandbox = Sandbox::new("quota").with_config(config(vec![
        lane("stub-a", "echo '429 rate limit exceeded'; exit 1"),
        lane(
            "stub-b",
            "echo https://github.com/valentinyeo/token-ladder/pull/99",
        ),
    ]));
    let (code, out, err) = sandbox.run(&["pick", "--run"]);
    assert_eq!(code, 0, "{err}");
    assert!(
        out.contains("plan-pick: lane stub-b finished after"),
        "missing summary line: {out}"
    );
    assert!(out.contains("pull/99"), "{out}");
    assert!(
        err.contains("lane stub-a hit a rate or usage limit"),
        "missing limit line: {err}"
    );
    let limits = sandbox.limits();
    let until = limits["stub-a"]["until"].as_f64().unwrap_or_default();
    assert!(until > now_epoch() + 1800.0);
    assert_eq!(limits["stub-a"]["reason"], "hit a rate or usage limit");
    assert_eq!(limits["stub-a"]["source"], "429");
    assert!(limits["stub-a"]["error_line"]
        .as_str()
        .unwrap_or_default()
        .contains("429"));
    let launches = sandbox.launches();
    assert_eq!(launches.len(), 2);
    assert_eq!(launches[0]["lane"], "stub-a");
    assert_eq!(launches[0]["outcome"], "limited");
    assert_eq!(launches[1]["lane"], "stub-b");
    assert_eq!(launches[1]["outcome"], "finished");
    assert_eq!(
        launches[1]["pr_url"],
        "https://github.com/valentinyeo/token-ladder/pull/99"
    );
    assert_eq!(sandbox.attempts()["ticket:VSTA-343"]["failure"], Value::Null);
}

#[test]
fn run_marks_empty_credit_limited_with_the_credit_hours() {
    let sandbox = Sandbox::new("credit-limit").with_config(config(vec![
        lane("stub-a", "echo '402 payment required'; exit 1"),
        lane("stub-b", "echo nothing to see"),
    ]));
    let (_code, _out, err) = sandbox.run(&["pick", "--run"]);
    assert!(err.contains("lane stub-a is out of credit"), "{err}");
    let limits = sandbox.limits();
    let until = limits["stub-a"]["until"].as_f64().unwrap_or_default();
    assert!(until > now_epoch() + 11.5 * 3600.0, "{until}");
    assert_eq!(limits["stub-a"]["source"], "quota error");
}

#[test]
fn run_records_a_worker_shim_refusal_as_environment_without_climbing() {
    let sandbox = Sandbox::new("shim").with_config(config(vec![lane(
        "stub-a",
        "echo 'plan-pick worker: sleep 240 refused (over 30s; workers push and exit)'; exit 3",
    )]));
    let (code, _out, err) = sandbox.run(&["pick", "--run"]);
    assert_ne!(code, 0);
    assert!(err.contains("worker-shim refusal"), "{err}");
    assert!(err.contains("no rung climb"), "{err}");
    let attempts = sandbox.attempts();
    assert_eq!(attempts["ticket:VSTA-343"]["failure"], "environment");
    assert_eq!(attempts["ticket:VSTA-343"]["rung"], "medium");
    assert_eq!(attempts["ticket:VSTA-343"]["exit_code"], 3);
    assert!(sandbox.limits().get("stub-a").is_none());
    assert_eq!(sandbox.launches()[0]["outcome"], "environment");
}

#[test]
fn run_records_a_work_failure_so_the_next_pick_climbs_a_rung() {
    let sandbox = Sandbox::new("climb").with_config(config(vec![
        lane("stub-a", "echo 'error: the tests failed'; exit 1"),
        lane(
            "codex",
            "echo https://github.com/valentinyeo/token-ladder/pull/100",
        ),
    ]));
    let (code, out, _err) = sandbox.run(&["pick", "--run"]);
    assert_eq!(code, 1);
    assert!(out.contains("with no pull request"), "{out}");
    let attempts = sandbox.attempts();
    assert_eq!(attempts["ticket:VSTA-343"]["failure"], "work");
    assert_eq!(attempts["ticket:VSTA-343"]["rung"], "medium");
    let (code, _out, err) = sandbox.run(&["pick", "--run"]);
    assert_eq!(code, 0, "{err}");
    let launches = sandbox.launches();
    let climbed = launches.last().expect("climbed launch");
    assert_eq!(climbed["rung"], "hard");
    assert_eq!(climbed["rung_source"], "climb");
    assert_eq!(climbed["lane"], "codex");
    assert_eq!(climbed["outcome"], "finished");
    assert_eq!(sandbox.attempts()["ticket:VSTA-343"]["rung"], "hard");
}

#[test]
fn run_writes_a_null_lane_row_and_exits_75_when_no_lane_is_free() {
    let sandbox = Sandbox::new("empty").with_config(config(vec![]));
    let (code, _out, err) = sandbox.run(&["pick", "--run"]);
    assert_eq!(code, 75, "{err}");
    assert!(err.contains("no lane left"), "{err}");
    let launches = sandbox.launches();
    assert_eq!(launches.len(), 1);
    assert!(launches[0]["lane"].is_null());
    assert_eq!(launches[0]["outcome"], "none");
    assert_eq!(launches[0]["rung"], "medium");
}

#[test]
fn run_refuses_a_brief_flag_and_reads_brief_txt_from_the_worktree() {
    let sandbox = Sandbox::new("brief").with_config(config(vec![lane("stub-a", "cat brief.txt")]));
    let (code, _out, err) = sandbox.run(&["pick", "--run", "--brief", "other.txt"]);
    assert_eq!(code, 2);
    assert!(err.contains("--run reads brief.txt"), "{err}");
    assert!(sandbox.launches().is_empty());
    let (code, out, err) = sandbox.run(&["pick", "--run"]);
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("Ticket: VSTA-343"), "{out}");
}

#[test]
fn run_appends_the_author_model_line_to_the_worker_brief() {
    let sandbox = Sandbox::new("author").with_config(config(vec![lane(
        "stub-a",
        "true --model deepseek-v4.1-flash; echo model=$PLAN_PICK_AUTHOR_MODEL; cat brief.txt; \
         echo https://github.com/valentinyeo/token-ladder/pull/101",
    )]));
    let (code, out, err) = sandbox.run(&["pick", "--run"]);
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("model=deepseek/deepseek-v4.1-flash"), "{out}");
    assert!(
        out.contains(
            "Your PR body must contain exactly: Author model: deepseek/deepseek-v4.1-flash"
        ),
        "{out}"
    );
    assert!(!out.contains("gets no Author model line"), "{err}");
}

fn pinned_config(codex_command: &str) -> Value {
    config(vec![
        lane("commandcode", "echo commandcode >> ran.txt; echo launched"),
        lane("vercel", "echo vercel >> ran.txt; echo launched"),
        json!({"lane": "codex", "pool": "codex", "command": codex_command}),
    ])
}

#[test]
fn plan_pick_run_hard_flag_launches_only_heavy_without_a_brief_rung() {
    let sandbox = Sandbox::new("hard-flag")
        .with_config(pinned_config("echo codex >> ran.txt; echo launched"));
    std::fs::write(sandbox.worktree.join("brief.txt"), "Ticket: VSTA-351\n").unwrap();
    sandbox.write_state(
        "status.json",
        r#"{"pools":{"codex":{"state":"ON","pace_ratio":0.5}}}"#,
    );
    let (code, _out, err) = sandbox.run_as_plan_pick(&["--run", "--rung", "hard"]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(sandbox.worktree_text("ran.txt"), "codex\n");
    let launches = sandbox.launches();
    assert_eq!(launches.len(), 1);
    assert_eq!(launches[0]["lane"], "codex");
    assert_eq!(launches[0]["rung"], "hard");
    assert_eq!(launches[0]["rung_source"], "flag");
}

#[test]
fn plan_pick_run_hard_refuses_instead_of_running_a_workhorse() {
    let sandbox = Sandbox::new("hard-unavailable")
        .with_config(pinned_config("echo codex >> ran.txt; echo launched"));
    sandbox.write_state("status.json", r#"{"pools":{"codex":{"state":"OFF"}}}"#);
    let (code, _out, err) = sandbox.run_as_plan_pick(&["--run", "--rung", "hard"]);
    assert_eq!(code, 75, "{err}");
    assert!(sandbox.worktree_text("ran.txt").is_empty());
    let launches = sandbox.launches();
    assert_eq!(launches.len(), 1);
    assert!(launches[0]["lane"].is_null());
    assert_eq!(launches[0]["rung"], "hard");
}

#[test]
fn plan_pick_run_lane_flag_and_brief_pin_launch_only_codex_despite_pace() {
    for source in ["flag", "brief"] {
        let sandbox = Sandbox::new(&format!("pin-{source}"))
            .with_config(pinned_config("echo codex >> ran.txt; echo launched"));
        sandbox.write_state(
            "status.json",
            r#"{"pools":{"codex":{"state":"ON","pace_ratio":1.5}}}"#,
        );
        let args = if source == "flag" {
            vec!["--run", "--lane", "codex"]
        } else {
            std::fs::write(
                sandbox.worktree.join("brief.txt"),
                "Ticket: VSTA-351\nLane: codex\n",
            )
            .unwrap();
            vec!["--run"]
        };
        let (code, _out, err) = sandbox.run_as_plan_pick(&args);
        assert_eq!(code, 0, "{source}: {err}");
        assert_eq!(sandbox.worktree_text("ran.txt"), "codex\n");
        assert_eq!(sandbox.launches()[0]["lane"], "codex");
    }
}

#[test]
fn plan_pick_run_pinned_codex_refuses_off_limited_and_quota_without_fallback() {
    for source in ["flag", "brief"] {
        for unavailable in ["off", "limited", "429", "quota", "silent"] {
            let command = match unavailable {
                "429" => "echo codex >> ran.txt; echo '429 rate limit exceeded'; exit 1",
                "quota" => "echo codex >> ran.txt; echo '402 payment required'; exit 1",
                "silent" => "echo codex >> ran.txt",
                _ => "echo codex >> ran.txt; echo launched",
            };
            let sandbox = Sandbox::new(&format!("pin-{source}-{unavailable}"))
                .with_config(pinned_config(command));
            let state = if unavailable == "off" { "OFF" } else { "ON" };
            sandbox.write_state(
                "status.json",
                &json!({"pools":{"codex":{"state":state}}}).to_string(),
            );
            if unavailable == "limited" {
                sandbox.write_state(
                    "limits.json",
                    &json!({"codex":now_epoch() + 3600.0}).to_string(),
                );
            }
            let args = if source == "flag" {
                vec!["--run", "--lane", "codex"]
            } else {
                std::fs::write(
                    sandbox.worktree.join("brief.txt"),
                    "Ticket: VSTA-351\nLane: codex\n",
                )
                .unwrap();
                vec!["--run"]
            };
            let (code, _out, err) = sandbox.run_as_plan_pick(&args);
            assert_eq!(code, 75, "{source} {unavailable}: {err}");
            let attempted = matches!(unavailable, "429" | "quota" | "silent");
            assert_eq!(
                sandbox.worktree_text("ran.txt"),
                if attempted { "codex\n" } else { "" }
            );
            let launches = sandbox.launches();
            assert_eq!(launches.len(), if attempted { 2 } else { 1 });
            let last = launches.last().expect("null launch");
            assert!(last["lane"].is_null());
            assert_eq!(last["outcome"], "none");
            if attempted {
                assert_eq!(launches[0]["lane"], "codex");
                assert_eq!(launches[0]["outcome"], "limited");
                assert!(sandbox.limits()["codex"]["until"].as_f64().unwrap() > now_epoch());
                assert!(!err.contains("trying the next lane"), "{err}");
            }
        }
    }
}

#[test]
fn plan_pick_run_pinned_orchestrators_are_refused_as_chat_only() {
    let sandbox = Sandbox::new("pin-chat-only").with_config(config(vec![
        lane("commandcode", "echo commandcode >> ran.txt"),
        lane("antigravity", "echo antigravity >> ran.txt"),
    ]));
    for name in ["antigravity", "claude"] {
        let (code, _out, err) = sandbox.run_as_plan_pick(&["--run", "--lane", name]);
        assert_eq!(code, 75, "{err}");
        assert!(err.contains("chat-only pool, never workers"), "{err}");
        assert!(sandbox.worktree_text("ran.txt").is_empty());
        assert!(sandbox.launches().last().expect("null launch")["lane"].is_null());
    }
}

#[test]
fn plan_pick_limited_writes_limits_json_atomically() {
    let sandbox = Sandbox::new("limited").with_config(config(vec![lane("stub-a", "true")]));
    sandbox.write_state("limits.json", r#"{"codex": 1.0}"#);
    let (code, out, err) = sandbox.run_as_plan_pick(&[
        "--limited",
        "stub-a",
        "--hours",
        "4",
        "--reason",
        "dashboard says 100% used",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("Lane stub-a is skipped until"), "{out}");
    let limits = sandbox.limits();
    let now = now_epoch();
    let until = limits["stub-a"]["until"].as_f64().unwrap_or_default();
    assert!(until > now + 3.9 * 3600.0 && until < now + 4.1 * 3600.0);
    assert_eq!(limits["stub-a"]["reason"], "dashboard says 100% used");
    assert_eq!(limits["stub-a"]["source"], "manual");
    assert_eq!(limits["codex"], 1.0);
    assert!(!sandbox.state("limits.json.tmp").exists());
}

#[test]
fn plan_pick_limited_refuses_an_unknown_lane() {
    let sandbox = Sandbox::new("limited-unknown").with_config(config(vec![lane("stub-a", "true")]));
    let (code, _out, err) = sandbox.run_as_plan_pick(&["--limited", "nope"]);
    assert_eq!(code, 2);
    assert!(err.contains("unknown lane nope"), "{err}");
    assert!(err.contains("stub-a"), "{err}");
}

#[test]
fn plan_pick_set_credit_writes_lane_credits_json_atomically() {
    let sandbox = Sandbox::new("credit").with_config(json!({
        "lanes": [{"lane": "opencode", "pool": "opencode", "command": "true"}],
        "limited_hours": 1
    }));
    sandbox.write_state("lane-credits.json", r#"{"vercel": {"usd": 14.24}}"#);
    let (code, out, err) = sandbox.run_as_plan_pick(&[
        "--set-credit",
        "opencode",
        "24.64",
        "--credit-source",
        "console screenshot",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("Stored opencode balance anchor at"), "{out}");
    assert!(out.contains("console screenshot"), "{out}");
    let credits: Value =
        serde_json::from_str(&sandbox.state_text("lane-credits.json")).expect("credits json");
    assert_eq!(credits["opencode"]["usd"], 24.64);
    assert_eq!(credits["opencode"]["source"], "console screenshot");
    assert_eq!(credits["vercel"]["usd"], 14.24);
    assert!(!sandbox.state("lane-credits.json.tmp").exists());
}

#[test]
fn plan_pick_set_credit_refuses_a_noncredit_lane_and_a_future_anchor() {
    let sandbox = Sandbox::new("credit-bad").with_config(json!({
        "lanes": [{"lane": "opencode", "pool": "opencode", "command": "true"}],
        "limited_hours": 1
    }));
    let (code, _out, err) = sandbox.run_as_plan_pick(&["--set-credit", "codex", "5"]);
    assert_eq!(code, 2);
    assert!(err.contains("--set-credit needs a credit lane"), "{err}");
    let (code, _out, err) = sandbox.run_as_plan_pick(&["--set-credit", "opencode", "not-a-number"]);
    assert_eq!(code, 2);
    assert!(err.contains("needs a number of USD"), "{err}");
    assert!(sandbox.state_text("lane-credits.json").is_empty());
}

#[test]
fn plans_json_keeps_chat_only_when_the_plan_or_its_pool_says_so() {
    let sandbox = Sandbox::new("plans").with_config(json!({
        "lanes": [{"lane": "stub-a", "command": "true"}],
        "plans": [
            {"name": "Google plan", "vendor": "google", "pool": "google", "chat_only": true},
            {"name": "Codex plan", "vendor": "openai", "pool": "codex"},
            {"name": "Plain plan", "vendor": "zai", "pool": "grok"}
        ],
        "limited_hours": 1
    }));
    sandbox.write_state(
        "status.json",
        r#"{"ts": 1795785009.0, "pools": {"google": {"state": "ON", "used_pct": 40.0},
             "codex": {"state": "ON", "used_pct": 20.0, "chat_only": true},
             "grok": {"state": "ON", "used_pct": 10.0}}}"#,
    );
    sandbox.write_state("lane-credits.json", "{}");
    sandbox.write_state("limits.json", "{}");
    let (code, out, err) = sandbox.run_as_plan_pick(&["--plans", "--json"]);
    assert_eq!(code, 0, "{err}");
    let value: Value = serde_json::from_str(&out).expect("plans json");
    let rows = value["plans"].as_array().expect("plan rows");
    let chat_only = |name: &str| {
        rows.iter()
            .find(|row| row["name"] == name)
            .map(|row| row["chat_only"].as_bool().unwrap_or(false))
            .expect("plan row")
    };
    assert!(chat_only("Google plan"));
    assert!(chat_only("Codex plan"));
    assert!(!chat_only("Plain plan"));
}

#[test]
fn pools_json_reads_a_pools_wrapper_without_a_timestamp() {
    let sandbox = Sandbox::new("pools-wrapper").with_config(config(vec![lane("stub-a", "true")]));
    sandbox.write_state(
        "status.json",
        r#"{"pools": {"codex": {"state": "ON", "used_pct": 56.0}}}"#,
    );
    sandbox.write_state("lane-credits.json", "{}");
    sandbox.write_state("limits.json", "{}");
    let (code, out, err) = sandbox.run(&["pools", "--json"]);
    assert_eq!(code, 0, "{err}");
    let value: Value = serde_json::from_str(&out).expect("pools json");
    assert_eq!(value["codex"]["state"], "ON");
    assert_eq!(value["codex"]["used_pct"], 56.0);
    assert!(value.get("pools").is_none());
}
