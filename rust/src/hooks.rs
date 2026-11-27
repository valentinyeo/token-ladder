use crate::config::{
    trim_float, CreditEntry, Credits, Pool, Status, COMMANDCODE_CREDITS_TO_USD,
    COMMANDCODE_PLAN_CREDITS,
};
use serde_json::Value;
use std::collections::HashMap;

pub const AGY_REFUSAL: &str = "Token ladder: Antigravity is orchestrator-only (chat-only); never run agy workers. Delegate via plan-pick --run to pi-deepseek or codex.";
pub const FAST_REFUSAL: &str =
    "Token ladder: fast mode is off; refuse any model id containing 'fast'.";
pub const BASETEN_REFUSAL: &str = "Token ladder: never Baseten (Valentin 2026-11-19).";
pub const OPENROUTER_REFUSAL: &str =
    "Token ladder: OpenRouter runs only as the openrouter-credit lane plan-pick prints.";
pub const DEEPSEEK_REFUSAL: &str = "no DeepSeek fallback (Valentin 2026-10-25)";
pub const LANE_REFUSAL: &str = "Token ladder: only the lanes plan-pick prints; DeepSeek Flash or pinned GLM Flash on pi, no paid-key fallback.";
pub const CLAUDE_OFF_REFUSAL: &str =
    "Token ladder: Claude pool OFF; wait for its reset, no paid fallback.";

const WRAPPERS: [&str; 8] = [
    "nohup", "setsid", "env", "exec", "nice", "time", "command", "stdbuf",
];
const SHELLS: [&str; 3] = ["bash", "sh", "zsh"];
const MODEL_TOOLS: [&str; 8] = [
    "claude",
    "hax",
    "codex",
    "pi",
    "cursor",
    "cursor-agent",
    "agent",
    "grok",
];
const PI_LANE_PROVIDERS: [&str; 5] = [
    "vercel-ai-gateway",
    "deepseek",
    "opencode-go",
    "commandcode",
    "openrouter-credit",
];
const DEEPSEEK_FLASH_MODELS: [&str; 3] = [
    "deepseek/deepseek-v4.1-flash",
    "deepseek-v4.1-flash",
    "deepseek-flash",
];
const GLM_MODEL: &str = "zai/glm-5.3-flash";
const PINNED_EXTENSION: &str = "/tools/pi-lane-pins.mjs";

pub type Env = HashMap<String, String>;

pub fn process_env() -> Env {
    std::env::vars().collect()
}

pub fn basename(word: &str) -> &str {
    word.rsplit('/').next().unwrap_or(word)
}

fn is_word_char(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'$'
}

pub fn shell_commands(cmd: &str) -> Vec<(Vec<String>, Env)> {
    let lines: Vec<&str> = cmd.split('\n').collect();
    let mut kept: Vec<String> = Vec::new();
    let mut index = 0;
    while index < lines.len() {
        kept.push(lines[index].to_string());
        if let Some(delimiter) = heredoc_delimiter(lines[index]) {
            index += 1;
            while index < lines.len() && lines[index].trim() != delimiter {
                index += 1;
            }
        }
        index += 1;
    }
    let text = kept.join("\n");
    let mut commands: Vec<Vec<String>> = Vec::new();
    let mut words: Vec<String> = Vec::new();
    let bytes = text.as_bytes();
    let mut position = 0;
    while position < bytes.len() {
        let byte = bytes[position];
        if matches!(byte, b' ' | b'\t' | b'\r') {
            position += 1;
            continue;
        }
        if matches!(byte, b';' | b'&' | b'|' | b'(' | b')' | b'<' | b'>' | b'\n') {
            if !words.is_empty() {
                commands.push(std::mem::take(&mut words));
            }
            position += 1;
            continue;
        }
        let start = position;
        let mut word = String::new();
        while position < bytes.len() {
            let byte = bytes[position];
            if matches!(byte, b' ' | b'\t' | b'\r' | b'\n' | b';' | b'&' | b'|' | b'(' | b')' | b'<' | b'>')
            {
                break;
            }
            if byte == b'\\' {
                if position + 1 < bytes.len() {
                    word.push(bytes[position + 1] as char);
                    position += 2;
                } else {
                    position += 1;
                }
                continue;
            }
            if byte == b'\'' {
                position += 1;
                while position < bytes.len() && bytes[position] != b'\'' {
                    word.push(bytes[position] as char);
                    position += 1;
                }
                position += 1;
                continue;
            }
            if byte == b'"' {
                position += 1;
                while position < bytes.len() && bytes[position] != b'"' {
                    if bytes[position] == b'\\' && position + 1 < bytes.len() {
                        word.push(bytes[position + 1] as char);
                        position += 2;
                        continue;
                    }
                    word.push(bytes[position] as char);
                    position += 1;
                }
                position += 1;
                continue;
            }
            if byte == b'`' {
                position += 1;
                while position < bytes.len() && bytes[position] != b'`' {
                    word.push(bytes[position] as char);
                    position += 1;
                }
                position += 1;
                continue;
            }
            word.push(byte as char);
            position += 1;
        }
        if position == start {
            position += 1;
            continue;
        }
        words.push(word);
    }
    if !words.is_empty() {
        commands.push(words);
    }
    let mut result = Vec::new();
    for mut words in commands {
        let mut assigned: Env = Env::new();
        while !words.is_empty() {
            let word = words[0].clone();
            if let Some((key, value)) = assignment(&word) {
                assigned.insert(key, value);
                words.remove(0);
            } else if WRAPPERS.contains(&basename(&word)) {
                words.remove(0);
            } else if word == "timeout" {
                words.drain(0..words.len().min(2));
            } else if word.starts_with('-') {
                words.remove(0);
            } else {
                break;
            }
        }
        if !words.is_empty() {
            result.push((words, assigned.into_iter().collect()));
        }
    }
    result
}

