// SPDX-License-Identifier: Apache-2.0
//! COSMIC panel applet for icloud-sync.
//!
//! Shows the state of the `icloud-autosync` systemd user service in the panel
//! (up to date / syncing / error / paused) and a popup with the last run's
//! result, progress of a running sync, and the next scheduled run. Everything
//! is read from systemd, the journal and `.sync_state.json`, so the sync
//! itself needs no changes.

use chrono::{DateTime, Local};
use cosmic::{
    app::{self, Core},
    applet::{menu_button, padded_control},
    cosmic_theme::Spacing,
    iced::{
        widget::{column, row},
        window, Alignment, Length, Subscription,
    },
    surface, theme,
    widget::{divider, space, text},
    Element, Task,
};
use std::{
    path::PathBuf,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::process::Command;

const ID: &str = "io.github.icloudsync.Applet";
const SERVICE: &str = "icloud-autosync.service";
const TIMER: &str = "icloud-autosync.timer";
const POLL: Duration = Duration::from_secs(3);

fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default()
}

/// Same lookup as sync_engine.py: environment variable, then ~/.config/icloud-sync/config.
fn setting(key: &str) -> Option<PathBuf> {
    if let Some(value) = std::env::var_os(key) {
        return Some(PathBuf::from(value));
    }
    let config = std::fs::read_to_string(home().join(".config/icloud-sync/config")).ok()?;
    config.lines().find_map(|line| {
        let (k, v) = line.trim().split_once('=')?;
        let v = v.trim().trim_matches(|c| c == '"' || c == '\'');
        (k.trim() == key).then(|| match v.strip_prefix("~/") {
            Some(rest) => home().join(rest),
            None => PathBuf::from(v),
        })
    })
}

fn sync_dir() -> PathBuf {
    setting("ICLOUD_SYNC_DIR").unwrap_or_else(|| home().join("Documents/icloud-drive"))
}

fn state_file() -> PathBuf {
    setting("ICLOUD_SYNC_STATE")
        .unwrap_or_else(|| home().join("Documents/icloud-sync/.sync_state.json"))
}

fn main() -> cosmic::iced::Result {
    tracing_subscriber::fmt::init();
    cosmic::applet::run::<Applet>(())
}

/// What the last (or current) run printed, parsed from its journal output.
#[derive(Debug, Clone, Default, PartialEq)]
struct RunInfo {
    phase: Option<&'static str>,
    /// Latest "Downloads: 3/10" style progress line: (label, done, total).
    progress: Option<(&'static str, u64, u64)>,
    downloaded: Option<u64>,
    uploaded: Option<u64>,
    deleted_remote: Option<u64>,
    deleted_local: Option<u64>,
    errors: Option<u64>,
    /// Last meaningful line, shown when the run failed.
    last_line: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq)]
struct Status {
    loaded: bool,
    running: bool,
    /// systemd `Result=` of the last run ("success", "exit-code", ...).
    result: String,
    started: Option<i64>,
    finished: Option<i64>,
    timer_active: bool,
    next_run: Option<i64>,
    run: RunInfo,
    /// From `.sync_state.json`; only refreshed when a run finishes or the popup opens.
    last_sync: Option<i64>,
    synced_files: Option<usize>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Health {
    Ok,
    Syncing,
    Error,
    Paused,
}

impl Status {
    fn health(&self) -> Health {
        if self.running {
            Health::Syncing
        } else if self.loaded && self.result != "success" && !self.result.is_empty() {
            Health::Error
        } else if self.run.errors.unwrap_or(0) > 0 {
            Health::Error
        } else if self.loaded && !self.timer_active {
            Health::Paused
        } else {
            Health::Ok
        }
    }
}

impl Health {
    fn icon(self) -> &'static str {
        match self {
            Health::Ok => "io.github.icloudsync.Applet-ok-symbolic",
            Health::Syncing => "io.github.icloudsync.Applet-syncing-symbolic",
            Health::Error => "io.github.icloudsync.Applet-error-symbolic",
            Health::Paused => "io.github.icloudsync.Applet-paused-symbolic",
        }
    }
}

