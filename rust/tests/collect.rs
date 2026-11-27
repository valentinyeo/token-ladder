use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Command, Output, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

fn binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_token-ladder"))
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0)
}

fn iso(epoch: f64) -> String {
    chrono::DateTime::<chrono::Utc>::from_timestamp(epoch as i64, 0)
        .map(|stamp| stamp.format("%Y-%m-%dT%H:%M:%SZ").to_string())
        .unwrap_or_default()
}

#[derive(Clone, Debug)]
struct Recorded {
    method: String,
    path: String,
    headers: HashMap<String, String>,
    body: String,
}

#[derive(Clone)]
enum Reply {
    Json(String),
    Status(u16),
}

struct Stub {
    url: String,
    seen: Arc<Mutex<Vec<Recorded>>>,
}

impl Stub {
    fn requests(&self) -> Vec<Recorded> {
        self.seen.lock().map(|seen| seen.clone()).unwrap_or_default()
    }

    fn last(&self) -> Option<Recorded> {
        self.requests().pop()
    }
}

fn stub(reply: Reply) -> Stub {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind a stub port");
    let addr = listener.local_addr().expect("stub address");
    let seen = Arc::new(Mutex::new(Vec::new()));
    let sink = Arc::clone(&seen);
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            if let Some(record) = serve(stream, &reply) {
                if let Ok(mut seen) = sink.lock() {
                    seen.push(record);
                }
            }
        }
    });
    Stub {
        url: format!("http://{addr}"),
        seen,
    }
}

fn serve(mut stream: TcpStream, reply: &Reply) -> Option<Recorded> {
    stream.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    let mut reader = BufReader::new(stream.try_clone().ok()?);
    let mut request_line = String::new();
    reader.read_line(&mut request_line).ok()?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next()?.to_string();
    let path = parts.next()?.to_string();
    let mut headers = HashMap::new();
    let mut length = 0usize;
    loop {
        let mut header = String::new();
        if reader.read_line(&mut header).ok()? == 0 {
            break;
        }
        let line = header.trim_end();
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            let name = name.trim().to_ascii_lowercase();
            let value = value.trim().to_string();
            if name == "content-length" {
                length = value.parse().unwrap_or(0);
            }
            headers.insert(name, value);
        }
    }
    let mut body = vec![0u8; length];
    if length > 0 {
        reader.read_exact(&mut body).ok()?;
    }
    let (code, payload) = match reply {
        Reply::Json(text) => (200u16, text.clone()),
        Reply::Status(code) => (*code, "{}".to_string()),
    };
    let response = format!(
        "HTTP/1.1 {code} Stub\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
        payload.len()
    );
    stream.write_all(response.as_bytes()).ok()?;
    Some(Recorded {
        method,
        path,
        headers,
        body: String::from_utf8(body).ok()?,
    })
}

struct Sandbox {
    root: PathBuf,
    home: PathBuf,
}