fn assignment(word: &str) -> Option<(String, String)> {
    let (key, value) = word.split_once('=')?;
    if key.is_empty() || !key.bytes().all(is_word_char) || key.as_bytes()[0].is_ascii_digit() {
        return None;
    }
    Some((key.to_string(), value.to_string()))
}

fn heredoc_delimiter(line: &str) -> Option<String> {
    let index = line.find("<<")?;
    let rest = &line[index + 2..];
    let rest = rest.strip_prefix('-').unwrap_or(rest);
    let rest = rest.trim_start();
    let rest = rest.trim_start_matches(['\'', '"']);
    let delimiter: String = rest
        .chars()
        .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
        .collect();
    if delimiter.is_empty() {
        None
    } else {
        Some(delimiter)
    }
}

fn agy_prompts(words: &[String]) -> Vec<String> {
    let mut prompts = Vec::new();
    for (index, word) in words.iter().enumerate().skip(1) {
        if word == "--print" || word == "-p" {
            prompts.push(words.get(index + 1).cloned().unwrap_or_default());
        } else if let Some(value) = word
            .strip_prefix("--print=")
            .or_else(|| word.strip_prefix("-p="))
        {
            prompts.push(value.to_string());
        } else if let Some(value) = word.strip_prefix("-p") {
            if !value.is_empty() {
                prompts.push(value.to_string());
            }
        }
    }
    prompts
}

fn flag_values(words: &[String], long: &str, shorts: &[&str]) -> Vec<String> {
    let mut values = Vec::new();
    for (index, word) in words.iter().enumerate().skip(1) {
        if word == long && index + 1 < words.len() {
            values.push(words[index + 1].clone());
            continue;
        }
        if let Some(value) = word.strip_prefix(&format!("{long}=")) {
            values.push(value.to_string());
            continue;
        }
        for short in shorts {
            if word == short && index + 1 < words.len() {
                values.push(words[index + 1].clone());
            } else if let Some(value) = word.strip_prefix(&format!("{short}=")) {
                values.push(value.to_string());
            } else if let Some(value) = word.strip_prefix(short) {
                if !value.is_empty() && *short == "-m" {
                    values.push(value.to_string());
                }
            }
        }
    }
    values
}

fn model_assignments(words: &[String]) -> Vec<String> {
    let mut values = Vec::new();
    for word in words.iter().skip(1) {
        let trimmed = word.trim_start();
        if let Some(rest) = trimmed.strip_prefix("model") {
            let rest = rest.trim_start();
            if let Some(value) = rest.strip_prefix('=') {
                values.push(value.trim_matches(['"', '\'']).to_string());
            }
        }
    }
    values
}

fn is_deepseek_flash(model: &str) -> bool {
    let lowered = model.to_ascii_lowercase();
    DEEPSEEK_FLASH_MODELS
        .iter()
        .any(|known| lowered.starts_with(known))
}