#[derive(Debug, Clone)]
enum Message {
    TogglePopup,
    CloseRequested(window::Id),
    Tick,
    Refreshed(Status),
    SyncNow,
    ToggleTimer,
    OpenFolder,
    ShowLog,
    CommandDone(Result<(), String>),
    #[allow(dead_code)]
    Surface(surface::Action),
}

#[derive(Default)]
struct Applet {
    core: Core,
    popup: Option<window::Id>,
    status: Status,
    refreshing: bool,
    /// Error from the last button action (e.g. systemctl failed).
    action_error: Option<String>,
}

async fn run(cmd: &str, args: &[&str]) -> Result<String, String> {
    let out = Command::new(cmd)
        .args(args)
        .output()
        .await
        .map_err(|e| format!("could not run {cmd}: {e}"))?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    } else {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_string();
        Err(if err.is_empty() { format!("{cmd} exited with {}", out.status) } else { err })
    }
}

fn parse_props(out: &str) -> impl Fn(&str) -> String + '_ {
    move |key: &str| {
        out.lines()
            .find_map(|l| l.strip_prefix(key).and_then(|r| r.strip_prefix('=')))
            .unwrap_or("")
            .to_string()
    }
}

/// `--timestamp=unix` prints "@1791949029"; empty or "n/a" when unset.
fn parse_unix(v: &str) -> Option<i64> {
    v.strip_prefix('@').and_then(|s| s.parse().ok()).filter(|&t| t > 0)
}

/// Value after "<label>:" on a summary line, e.g. "  Downloaded:   3".
fn summary_value(line: &str, label: &str) -> Option<u64> {
    line.trim().strip_prefix(label)?.trim().parse().ok()
}