impl Sandbox {
    fn new(name: &str) -> Self {
        let root = std::env::temp_dir().join(format!(
            "token-ladder-collect-{name}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        let home = root.join("home");
        std::fs::create_dir_all(home.join(".local/state/token-ladder")).ok();
        std::fs::create_dir_all(home.join(".codex/sessions")).ok();
        std::fs::create_dir_all(home.join(".claude")).ok();
        std::fs::create_dir_all(home.join(".gemini/antigravity-cli")).ok();
        let sandbox = Sandbox { root, home };
        sandbox.stub_command(
            "ps",
            "#!/bin/sh\nprintf 'claude 1048576\\nnode 524288\\ncodex 262144\\nzsh 1024\\n'\n",
        );
        sandbox
    }

    fn config(&self, value: Value) {
        std::fs::write(self.root.join("config.json"), value.to_string()).ok();
    }

    fn base_config(&self) -> Value {
        json!({
            "lanes": [
                {"lane": "codex", "pool": "codex", "command": "true"},
                {"lane": "opencode", "credit": "opencode", "command": "true"}
            ],
            "plans": [
                {"name": "OpenCode Go", "vendor": "opencode", "credit": "opencode", "usd_per_cycle": 40}
            ],
            "primary_hostname": "ladder-test-host",
            "primary_ssh": "ladder-test-host",
            "mirrors": [],
            "push_url": "",
            "push_token_key": ["Multiprompt Account", "USAGE_PUSH_TOKEN"],
            "claude_label": "Claude Max",
            "codex_label": "Codex Plus",
            "ladder_name": "Test ladder",
            "grok_stale_hours": 3,
            "pool_warn_pct": 80,
            "pool_off_pct": 95,
            "claude_floor_percent": 15,
            "five_hour_max_percent": 80,
            "reset_use_at_percent": 95,
            "reset_burn_hours": 36,
            "reset_min_hours_before_weekly": 24,
            "claude_behind_margin": 5,
            "codex_resets": [],
            "claude_resets": [],
            "tight_ram_free_gb": 16,
            "tight_load_per_core": 2,
            "unknown_assumed_pct": 70,
            "key_lookup_every_s": 1800,
            "measured_burn": []
        })
    }

    fn write(&self, path: &str, text: &str) {
        let full = self.home.join(path);
        if let Some(parent) = full.parent() {
            std::fs::create_dir_all(parent).ok();
        }
        std::fs::write(full, text).ok();
    }

    fn stub_command(&self, name: &str, script: &str) -> PathBuf {
        let path = self.root.join(name);
        std::fs::write(&path, script).ok();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).ok();
        path
    }

    fn state(&self, name: &str) -> PathBuf {
        self.home.join(".local/state/token-ladder").join(name)
    }

    fn state_text(&self, name: &str) -> String {
        std::fs::read_to_string(self.state(name)).unwrap_or_default()
    }

    fn status(&self) -> Value {
        serde_json::from_str(&self.state_text("status.json")).expect("status.json is JSON")
    }

    fn command(&self, args: &[&str]) -> Command {
        let mut command = Command::new(binary());
        command
            .args(args)
            .current_dir(&self.root)
            .env("HOME", &self.home)
            .env("TOKEN_LADDER_CONFIG", self.root.join("config.json"))
            .env("TOKEN_LADDER_HOSTNAME", "ladder-test-host")
            .env("TOKEN_LADDER_AGY", "/nonexistent/agy")
            .env("TOKEN_LADDER_CODEX", "/nonexistent/codex")
            .env("TOKEN_LADDER_SSH", "/nonexistent/ssh")
            .env("TOKEN_LADDER_RSYNC", "/nonexistent/rsync")
            .env("TOKEN_LADDER_PS", self.root.join("ps"))
            .env("TOKEN_LADDER_YEO_KEY", self.root.join("yeo-key"))
            .env_remove("TOKEN_LADDER_STATE_DIR")
            .env_remove("TOKEN_LADDER_LOCAL_CONFIG")
            .env_remove("PLAN_PICK_LAUNCHES")
            .env_remove("OPENROUTER_API_KEY")
            .env_remove("OPENCODE_API_KEY")
            .env_remove("COMMANDCODE_API_KEY")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        command
    }

    fn output(&self, command: &mut Command) -> Output {
        command.output().expect("the binary runs")
    }

    fn run(&self, args: &[&str]) -> (i32, String, String) {
        let output = self.output(&mut self.command(args));
        (
            output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&output.stdout).to_string(),
            String::from_utf8_lossy(&output.stderr).to_string(),
        )
    }

    fn run_as_plan_pick(&self, args: &[&str]) -> (i32, String, String) {
        let link = self.root.join("plan-pick");
        let _ = std::fs::remove_file(&link);
        std::os::unix::fs::symlink(binary(), &link).expect("symlink plan-pick");
        let mut command = self.command(args);
        command.arg0(&link);
        let output = self.output(&mut command);
        (
            output.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&output.stdout).to_string(),
            String::from_utf8_lossy(&output.stderr).to_string(),
        )
    }
}