fn pinned_glm(words: &[String], models: &[String], providers: &[String]) -> bool {
    if models != [GLM_MODEL.to_string()] || providers != ["vercel-ai-gateway".to_string()] {
        return false;
    }
    words.iter().enumerate().any(|(index, word)| {
        matches!(word.as_str(), "-e" | "--extension")
            && words
                .get(index + 1)
                .is_some_and(|path| path.ends_with(PINNED_EXTENSION))
    })
}

pub fn bash_refusal(cmd: &str, environment: &Env) -> Option<String> {
    for (words, assigned) in shell_commands(cmd) {
        let mut env = environment.clone();
        env.extend(assigned);
        let exe = basename(&words[0]);
        if SHELLS.contains(&exe) {
            let mut index = 1;
            let mut has_command = false;
            while index < words.len() {
                let word = &words[index];
                if word == "--" {
                    index += 1;
                    break;
                }
                if !word.starts_with('-') || word == "-" {
                    break;
                }
                if !word.starts_with("--") && word.contains('c') {
                    has_command = true;
                }
                index += 1;
            }
            if has_command {
                if let Some(inner) = words.get(index) {
                    if let Some(reason) = bash_refusal(inner, &env) {
                        return Some(reason);
                    }
                }
            }
        }
        if exe == "agy" && agy_prompts(&words).iter().any(|prompt| prompt != "/usage") {
            return Some(AGY_REFUSAL.to_string());
        }
        if (exe == "cc-deepseek-apikey" || exe == "ccr")
            && env.get("TOKEN_LADDER_DEEPSEEK_OK").map(String::as_str) != Some("1")
        {
            return Some(DEEPSEEK_REFUSAL.to_string());
        }
        if !MODEL_TOOLS.contains(&exe) {
            continue;
        }
        let mut models = flag_values(&words, "--model", &["-m"]);
        models.extend(flag_values(&words, "--model-id", &[]));
        models.extend(model_assignments(&words));
        let providers: Vec<String> = flag_values(&words, "--provider", &[])
            .into_iter()
            .map(|value| value.to_ascii_lowercase())
            .collect();
        if models
            .iter()
            .any(|model| model.to_ascii_lowercase().contains("fast"))
        {
            return Some(FAST_REFUSAL.to_string());
        }
        if words
            .iter()
            .skip(1)
            .any(|word| word.to_ascii_lowercase().contains("baseten"))
        {
            return Some(BASETEN_REFUSAL.to_string());
        }
        if providers.iter().any(|provider| provider == "openrouter")
            && env.get("TOKEN_LADDER_OPENROUTER_OK").map(String::as_str) != Some("1")
        {
            return Some(OPENROUTER_REFUSAL.to_string());
        }
        let deepseek_only =
            exe == "pi" && !models.is_empty() && models.iter().all(|model| is_deepseek_flash(model));
        let glm_lane = exe == "pi" && pinned_glm(&words, &models, &providers);
        let api_key = words
            .iter()
            .skip(1)
            .any(|word| word == "--api-key" || word.starts_with("--api-key="));
        let unapproved_provider = providers.iter().any(|provider| {
            provider != "codex"
                && provider != "openrouter"
                && !(PI_LANE_PROVIDERS.contains(&provider.as_str())
                    && (deepseek_only || glm_lane))
        });
        if api_key || unapproved_provider {
            return Some(LANE_REFUSAL.to_string());
        }
    }
    None
}

fn tool_input_field<'a>(input: &'a Value, key: &str) -> Option<&'a Value> {
    input.get("tool_input").and_then(|value| value.get(key))
}

pub fn agent_refusal(tool_input: &Value, status: &Status) -> Option<String> {
    if let Some(model) = tool_input_field(tool_input, "model") {
        let text = match model {
            Value::String(text) => text.clone(),
            other => other.to_string(),
        };
        if text.to_ascii_lowercase().contains("fast") {
            return Some(FAST_REFUSAL.to_string());
        }
    }
    let pool = status.claude_pool();
    if pool.is_some_and(Pool::is_off) {
        return Some(CLAUDE_OFF_REFUSAL.to_string());
    }
    None
}

pub fn command_refusal(tool_input: &Value, environment: &Env) -> Option<String> {
    let command = tool_input_field(tool_input, "command")?.as_str()?;
    bash_refusal(command, environment)
}