fn parse_journal(text: &str) -> RunInfo {
    let mut info = RunInfo::default();
    for line in text.lines() {
        if line.contains("Authenticating with iCloud") {
            info.phase = Some("Connecting to iCloud");
        } else if line.contains("Phase 1:") {
            info.phase = Some("Scanning iCloud Drive");
        } else if line.contains("Phase 2:") {
            info.phase = Some("Syncing files");
        }

        // Progress lines are redrawn with \r, so one journal line can hold many
        // updates; the right-most one is the latest.
        let mut best: Option<(usize, (&'static str, u64, u64))> = None;
        for label in ["Downloads", "Uploads", "Deletes"] {
            let needle = format!("{label}: ");
            if let Some(pos) = line.rfind(&needle) {
                let rest = &line[pos + needle.len()..];
                let frac = rest.split_whitespace().next().unwrap_or("");
                if let Some((d, t)) = frac.split_once('/') {
                    if let (Ok(d), Ok(t)) = (d.parse(), t.parse()) {
                        if best.map_or(true, |(p, _)| pos > p) {
                            best = Some((pos, (label, d, t)));
                        }
                    }
                }
            }
        }
        if let Some((_, p)) = best {
            info.progress = Some(p);
        }

        if let Some(v) = summary_value(line, "Downloaded:") {
            info.downloaded = Some(v);
        } else if let Some(v) = summary_value(line, "Uploaded:") {
            info.uploaded = Some(v);
        } else if let Some(v) = summary_value(line, "Deleted from iCloud:") {
            info.deleted_remote = Some(v);
        } else if let Some(v) = summary_value(line, "Deleted locally:") {
            info.deleted_local = Some(v);
        } else if let Some(v) = summary_value(line, "Errors:") {
            info.errors = Some(v);
        }

        let t = line.trim();
        if !t.is_empty() && !t.starts_with("===") {
            info.last_line = Some(t.chars().take(200).collect());
        }
    }
    info
}

fn read_state() -> (Option<i64>, Option<usize>) {
    let Ok(raw) = std::fs::read(state_file()) else {
        return (None, None);
    };
    // The sync rewrites this file in place, so a read can catch it half-written.
    let Ok(v) = serde_json::from_slice::<serde_json::Value>(&raw) else {
        return (None, None);
    };
    (
        v.get("last_sync").and_then(|t| t.as_f64()).map(|t| t as i64),
        v.get("synced_files").and_then(|f| f.as_array()).map(|a| a.len()),
    )
}

async fn fetch_status(prev: Status, reload_state: bool) -> Status {
    let mut s = Status { loaded: true, ..Default::default() };

    let svc = run(
        "systemctl",
        &[
            "--user", "show", SERVICE, "--timestamp=unix",
            "-p", "ActiveState,Result,InvocationID,ExecMainStartTimestamp,ExecMainExitTimestamp",
        ],
    )
    .await
    .unwrap_or_default();
    let get = parse_props(&svc);
    let active = get("ActiveState");
    s.running = active == "activating" || active == "active" || active == "deactivating";
    s.result = get("Result");
    s.started = parse_unix(&get("ExecMainStartTimestamp"));
    s.finished = parse_unix(&get("ExecMainExitTimestamp"));

    let invocation = get("InvocationID");
    if !invocation.is_empty() {
        let filter = format!("_SYSTEMD_INVOCATION_ID={invocation}");
        if let Ok(log) =
            run("journalctl", &["--user", &filter, "-o", "cat", "--no-pager", "-n", "400"]).await
        {
            s.run = parse_journal(&log);
        }
    }

    s.timer_active = run("systemctl", &["--user", "is-active", TIMER])
        .await
        .map(|o| o.trim() == "active")
        .unwrap_or(false);
    if s.timer_active {
        if let Ok(json) = run("systemctl", &["--user", "list-timers", TIMER, "-o", "json"]).await {
            s.next_run = serde_json::from_str::<serde_json::Value>(&json)
                .ok()
                .and_then(|v| v.get(0)?.get("next")?.as_i64())
                .map(|us| us / 1_000_000)
                .filter(|&t| t > 0);
        }
    }

    let run_finished = prev.running && !s.running;
    if reload_state || run_finished || !prev.loaded {
        let (last, count) = tokio::task::spawn_blocking(read_state).await.unwrap_or((None, None));
        s.last_sync = last.or(prev.last_sync);
        s.synced_files = count.or(prev.synced_files);
    } else {
        s.last_sync = prev.last_sync;
        s.synced_files = prev.synced_files;
    }
    s
}

fn now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

fn clock(ts: i64) -> String {
    DateTime::from_timestamp(ts, 0)
        .map(|d| {
            let local = d.with_timezone(&Local);
            if local.date_naive() == Local::now().date_naive() {
                local.format("%H:%M").to_string()
            } else {
                local.format("%d/%m %H:%M").to_string()
            }
        })
        .unwrap_or_default()
}

fn span(secs: i64) -> String {
    let s = secs.max(0);
    match s {
        0..=59 => format!("{s}s"),
        60..=3599 => format!("{} min", s / 60),
        3600..=86_399 => format!("{} h {} min", s / 3600, (s % 3600) / 60),
        _ => format!("{} days", s / 86_400),
    }
}

fn ago(ts: i64) -> String {
    let d = now() - ts;
    if d < 10 {
        format!("just now ({})", clock(ts))
    } else {
        format!("{} ago ({})", span(d), clock(ts))
    }
}

impl Applet {
    fn refresh(&mut self, reload_state: bool) -> app::Task<Message> {
        if self.refreshing {
            return Task::none();
        }
        self.refreshing = true;
        let prev = self.status.clone();
        cosmic::task::future(async move { Message::Refreshed(fetch_status(prev, reload_state).await) })
    }

    fn headline(&self) -> String {
        let s = &self.status;
        if !s.loaded {
            return "Checking…".into();
        }
        match s.health() {
            Health::Syncing => match (s.run.phase, s.run.progress) {
                (Some("Syncing files"), Some((label, d, t))) => {
                    format!("Syncing — {label} {d}/{t}")
                }
                (Some(phase), _) => format!("{phase}…"),
                _ => "Syncing…".into(),
            },
            Health::Error if s.result != "success" => "Last sync failed".into(),
            Health::Error => format!("Synced with {} error(s)", s.run.errors.unwrap_or(0)),
            Health::Paused => "Automatic sync paused".into(),
            Health::Ok => "Up to date".into(),
        }
    }
}

fn info_row<'a>(label: &'a str, value: String) -> Element<'a, Message> {
    padded_control(
        row![text::body(label), space().width(Length::Fill), text::body(value)]
            .align_y(Alignment::Center)
            .spacing(12),
    )
    .into()
}