impl Drop for Sandbox {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn claude_usage_reply() -> String {
    let stamp = now();
    json!({
        "five_hour": {"utilization": 42.0, "resets_at": iso(stamp + 3.0 * 3600.0)},
        "seven_day": {"utilization": 63.0, "resets_at": iso(stamp + 4.0 * 86400.0)},
        "limits": [],
        "weekly_scoped_shares": []
    })
    .to_string()
}

fn fixture_claude(sandbox: &Sandbox) {
    sandbox.write(
        ".claude/.credentials.json",
        r#"{"claudeAiOauth":{"accessToken":"claude-access-token-value"}}"#,
    );
    sandbox.write(
        ".claude.json",
        r#"{"oauthAccount":{"emailAddress":"valentin@example.com"}}"#,
    );
}

fn fixture_codex(sandbox: &Sandbox) {
    let stamp = now();
    let line = json!({
        "timestamp": iso(stamp - 30.0),
        "payload": {"rate_limits": {
            "plan_type": "plus",
            "primary": {"used_percent": 56.0, "resets_at": stamp + 4.0 * 86400.0, "window_minutes": 10080},
            "secondary": {"used_percent": 12.0, "resets_at": stamp + 2.0 * 3600.0, "window_minutes": 300}
        }}
    });
    sandbox.write(
        ".codex/sessions/2026/11/24/rollout-1.jsonl",
        &format!("{line}\n"),
    );
}

fn fixture_agy(sandbox: &Sandbox) {
    let stamp = now();
    let text = format!(
        "gemini models\tweekly limit remaining\t73%\t{}\ngemini models\tfive hour limit remaining\t91%\t{}\n",
        iso(stamp + 4.0 * 86400.0),
        iso(stamp + 3.0 * 3600.0)
    );
    sandbox.write(
        ".local/state/token-ladder/agy-usage.json",
        &json!({"at": stamp, "text": text}).to_string(),
    );
    sandbox.write(
        ".gemini/antigravity-cli/history.jsonl",
        &format!(
            "{}\n{}\n",
            json!({"timestamp": (stamp - 600.0) * 1000.0}),
            json!({"timestamp": (stamp - 60.0) * 1000.0})
        ),
    );
}

fn fixture_grok(sandbox: &Sandbox) {
    let stamp = now();
    sandbox.write(
        ".local/state/token-ladder/grok.json",
        &json!({
            "pct": 41.0,
            "resets_at": iso(stamp + 5.0 * 86400.0),
            "updated_at": iso(stamp - 60.0)
        })
        .to_string(),
    );
}

fn collect_with(sandbox: &Sandbox) -> (i32, String, String, Value) {
    sandbox.config(sandbox.base_config());
    let (code, out, err) = sandbox.run(&["collect"]);
    (code, out, err, sandbox.status())
}

#[test]
fn collect_assembles_the_status_the_dashboards_read() {
    let sandbox = Sandbox::new("status");
    fixture_claude(&sandbox);
    fixture_codex(&sandbox);
    fixture_agy(&sandbox);
    fixture_grok(&sandbox);
    sandbox.config(sandbox.base_config());
    let usage = stub(Reply::Json(claude_usage_reply()));
    let mut command = sandbox.command(&["collect"]);
    command.env("TOKEN_LADDER_CLAUDE_USAGE_URL", format!("{}/api/oauth/usage", usage.url));
    let output = sandbox.output(&mut command);
    assert_eq!(
        output.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let status = sandbox.status();

    assert!(status["ts"].as_f64().unwrap_or_default() > 0.0);
    assert!(status["mode"].is_string());
    assert_eq!(status["override"], "auto");
    assert!(status["config"]["lanes"].is_array());
    assert!(status["config"]["push_url"].is_string());

    let accounts = status["accounts"].as_array().expect("account rows");
    let claude = accounts
        .iter()
        .find(|row| row["id"] == "claude")
        .expect("claude account");
    assert_eq!(claude["email"], "valentin@example.com");
    assert_eq!(claude["week"]["used"], 63.0);
    assert!(claude["week"]["time_gone"].as_f64().unwrap_or_default() > 40.0);
    assert_eq!(claude["stale"], false);
    assert_eq!(claude["in_ladder"], true);
    let codex = accounts
        .iter()
        .find(|row| row["id"] == "codex")
        .expect("codex account");
    assert_eq!(codex["week"]["used"], 56.0);
    assert_eq!(codex["stale"], false);

    assert_eq!(status["claude"]["week"]["used"], 63.0);
    assert_eq!(status["claude"]["five_hour"]["used"], 42.0);
    assert_eq!(status["codex"]["week"]["used"], 56.0);
    assert_eq!(status["codex"]["five_hour"]["used"], 12.0);

    let pools = &status["pools"];
    assert_eq!(pools["google"]["chat_only"], true);
    assert_eq!(pools["google"]["shared_with_chats"], false);
    assert_eq!(pools["google"]["used_pct"], 27.0);
    assert_eq!(pools["google"]["five_hour_used_pct"], 9.0);
    assert_eq!(pools["google"]["used"], 2.0);
    assert!(pools["google"]["window_minutes"].as_u64().unwrap_or_default() > 0);
    assert_eq!(pools["grok"]["used_pct"], 41.0);
    assert_eq!(pools["claude"]["used_pct"], 63.0);
    assert_eq!(pools["codex"]["used_pct"], 56.0);

    let resources = &status["resources"];
    assert_eq!(resources["claude"]["count"], 1);
    assert_eq!(resources["claude"]["rss_gb"], 1.0);
    assert_eq!(resources["node"]["rss_gb"], 0.5);
    assert_eq!(resources["codex"]["rss_gb"], 0.25);
    assert!(resources["ram_total_gb"].as_f64().unwrap_or_default() > 0.0);
    assert!(resources["cores"].as_u64().unwrap_or_default() >= 1);
    assert!(resources["pressure"].is_string());

    let burn = &status["burn"];
    assert_eq!(burn["claude"]["left"], 37.0);
    assert_eq!(burn["codex"]["left"], 44.0);
    assert!(burn["claude"]["reset"].is_null());
    assert!(status["onepassword"]["error"].is_string());
    assert!(status["openrouter"]["error"].is_string());
    assert_eq!(status["pace_alerts"], json!([]));
    assert!(status["pushed_at"].is_null());
    assert!(status["cloud"]["codex"].is_object());

    let history = sandbox.state_text("history.jsonl");
    let row: Value = serde_json::from_str(history.lines().next().expect("history row")).expect("history JSON");
    assert_eq!(row["accounts"]["claude"]["week"]["used"], 63.0);
    assert_eq!(row["claude_week"], 63.0);

    let after = usage.last().expect("claude usage request");
    assert_eq!(after.path, "/api/oauth/usage");
    assert_eq!(
        after.headers.get("anthropic-beta").map(String::as_str),
        Some("oauth-2025-04-20")
    );
}

#[test]
fn collect_records_a_provider_failure_and_still_writes_status() {
    let sandbox = Sandbox::new("provider-error");
    fixture_claude(&sandbox);
    sandbox.config(sandbox.base_config());
    let usage = stub(Reply::Status(500));
    let mut command = sandbox.command(&["collect"]);
    command.env("TOKEN_LADDER_CLAUDE_USAGE_URL", format!("{}/api/oauth/usage", usage.url));
    let output = sandbox.output(&mut command);
    assert_eq!(output.status.code(), Some(0));
    let status = sandbox.status();
    assert_eq!(status["claude"]["error"], "status 500");
    assert!(status["pools"]["claude"]["state"].is_string());
    assert_eq!(status["accounts"][0]["stale"], true);
}

#[test]
fn mode_keeps_the_override_and_auto_removes_it() {
    let sandbox = Sandbox::new("mode");
    sandbox.config(sandbox.base_config());
    let (code, _out, err) = sandbox.run(&["mode", "claude"]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(sandbox.state_text("override").trim(), "claude");
    assert_eq!(sandbox.status()["override"], "claude");
    assert_eq!(sandbox.status()["mode"], "claude");

    let (code, _out, err) = sandbox.run(&["mode", "codex"]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(sandbox.state_text("override").trim(), "codex");
    assert_eq!(sandbox.status()["mode"], "codex");

    let (code, _out, err) = sandbox.run(&["mode", "auto"]);
    assert_eq!(code, 0, "{err}");
    assert!(!sandbox.state("override").exists());
    assert_eq!(sandbox.status()["override"], "auto");

    let (code, out, err) = sandbox.run(&["mode", "turbo"]);
    assert_eq!(code, 1);
    assert!(out.is_empty());
    assert!(err.contains("usage: token-ladder mode claude|codex|auto"), "{err}");
}

#[test]
fn mode_forwards_to_the_primary_host_off_it() {
    let sandbox = Sandbox::new("mode-forward");
    let recorder = sandbox.root.join("forwarded.txt");
    let ssh = sandbox.stub_command(
        "ssh",
        &format!("#!/bin/sh\nprintf '%s\\n' \"$*\" > {}\nexit 0\n", recorder.display()),
    );
    sandbox.config(sandbox.base_config());
    let mut command = sandbox.command(&["mode", "claude"]);
    command
        .env("TOKEN_LADDER_HOSTNAME", "some-other-host")
        .env("TOKEN_LADDER_SSH", &ssh);
    let output = sandbox.output(&mut command);
    assert_eq!(output.status.code(), Some(0));
    let forwarded = std::fs::read_to_string(&recorder).unwrap_or_default();
    assert!(forwarded.contains("ladder-test-host"), "{forwarded}");
    assert!(forwarded.contains("projects/token-ladder/bin/token-ladder"), "{forwarded}");
    assert!(forwarded.trim_end().ends_with("mode claude"), "{forwarded}");
    assert!(!sandbox.state("override").exists());
}

#[test]
fn route_json_matches_the_shared_cloud_map() {
    let sandbox = Sandbox::new("route");
    sandbox.config(sandbox.base_config());
    std::fs::write(
        sandbox.state("status.json"),
        json!({
            "ts": 1_795_783_606.0,
            "mode": "codex",
            "cloud": {"codex": {"valentinyeo/token-ladder": "env-42"}, "claude": {}}
        })
        .to_string(),
    )
    .ok();
    let (code, out, err) = sandbox.run(&["route", "--repo", "valentinyeo/token-ladder", "--json"]);
    assert_eq!(code, 0, "{err}");
    let route: Value = serde_json::from_str(&out).expect("route JSON");
    assert_eq!(route["destination"], "codex-cloud");
    assert_eq!(route["command"], "codex cloud exec --env env-42");
    assert_eq!(route["mode"], "codex");
    assert_eq!(route["repo"], "valentinyeo/token-ladder");

    let (code, out, _err) = sandbox.run(&["route", "--repo", "valentinyeo/other"]);
    assert_eq!(code, 0);
    assert!(out.starts_with("local\t"), "{out}");
    assert!(out.contains("hax --provider=codex"), "{out}");

    std::fs::write(
        sandbox.state("status.json"),
        json!({"mode": "claude", "cloud": {"claude": {"valentinyeo/api": "task-1"}}}).to_string(),
    )
    .ok();
    let (code, out, _err) = sandbox.run(&["route", "--repo", "valentinyeo/api", "--json"]);
    assert_eq!(code, 0);
    let route: Value = serde_json::from_str(&out).expect("route JSON");
    assert_eq!(route["destination"], "claude-cloud");
    assert_eq!(route["command"], "claude-cloud-task");

    let (code, out, _err) = sandbox.run(&["route", "--repo", "valentinyeo/unknown", "--json"]);
    assert_eq!(code, 0);
    let route: Value = serde_json::from_str(&out).expect("route JSON");
    assert_eq!(route["destination"], "local");
    assert_eq!(route["command"], "Agent tool in this session");
}

#[test]
fn grok_set_writes_the_report_and_collect_reads_it() {
    let sandbox = Sandbox::new("grok-set");
    sandbox.config(sandbox.base_config());
    let reset = iso(now() + 5.0 * 86400.0);
    let (code, out, err) = sandbox.run(&["grok-set", "55", "--reset", &reset]);
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("Grok: 55% used"), "{out}");
    let report: Value = serde_json::from_str(&sandbox.state_text("grok.json")).expect("grok report");
    assert_eq!(report["pct"], 55.0);
    assert_eq!(report["resets_at"], reset.as_str());
    assert!(report["updated_at"].is_string());

    let (code, _out, err) = sandbox.run(&["grok-set", "--pct", "61", "--resets-at", &reset]);
    assert_eq!(code, 0, "{err}");
    let report: Value = serde_json::from_str(&sandbox.state_text("grok.json")).expect("grok report");
    assert_eq!(report["pct"], 61.0);

    let (code, out, _err) = sandbox.run(&["grok-set", "140"]);
    assert_eq!(code, 2);
    assert!(out.is_empty());
    let (code, _out, _err) = sandbox.run(&["grok-set", "40", "--reset", "not-a-date"]);
    assert_eq!(code, 2);

    let status = collect_with(&sandbox);
    assert_eq!(status.0, 0);
    assert_eq!(status.3["pools"]["grok"]["used_pct"], 61.0);
    assert!(status.3["pools"]["grok"]["state"].is_string());
}

#[test]
fn burn_and_pace_log_read_the_launch_log() {
    let sandbox = Sandbox::new("burn");
    sandbox.config(sandbox.base_config());
    let stamp = now();
    sandbox.write(
        ".local/state/token-ladder/launches.jsonl",
        &format!(
            "{}\n{}\n",
            json!({"at": stamp - 3600.0, "lane": "opencode", "caller": "/tmp/ticket-a", "outcome": "finished"}),
            json!({"at": stamp - 600.0, "lane": "codex", "caller": "/tmp/ticket-b", "outcome": "limited"})
        ),
    );
    sandbox.write(
        ".local/state/token-ladder/status.json",
        json!({"ts": stamp, "pools": {
            "codex": {"state": "ON", "used_pct": 30.0, "elapsed_pct": 25.0, "pace_ratio": 1.2, "window_minutes": 10080},
            "credit:opencode": {"state": "ON", "used_pct": 50.0, "pace_ratio": 0.9}
        }})
        .to_string()
        .as_str(),
    );

    let (code, out, err) = sandbox.run_as_plan_pick(&["--burn"]);
    assert_eq!(code, 0, "{err}");
    assert!(out.starts_with("Burn "), "{out}");
    assert!(out.contains("Per lane"), "{out}");
    assert!(out.contains("opencode: unknown; 0 messages; 0 unpriced; 1 launches"), "{out}");
    assert!(out.contains("codex: unknown; 0 messages; 0 unpriced; 1 launches"), "{out}");
    assert!(out.contains("/tmp/ticket-a: unknown; 0 messages; 0 unpriced; 1 launches"), "{out}");

    let (code, out, err) = sandbox.run_as_plan_pick(&["--burn", "--json"]);
    assert_eq!(code, 0, "{err}");
    let report: Value = serde_json::from_str(&out).expect("burn JSON");
    assert_eq!(report["currency"], "USD");
    assert_eq!(report["by_lane"]["opencode"]["launches"], 1);
    assert!(report["by_caller"].is_object());
    assert_eq!(report["month"].as_str().unwrap_or_default().len(), 7);

    let (code, out, err) = sandbox.run_as_plan_pick(&["--pace-log"]);
    assert_eq!(code, 0, "{err}");
    assert!(out.starts_with("Pace log:"), "{out}");
    assert!(out.contains("codex: 1 launch since"), "{out}");
    assert!(out.contains("pace 1.20x"), "{out}");
    assert!(out.contains("opencode: 1 launch since"), "{out}");

    let (code, out, err) = sandbox.run_as_plan_pick(&["--pace-log", "--json"]);
    assert_eq!(code, 0, "{err}");
    let report: Value = serde_json::from_str(&out).expect("pace log JSON");
    assert_eq!(report["lanes"]["codex"]["launches"], 1);
    assert!(report["launch_log"].as_str().unwrap_or_default().ends_with("launches.jsonl"));
}

#[test]
fn push_posts_the_status_with_a_bearer_token() {
    let sandbox = Sandbox::new("push");
    fixture_grok(&sandbox);
    let server = stub(Reply::Json("{}".to_string()));
    let mut config = sandbox.base_config();
    config["push_url"] = json!(format!("{}/v1/usage", server.url));
    sandbox.config(config);
    let key = sandbox.stub_command("yeo-key", "#!/bin/sh\nprintf 'push-token-value\\n'\n");
    let mut command = sandbox.command(&["collect"]);
    command.env("TOKEN_LADDER_YEO_KEY", &key);
    let output = sandbox.output(&mut command);
    assert_eq!(output.status.code(), Some(0));

    let request = server.last().expect("usage push");
    assert_eq!(request.method, "POST");
    assert_eq!(request.path, "/v1/usage");
    assert_eq!(
        request.headers.get("authorization").map(String::as_str),
        Some("Bearer push-token-value")
    );
    assert_eq!(
        request.headers.get("content-type").map(String::as_str),
        Some("application/json")
    );
    let body: Value = serde_json::from_str(&request.body).expect("pushed status JSON");
    assert!(body["ts"].as_f64().unwrap_or_default() > 0.0);
    assert!(body["pools"]["grok"].is_object());
    assert!(body["config"].is_object());
    let pushed_at = sandbox.status()["pushed_at"].as_f64().unwrap_or_default();
    assert!(pushed_at > 0.0);
}

#[test]
fn push_failure_keeps_the_local_status_and_exits_zero() {
    let sandbox = Sandbox::new("push-error");
    let server = stub(Reply::Status(500));
    let mut config = sandbox.base_config();
    config["push_url"] = json!(format!("{}/v1/usage", server.url));
    sandbox.config(config);
    sandbox.stub_command("yeo-key", "#!/bin/sh\nprintf 'push-token-value\\n'\n");
    let (code, _out, err) = sandbox.run(&["collect"]);
    assert_eq!(code, 0, "{err}");
    assert!(sandbox.state("status.json").exists());
    assert!(!sandbox.state("last_push_ok").exists());
    assert!(sandbox.status()["pushed_at"].is_null());
    assert!(!server.requests().is_empty());

    let mut config = sandbox.base_config();
    config["push_url"] = json!("http://127.0.0.1:1/v1/usage");
    sandbox.config(config);
    let _ = std::fs::remove_file(sandbox.state("push_tried"));
    let (code, _out, err) = sandbox.run(&["collect"]);
    assert_eq!(code, 0, "{err}");
    assert!(sandbox.state("status.json").exists());
    assert!(sandbox.status()["pools"].is_object());
}

#[test]
fn no_secret_reaches_the_status_file_or_the_output() {
    let sandbox = Sandbox::new("secrets");
    fixture_claude(&sandbox);
    sandbox.write(
        ".claude/.credentials.json",
        r#"{"claudeAiOauth":{"accessToken":"claude-secret-token-abc123"}}"#,
    );
    fixture_grok(&sandbox);
    let usage = stub(Reply::Json(claude_usage_reply()));
    let key_server = stub(Reply::Json(
        json!({"data": {"usage": 1.5, "limit": 20.0}}).to_string(),
    ));
    let server = stub(Reply::Json("{}".to_string()));
    let mut config = sandbox.base_config();
    config["push_url"] = json!(format!("{}/v1/usage", server.url));
    sandbox.config(config);
    let key = sandbox.stub_command("yeo-key", "#!/bin/sh\nprintf 'push-secret-token-xyz789\\n'\n");
    let mut command = sandbox.command(&["collect"]);
    command
        .env("TOKEN_LADDER_YEO_KEY", &key)
        .env("TOKEN_LADDER_CLAUDE_USAGE_URL", format!("{}/api/oauth/usage", usage.url))
        .env("TOKEN_LADDER_OPENROUTER_KEY_URL", format!("{}/api/v1/key", key_server.url))
        .env("OPENROUTER_API_KEY", "openrouter-secret-key-qwerty");
    let output = sandbox.output(&mut command);
    assert_eq!(output.status.code(), Some(0));
    let combined = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let status_text = sandbox.state_text("status.json");
    let history = sandbox.state_text("history.jsonl");
    for secret in [
        "claude-secret-token-abc123",
        "push-secret-token-xyz789",
        "openrouter-secret-key-qwerty",
    ] {
        assert!(!combined.contains(secret), "secret leaked to output: {secret}");
        assert!(!status_text.contains(secret), "secret leaked to status.json: {secret}");
        assert!(!history.contains(secret), "secret leaked to history.jsonl: {secret}");
    }
    assert!(sandbox.status()["openrouter"]["usage"].is_number());

    let usage_request = usage.last().expect("claude usage request");
    assert_eq!(
        usage_request.headers.get("authorization").map(String::as_str),
        Some("Bearer claude-secret-token-abc123")
    );
    let push = server.last().expect("usage push");
    assert_eq!(
        push.headers.get("authorization").map(String::as_str),
        Some("Bearer push-secret-token-xyz789")
    );
}

#[test]
fn collect_refreshes_the_due_credit_lanes() {
    let sandbox = Sandbox::new("collect-credits");
    let mut config = sandbox.base_config();
    config["opencode_reader_command"] = json!([sandbox.root.join("opencode-read").display().to_string()]);
    sandbox.config(config);
    sandbox.stub_command(
        "opencode-read",
        "#!/bin/sh\nprintf '{\"credits_usd\": 31.25, \"at\": 1795783606.0}\\n'\n",
    );
    let (code, _out, err) = sandbox.run(&["collect"]);
    assert_eq!(code, 0, "{err}");
    let credits: Value =
        serde_json::from_str(&sandbox.state_text("lane-credits.json")).expect("lane credits");
    assert_eq!(credits["opencode"]["usd"], 31.25);
    assert_eq!(credits["opencode"]["limit_usd"], 40.0);
    let first_at = credits["opencode"]["at"].as_f64().unwrap_or_default();
    assert!(first_at > 0.0);

    let (code, _out, err) = sandbox.run(&["collect"]);
    assert_eq!(code, 0, "{err}");
    let credits: Value =
        serde_json::from_str(&sandbox.state_text("lane-credits.json")).expect("lane credits");
    assert_eq!(credits["opencode"]["at"].as_f64().unwrap_or_default(), first_at);
}

#[test]
fn read_credits_stores_the_balances_quietly_when_a_key_is_missing() {
    let sandbox = Sandbox::new("credits");
    let mut config = sandbox.base_config();
    config["opencode_reader_command"] = json!([sandbox.root.join("opencode-read").display().to_string()]);
    sandbox.config(config);
    sandbox.stub_command(
        "opencode-read",
        "#!/bin/sh\nprintf '{\"credits_usd\": 24.5, \"at\": 1795783606.0}\\n'\n",
    );
    let (code, out, err) = sandbox.run_as_plan_pick(&["--read-credits", "opencode"]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(out.trim(), "opencode $24.50");
    let credits: Value =
        serde_json::from_str(&sandbox.state_text("lane-credits.json")).expect("lane credits");
    assert_eq!(credits["opencode"]["usd"], 24.5);
    assert_eq!(credits["opencode"]["limit_usd"], 40.0);
    assert!(!sandbox.state("lane-credits.json.tmp").exists());

    let (code, _out, err) = sandbox.run_as_plan_pick(&["--read-credits", "vercel"]);
    assert_eq!(code, 0, "{err}");
    assert!(err.contains("plan-pick: cannot read vercel credit"), "{err}");
}

#[test]
fn opencode_usage_export_prints_the_csv_and_reports_a_missing_key() {
    let sandbox = Sandbox::new("export");
    sandbox.config(sandbox.base_config());
    let (code, out, err) = sandbox.run_as_plan_pick(&["--opencode-usage-export"]);
    assert_eq!(code, 1);
    assert!(out.is_empty());
    assert!(err.contains("plan-pick: OPENCODE_API_KEY missing"), "{err}");

    let server = stub(Reply::Json(
        "day,user_id,model,requests,input_tokens,output_tokens,cache_read_tokens,cache_write_5m_tokens,cache_write_1h_tokens,cost_usd\n2026-11-22,u1,gpt,1,10,20,0,0,0,0.25\n".to_string(),
    ));
    let mut command = sandbox.command(&["--plan-pick", "--opencode-usage-export"]);
    command
        .env("OPENCODE_API_KEY", "opencode-usage-key")
        .env("TOKEN_LADDER_OPENCODE_USAGE_URL", format!("{}/usage/export", server.url));
    let output = sandbox.output(&mut command);
    assert_eq!(output.status.code(), Some(0));
    let text = String::from_utf8_lossy(&output.stdout);
    assert!(text.starts_with("day,user_id,model"), "{text}");
    assert!(text.contains("2026-11-22,u1,gpt"), "{text}");
    let request = server.last().expect("usage export request");
    assert_eq!(
        request.headers.get("authorization").map(String::as_str),
        Some("Bearer opencode-usage-key")
    );
}