pub fn age_text(seconds: f64) -> String {
    let minutes = (seconds.max(0.0) / 60.0) as i64;
    if minutes < 60 {
        return format!("{minutes} min");
    }
    let (hours, rest) = (minutes / 60, minutes % 60);
    if rest == 0 {
        format!("{hours} h")
    } else {
        format!("{hours} h {rest} min")
    }
}

fn week_part(label: &str, pool: Option<&Pool>, week_used: Option<f64>, bonus: Option<&str>) -> Option<String> {
    let used = pool
        .and_then(|pool| pool.used_pct)
        .or(week_used)?;
    let estimated = pool.is_some_and(|pool| pool.estimated);
    let mark = if estimated { " est" } else { "" };
    match bonus {
        Some(bonus) => Some(format!("{label} {}% wk{mark} ({bonus})", trim_float(used))),
        None => Some(format!("{label} {}% wk{mark}", trim_float(used))),
    }
}

pub fn bonus_reset_note(status: &Status, plan: &str, now: i64) -> Option<String> {
    let config = status.config.as_ref()?;
    let label = crate::config::plan_label(plan);
    let entry = status.burn.as_ref().and_then(|burn| burn.entry(plan));
    let hours = entry
        .and_then(|entry| entry.reset_hours)
        .or_else(|| config.bonus_reset_hours(plan, now))?;
    let burn_now = entry
        .map(|entry| entry.burn_now)
        .unwrap_or(hours <= config.reset_burn_hours);
    if burn_now {
        return Some(format!("{label} bonus reset in {hours:.0}h (burn now)"));
    }
    Some(format!("{label} bonus reset in {hours:.0}h"))
}

fn google_part(pool: Option<&Pool>) -> Option<String> {
    let pool = pool?;
    let gemini = pool.gemini.as_ref();
    let weekly_left = gemini.and_then(|gemini| gemini.weekly_remaining_pct);
    let used = match weekly_left {
        Some(left) => 100.0 - left,
        None => pool.used_pct?,
    };
    let five = match gemini.and_then(|gemini| gemini.five_hour_remaining_pct) {
        Some(left) => Some(100.0 - left),
        None => pool.five_hour_used_pct,
    };
    let mut part = format!("Antigravity Gemini {}% wk", trim_float(used));
    if let Some(five) = five {
        part.push_str(&format!(", 5h {}%", trim_float(five)));
    }
    if pool.estimated {
        part.push_str(" est");
    }
    if pool.chat_only {
        part.push_str(" (chat-only)");
    }
    Some(part)
}

fn credit_part(vendor: &str, entry: Option<&CreditEntry>) -> Option<String> {
    let entry = entry?;
    let usd = entry.usd;
    let credits = entry.credits;
    match vendor {
        "opencode" => usd.map(|usd| format!("OpenCode credit ${usd:.2}")),
        "vercel" => usd.map(|usd| format!("Vercel Token Pass ${usd:.0} left")),
        "commandcode" => credits.map(|credits| {
            let used_pct = (COMMANDCODE_PLAN_CREDITS - credits) * 100.0 / COMMANDCODE_PLAN_CREDITS;
            format!("CommandCode {used_pct:.1}% mo")
        }),
        _ => None,
    }
}

pub fn prompt_usage_line(status: &Status, credits: &Credits, now: i64) -> Option<String> {
    let config = status.config.as_ref()?;
    let mut parts: Vec<String> = Vec::new();
    let mut seen: Vec<&str> = Vec::new();
    for plan in &config.plans {
        let Some(vendor) = plan.vendor.as_deref() else {
            continue;
        };
        if !matches!(
            vendor,
            "anthropic" | "openai" | "google" | "opencode" | "vercel" | "commandcode"
        ) {
            continue;
        }
        if seen.contains(&vendor) {
            continue;
        }
        seen.push(vendor);
        let part = match vendor {
            "anthropic" | "openai" => {
                let (label, key) = if vendor == "anthropic" {
                    ("Claude", "claude")
                } else {
                    ("Codex", "codex")
                };
                let week_used = status
                    .account(key)
                    .and_then(|read| read.week.as_ref())
                    .and_then(|week| week.used);
                let bonus = bonus_reset_note(status, key, now);
                week_part(label, status.pool(key), week_used, bonus.as_deref())
            }
            "google" => google_part(status.pool("google")),
            other => credit_part(other, credits.get(other)),
        };
        if let Some(part) = part {
            parts.push(part);
        }
    }
    if parts.is_empty() {
        return None;
    }
    let head = match status.age_seconds(now) {
        Some(age) => format!("Plan usage (token-ladder, {} old)", age_text(age)),
        None => "Plan usage (token-ladder)".to_string(),
    };
    Some(format!(
        "{head}: {}. Source: plan-pick --plans.",
        parts.join(" \u{b7} ")
    ))
}