impl cosmic::Application for Applet {
    type Executor = cosmic::SingleThreadExecutor;
    type Flags = ();
    type Message = Message;
    const APP_ID: &'static str = ID;

    fn core(&self) -> &Core {
        &self.core
    }

    fn core_mut(&mut self) -> &mut Core {
        &mut self.core
    }

    fn init(core: Core, _flags: Self::Flags) -> (Self, app::Task<Self::Message>) {
        let mut applet = Self { core, ..Default::default() };
        let task = applet.refresh(true);
        (applet, task)
    }

    fn on_close_requested(&self, id: window::Id) -> Option<Message> {
        Some(Message::CloseRequested(id))
    }

    fn subscription(&self) -> Subscription<Message> {
        cosmic::iced::time::every(POLL).map(|_| Message::Tick)
    }

    fn update(&mut self, message: Self::Message) -> app::Task<Self::Message> {
        match message {
            Message::TogglePopup => {
                if let Some(p) = self.popup.take() {
                    return cosmic::surface::surface_task(cosmic::surface::action::destroy_popup(p));
                }
                self.action_error = None;
                let open = cosmic::surface::surface_task(cosmic::surface::action::app_popup(
                    |_| Default::default(),
                    |app: &mut Applet| {
                        let new_id = window::Id::unique();
                        app.popup.replace(new_id);
                        let mut settings = app.core.applet.get_popup_settings(
                            app.core.main_window_id().unwrap(),
                            new_id,
                            None,
                            None,
                            None,
                        );
                        settings.positioner.size_limits = cosmic::iced::Limits::NONE
                            .min_width(320.0)
                            .max_width(420.0)
                            .min_height(100.0)
                            .max_height(800.0);
                        settings
                    },
                    None,
                ));
                return Task::batch([open, self.refresh(true)]);
            }
            Message::CloseRequested(id) => {
                if Some(id) == self.popup {
                    self.popup = None;
                }
            }
            Message::Tick => return self.refresh(false),
            Message::Refreshed(status) => {
                self.refreshing = false;
                self.status = status;
            }
            Message::SyncNow => {
                self.action_error = None;
                // --no-block: return immediately; the poll picks up "activating".
                return cosmic::task::future(async {
                    Message::CommandDone(
                        run("systemctl", &["--user", "start", "--no-block", SERVICE]).await.map(drop),
                    )
                });
            }
            Message::ToggleTimer => {
                self.action_error = None;
                // start/stop (not enable/disable): a pause lasts until resumed or next login.
                let verb = if self.status.timer_active { "stop" } else { "start" };
                return cosmic::task::future(async move {
                    Message::CommandDone(run("systemctl", &["--user", verb, TIMER]).await.map(drop))
                });
            }
            Message::OpenFolder => {
                let dir = sync_dir();
                return cosmic::task::future(async move {
                    let dir = dir.to_string_lossy().into_owned();
                    Message::CommandDone(run("xdg-open", &[&dir]).await.map(drop))
                });
            }
            Message::ShowLog => {
                return cosmic::task::future(async {
                    let res = run(
                        "cosmic-term",
                        &["-e", "journalctl", "--user", "-u", SERVICE, "-n", "100", "-f"],
                    )
                    .await
                    .map(drop);
                    Message::CommandDone(res)
                });
            }
            Message::CommandDone(res) => {
                if let Err(e) = res {
                    tracing::error!("action failed: {e}");
                    self.action_error = Some(e);
                }
                return self.refresh(false);
            }
            Message::Surface(a) => {
                return cosmic::task::message(cosmic::Action::Cosmic(cosmic::app::Action::Surface(a)));
            }
        }
        Task::none()
    }