pub fn commandcode_pool_note(credits: f64) -> String {
    let usd = credits * COMMANDCODE_CREDITS_TO_USD;
    format!(
        "{}/{} monthly credits left (~${usd:.2} Flash usage)",
        trim_float(credits),
        trim_float(COMMANDCODE_PLAN_CREDITS)
    )
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use crate::config::{Config, PlanSpec, PoolGroup, Pools};
    use serde_json::json;

    fn env() -> Env {
        Env::new()
    }

    fn env_with(pairs: &[(&str, &str)]) -> Env {
        pairs
            .iter()
            .map(|(key, value)| (key.to_string(), value.to_string()))
            .collect()
    }

    fn refuses(cmd: &str) -> String {
        bash_refusal(cmd, &env()).unwrap_or_else(|| panic!("expected a refusal for {cmd}"))
    }

    #[test]
    fn shell_commands_split_words_quotes_and_operators() {
        let commands = shell_commands("pi -p --model deepseek-v4.1-flash \"$(cat brief.txt)\" </dev/null; echo done");
        assert_eq!(commands[0].0[0], "pi");
        assert_eq!(commands[0].0[3], "deepseek-v4.1-flash");
        assert_eq!(commands[0].0[4], "$(cat brief.txt)");
        assert_eq!(commands.last().unwrap().0[0], "echo");
    }

    #[test]
    fn shell_commands_strip_wrappers_timeouts_and_assignments() {
        let commands = shell_commands("FOO=1 nohup timeout 60 pi -p --model deepseek-v4.1-flash hi");
        assert_eq!(commands[0].0[0], "pi");
        assert_eq!(commands[0].1.get("FOO"), Some(&"1".to_string()));
        let commands = shell_commands("env A=1 command pi -p hi");
        assert_eq!(commands[0].0[0], "pi");
        assert_eq!(commands[0].1.get("A"), Some(&"1".to_string()));
    }

    #[test]
    fn shell_commands_skip_heredoc_bodies() {
        let commands = shell_commands("cat <<EOF\nagy --print edit\nEOF\necho done");
        assert!(commands.iter().all(|(words, _)| words[0] != "agy"));
        assert_eq!(commands[0].0[0], "cat");
        assert_eq!(commands.last().unwrap().0[0], "echo");
    }

    #[test]
    fn agy_print_worker_is_refused_but_usage_is_allowed() {
        assert_eq!(refuses("agy --print \"edit\""), AGY_REFUSAL);
        assert_eq!(refuses("agy -p edit"), AGY_REFUSAL);
        assert_eq!(refuses("agy --print=edit"), AGY_REFUSAL);
        assert_eq!(refuses("agy -pedit"), AGY_REFUSAL);
        assert!(bash_refusal("agy --print /usage", &env()).is_none());
        assert!(bash_refusal("agy /usage", &env()).is_none());
        assert!(bash_refusal("agy --print=/usage", &env()).is_none());
    }

    #[test]
    fn nested_shells_are_checked_recursively_with_option_terminators() {
        assert_eq!(refuses("bash -c -- 'agy --print edit'"), AGY_REFUSAL);
        assert_eq!(refuses("bash -c 'agy --print edit'"), AGY_REFUSAL);
        assert_eq!(refuses("sh -c -e -- 'agy -p edit'"), AGY_REFUSAL);
        assert_eq!(refuses("zsh -c 'bash -c \"agy -p edit\"'"), AGY_REFUSAL);
        assert!(bash_refusal("bash -c 'agy --print /usage'", &env()).is_none());
        assert!(bash_refusal("bash --login -c 'echo ok'", &env()).is_none());
        assert!(bash_refusal("bash -lc 'echo ok'", &env()).is_none());
        assert!(bash_refusal("sh -c 'exit 0'", &env()).is_none());
    }

    #[test]
    fn baseten_and_fast_models_are_refused() {
        assert_eq!(refuses("pi -p --provider baseten --model x"), BASETEN_REFUSAL);
        assert_eq!(refuses("pi -p --model x-fast hi"), FAST_REFUSAL);
        assert_eq!(refuses("pi -p --model=fast-model hi"), FAST_REFUSAL);
        assert_eq!(refuses("codex --model gpt-fast"), FAST_REFUSAL);
        assert_eq!(refuses("pi model=x-fast"), FAST_REFUSAL);
    }

    #[test]
    fn deepseek_fallback_and_ccr_need_the_ok_variable() {
        assert_eq!(refuses("ccr hi"), DEEPSEEK_REFUSAL);
        assert_eq!(refuses("cc-deepseek-apikey hi"), DEEPSEEK_REFUSAL);
        assert!(bash_refusal(
            "ccr hi",
            &env_with(&[("TOKEN_LADDER_DEEPSEEK_OK", "1")])
        )
        .is_none());
        assert!(bash_refusal(
            "TOKEN_LADDER_DEEPSEEK_OK=1 cc-deepseek-apikey hi",
            &env()
        )
        .is_none());
    }

    #[test]
    fn openrouter_and_api_keys_are_refused_without_the_ok_variable() {
        assert_eq!(
            refuses("pi -p --provider openrouter --model deepseek/deepseek-v4.1-flash hi"),
            OPENROUTER_REFUSAL
        );
        assert!(bash_refusal(
            "pi -p --provider openrouter --model deepseek/deepseek-v4.1-flash hi",
            &env_with(&[("TOKEN_LADDER_OPENROUTER_OK", "1")])
        )
        .is_none());
        assert_eq!(
            refuses("pi -p --api-key sk-secret --model deepseek-v4.1-flash hi"),
            LANE_REFUSAL
        );
    }

    #[test]
    fn valid_pi_deepseek_and_pinned_glm_commands_are_allowed() {
        assert!(bash_refusal(
            "~/.npm-global/bin/pi -p --mode json --thinking medium --provider opencode-go --model deepseek-v4.1-flash \"$(cat brief.txt)\" </dev/null",
            &env()
        )
        .is_none());
        assert!(bash_refusal(
            "pi -p --provider vercel-ai-gateway --model deepseek/deepseek-v4.1-flash hi",
            &env()
        )
        .is_none());
        assert!(bash_refusal(
            "pi -p -e ~/projects/token-ladder/tools/pi-lane-pins.mjs --provider vercel-ai-gateway --model zai/glm-5.3-flash hi",
            &env()
        )
        .is_none());
        assert_eq!(
            refuses("pi -p --provider opencode-go --model zai/glm-5.3-flash hi"),
            LANE_REFUSAL
        );
        assert_eq!(
            refuses("pi -p --provider some-paid-key --model deepseek-v4.1-flash hi"),
            LANE_REFUSAL
        );
        assert!(bash_refusal("ls -la", &env()).is_none());
        assert!(bash_refusal("claude --model sonnet hi", &env()).is_none());
    }

    #[test]
    fn command_refusal_reads_the_tool_input_shape() {
        let input = json!({"tool_input": {"command": "agy --print edit"}});
        assert_eq!(command_refusal(&input, &env()), Some(AGY_REFUSAL.to_string()));
        let input = json!({"tool_input": {"command": "ls"}});
        assert!(command_refusal(&input, &env()).is_none());
        let input = json!({"tool_input": {}});
        assert!(command_refusal(&input, &env()).is_none());
        let input = json!({});
        assert!(command_refusal(&input, &env()).is_none());
    }

    fn status_with_claude(state: &str) -> Status {
        Status {
            pools: Pools::from([(
                "claude".to_string(),
                Pool {
                    state: Some(state.to_string()),
                    used_pct: Some(if state == "OFF" { 100.0 } else { 40.0 }),
                    ..Pool::default()
                },
            )]),
            ..Status::default()
        }
    }

    #[test]
    fn agent_refusal_blocks_fast_models_and_an_off_claude_pool() {
        let status = status_with_claude("ON");
        let input = json!({"tool_input": {"model": "claude-sonnet-fast"}});
        assert_eq!(agent_refusal(&input, &status), Some(FAST_REFUSAL.to_string()));
        let input = json!({"tool_input": {"model": "claude-sonnet"}});
        assert!(agent_refusal(&input, &status).is_none());
        let off = status_with_claude("OFF");
        assert_eq!(
            agent_refusal(&input, &off),
            Some(CLAUDE_OFF_REFUSAL.to_string())
        );
        let input = json!({"tool_input": {}});
        assert!(agent_refusal(&input, &status).is_none());
    }

    fn usage_status() -> Status {
        let mut pools = Pools::new();
        pools.insert(
            "claude".to_string(),
            Pool {
                state: Some("ON".to_string()),
                used_pct: Some(42.0),
                ..Pool::default()
            },
        );
        pools.insert(
            "codex".to_string(),
            Pool {
                state: Some("ON".to_string()),
                used_pct: Some(56.0),
                ..Pool::default()
            },
        );
        pools.insert(
            "google".to_string(),
            Pool {
                state: Some("ON".to_string()),
                used_pct: Some(49.0),
                chat_only: true,
                gemini: Some(PoolGroup {
                    weekly_remaining_pct: Some(51.0),
                    five_hour_remaining_pct: Some(97.0),
                    ..PoolGroup::default()
                }),
                ..Pool::default()
            },
        );
        let config = Config {
            plans: vec![
                PlanSpec {
                    name: "Claude".to_string(),
                    vendor: Some("anthropic".to_string()),
                    ..PlanSpec::default()
                },
                PlanSpec {
                    name: "ChatGPT".to_string(),
                    vendor: Some("openai".to_string()),
                    ..PlanSpec::default()
                },
                PlanSpec {
                    name: "Antigravity".to_string(),
                    vendor: Some("google".to_string()),
                    ..PlanSpec::default()
                },
                PlanSpec {
                    name: "Vercel".to_string(),
                    vendor: Some("vercel".to_string()),
                    credit: Some("vercel".to_string()),
                    ..PlanSpec::default()
                },
                PlanSpec {
                    name: "CommandCode".to_string(),
                    vendor: Some("commandcode".to_string()),
                    credit: Some("commandcode".to_string()),
                    ..PlanSpec::default()
                },
            ],
            ..Config::default()
        };
        Status {
            ts: Some(1_795_785_000.0),
            pools,
            config: Some(config),
            ..Status::default()
        }
    }

    #[test]
    fn prompt_usage_line_tags_antigravity_as_chat_only() {
        let mut credits = Credits::new();
        credits.insert(
            "vercel".to_string(),
            CreditEntry {
                usd: Some(14.0),
                ..CreditEntry::default()
            },
        );
        credits.insert(
            "commandcode".to_string(),
            CreditEntry {
                credits: Some(60.7),
                ..CreditEntry::default()
            },
        );
        let line = prompt_usage_line(&usage_status(), &credits, 1_795_786_800).unwrap();
        assert!(line.starts_with("Plan usage (token-ladder, 30 min old): "), "{line}");
        assert!(line.contains("Claude 42% wk"), "{line}");
        assert!(line.contains("Codex 56% wk"), "{line}");
        assert!(
            line.contains("Antigravity Gemini 49% wk, 5h 3% (chat-only)"),
            "{line}"
        );
        assert!(line.contains("Vercel Token Pass $14 left"), "{line}");
        assert!(line.contains("CommandCode 13.3% mo"), "{line}");
        assert!(line.ends_with(". Source: plan-pick --plans."), "{line}");
        assert_eq!(line.matches(" \u{b7} ").count(), 4);
    }

    #[test]
    fn prompt_usage_line_is_absent_without_plan_readings() {
        let status = Status {
            config: Some(Config::default()),
            ..Status::default()
        };
        assert!(prompt_usage_line(&status, &Credits::new(), 0).is_none());
        assert!(prompt_usage_line(&Status::default(), &Credits::new(), 0).is_none());
    }

    #[test]
    fn age_text_reads_like_the_python_line() {
        assert_eq!(age_text(0.0), "0 min");
        assert_eq!(age_text(59.0), "0 min");
        assert_eq!(age_text(1800.0), "30 min");
        assert_eq!(age_text(7200.0), "2 h");
        assert_eq!(age_text(7500.0), "2 h 5 min");
    }

    #[test]
    fn commandcode_credit_note_uses_the_plan_ratio() {
        assert_eq!(
            commandcode_pool_note(60.7084257934),
            "60.7084257934/70 monthly credits left (~$86.73 Flash usage)"
        );
    }

}