    fn view(&self) -> Element<'_, Self::Message> {
        self.core
            .applet
            .icon_button(self.status.health().icon())
            .on_press_down(Message::TogglePopup)
            .into()
    }

    fn view_window(&self, _id: window::Id) -> Element<'_, Self::Message> {
        let Spacing { space_xxs, space_s, .. } = theme::active().cosmic().spacing;
        let s = &self.status;
        let health = s.health();

        let mut content = column![
            padded_control(
                column![text::heading("iCloud Drive"), text::body(self.headline())].spacing(2)
            ),
            padded_control(divider::horizontal::default()).padding([space_xxs, space_s]),
        ];

        if health == Health::Syncing {
            if let Some(started) = s.started {
                content = content.push(info_row("Running for", span(now() - started)));
            }
        }

        let last = s
            .finished
            .filter(|_| s.result == "success")
            .or(s.last_sync)
            .map(ago)
            .unwrap_or_else(|| "never".into());
        content = content.push(info_row("Last sync", last));

        let next = match (s.timer_active, s.next_run) {
            (false, _) => "paused".to_string(),
            (true, Some(t)) if t > now() => format!("in {} ({})", span(t - now()), clock(t)),
            (true, Some(_)) => "any moment".to_string(),
            (true, None) if health == Health::Syncing => "after this run".to_string(),
            (true, None) => "—".to_string(),
        };
        content = content.push(info_row("Next sync", next));

        if health != Health::Syncing {
            let r = &s.run;
            if r.downloaded.is_some() || r.uploaded.is_some() {
                let changes = format!(
                    "↓ {}  ↑ {}  ✕ {}",
                    r.downloaded.unwrap_or(0),
                    r.uploaded.unwrap_or(0),
                    r.deleted_remote.unwrap_or(0) + r.deleted_local.unwrap_or(0),
                );
                content = content.push(info_row("Last run", changes));
            }
        }
        if let Some(n) = s.synced_files {
            content = content.push(info_row("Files tracked", n.to_string()));
        }

        if health == Health::Error {
            let detail = if s.result != "success" {
                s.run.last_line.clone().unwrap_or_else(|| format!("systemd result: {}", s.result))
            } else {
                "Some files failed — see the log for details.".to_string()
            };
            content = content.push(padded_control(text::caption(detail)).width(Length::Fill));
        }
        if let Some(e) = &self.action_error {
            content = content.push(padded_control(text::caption(format!("Error: {e}"))).width(Length::Fill));
        }

        content = content
            .push(padded_control(divider::horizontal::default()).padding([space_xxs, space_s]))
            .push(
                menu_button(text::body(if s.running { "Syncing…" } else { "Sync now" }))
                    .on_press_maybe((!s.running).then_some(Message::SyncNow)),
            )
            .push(
                menu_button(text::body(if s.timer_active {
                    "Pause automatic sync"
                } else {
                    "Resume automatic sync"
                }))
                .on_press(Message::ToggleTimer),
            )
            .push(menu_button(text::body("Open iCloud folder")).on_press(Message::OpenFolder))
            .push(menu_button(text::body("Show log")).on_press(Message::ShowLog));

        self.core.applet.popup_container(content.padding([8, 0])).into()
    }

    fn style(&self) -> Option<cosmic::iced::theme::Style> {
        Some(cosmic::applet::style())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_summary_and_progress() {
        let log = "Authenticating with iCloud...\nPhase 1: Scanning iCloud Drive...\nPhase 2: Syncing files...\n  ↓ Downloads: 3/46 done (43 remaining)  ↓ Downloads: 4/46 done (42 remaining)      x.mov  26%\n  Downloaded:          4\n  Uploaded:            1\n  Deleted from iCloud: 2\n  Deleted locally:     0\n  Already up to date:  0\n  Errors:              0\n=====";
        let r = parse_journal(log);
        assert_eq!(r.phase, Some("Syncing files"));
        assert_eq!(r.progress, Some(("Downloads", 4, 46)));
        assert_eq!((r.downloaded, r.uploaded, r.deleted_remote, r.errors), (Some(4), Some(1), Some(2), Some(0)));
    }

    #[test]
    fn last_line_on_failure() {
        let r = parse_journal("Authenticating with iCloud...\niCloud session expired. Run `uv run python authenticate.py` in a terminal.\n");
        assert!(r.last_line.unwrap().starts_with("iCloud session expired"));
    }
}
