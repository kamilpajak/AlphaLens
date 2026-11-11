//! `newsvault` - command line over the point-in-time news archive.
//!
//! Follows the repo's CLI contract: the result goes to stdout and nothing else does,
//! logs and errors go to stderr, JSON mode emits exactly one value, and
//! `newsvault schema` describes the command tree for a caller that is not a human.

use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;

use chrono::{DateTime, NaiveDate, Utc};
use clap::{Parser, Subcommand, ValueEnum};

use newsvault::lake::{Day, LakeError, LakeReader};
use newsvault::pit::Cutoff;

/// Exit codes, kept small and documented. Domain detail belongs in the error body.
const EXIT_USAGE: u8 = 2;
const EXIT_NOT_FOUND: u8 = 4;
const EXIT_FAILED: u8 = 1;

#[derive(Parser)]
#[command(
    name = "newsvault",
    about = "Point-in-time news archive: complete days, honest timestamps.",
    version
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Every item the archive holds for one UTC day.
    Day {
        /// UTC day, as YYYY-MM-DD.
        date: String,
        /// Lake root. Defaults to ~/.alphalens/thematic_news_lake.
        #[arg(long)]
        lake: Option<PathBuf>,
        /// Keep only items visible at this instant (RFC 3339). Both the publication
        /// and the observation must be at or before it.
        #[arg(long)]
        at: Option<String>,
        #[arg(long, value_enum, default_value_t = Format::Human)]
        format: Format,
    },
    /// Machine-readable description of this tool.
    Schema {
        #[arg(long, value_enum, default_value_t = Format::Json)]
        format: Format,
    },
}

#[derive(Clone, Copy, ValueEnum)]
enum Format {
    Human,
    Json,
    Ndjson,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            let code = match e.downcast_ref::<LakeError>() {
                Some(LakeError::MissingRoot(_) | LakeError::MissingDay { .. }) => EXIT_NOT_FOUND,
                _ => EXIT_FAILED,
            };
            let _ = writeln!(std::io::stderr(), "error: {e:#}");
            ExitCode::from(code)
        }
    }
}

fn run(cli: Cli) -> anyhow::Result<()> {
    match cli.command {
        Command::Schema { format } => {
            print_schema(format);
            Ok(())
        }
        Command::Day {
            date,
            lake,
            at,
            format,
        } => {
            let date: NaiveDate = date
                .parse()
                .map_err(|_| usage(format!("date {date:?} is not YYYY-MM-DD")))?;
            let cutoff = match at {
                None => None,
                Some(raw) => {
                    Some(Cutoff::new(raw.parse::<DateTime<Utc>>().map_err(|_| {
                        usage(format!("--at {raw:?} is not an RFC 3339 instant"))
                    })?))
                }
            };
            let reader = LakeReader::new(lake.unwrap_or_else(LakeReader::default_root));
            let mut day = reader.day(date)?;
            let before = day.items.len();
            if let Some(cutoff) = cutoff {
                day.items.retain(|i| i.visibility.visible_at(cutoff));
            }
            emit(&day, before, format)
        }
    }
}

fn usage(message: String) -> anyhow::Error {
    let _ = writeln!(std::io::stderr(), "usage error: {message}");
    std::process::exit(i32::from(EXIT_USAGE));
}

fn emit(day: &Day, before_cutoff: usize, format: Format) -> anyhow::Result<()> {
    let out = std::io::stdout();
    let mut out = out.lock();
    match format {
        Format::Json => {
            serde_json::to_writer_pretty(
                &mut out,
                &serde_json::json!({
                    "schema": "newsvault.day/1",
                    "date": day.date,
                    "runs": day.runs,
                    "raw_rows": day.raw_rows,
                    "distinct_items": before_cutoff,
                    "items_returned": day.items.len(),
                    "valid_time_conflicts": day.valid_time_conflicts,
                    "unusable_rows": day.unusable_rows,
                    "items": day.items,
                }),
            )?;
            writeln!(out)?;
        }
        Format::Ndjson => {
            for item in &day.items {
                serde_json::to_writer(&mut out, item)?;
                writeln!(out)?;
            }
            serde_json::to_writer(
                &mut out,
                &serde_json::json!({
                    "type": "summary",
                    "schema": "newsvault.day/1",
                    "date": day.date,
                    "raw_rows": day.raw_rows,
                    "distinct_items": before_cutoff,
                    "items_returned": day.items.len(),
                }),
            )?;
            writeln!(out)?;
        }
        Format::Human => {
            writeln!(out, "day              {}", day.date)?;
            writeln!(out, "run files        {}", day.runs.len())?;
            writeln!(out, "raw rows         {}", day.raw_rows)?;
            writeln!(out, "distinct items   {before_cutoff}")?;
            writeln!(out, "returned         {}", day.items.len())?;
            writeln!(out, "valid-time clash {}", day.valid_time_conflicts)?;
            writeln!(out, "unusable rows    {}", day.unusable_rows)?;
            if let (Some(first), Some(last)) = (day.items.first(), day.items.last()) {
                writeln!(
                    out,
                    "published        {} .. {}",
                    first.visibility.valid().inner(),
                    last.visibility.valid().inner()
                )?;
                let earliest = day
                    .items
                    .iter()
                    .map(|i| i.visibility.observed())
                    .min()
                    .map(|o| o.inner());
                if let Some(e) = earliest {
                    writeln!(out, "first sighting   {e}")?;
                }
            }
        }
    }
    Ok(())
}

fn print_schema(format: Format) {
    let doc = serde_json::json!({
        "schema": "newsvault.schema/1",
        "tool": "newsvault",
        "commands": [
            {
                "path": ["day"],
                "summary": "Every item the archive holds for one UTC day.",
                "args": [{"name": "date", "type": "date", "required": true}],
                "options": [
                    {"name": "lake", "type": "path"},
                    {"name": "at", "type": "rfc3339",
                     "doc": "keep only items visible at this instant (publication AND observation)"},
                    {"name": "format", "type": "enum", "values": ["human", "json", "ndjson"]}
                ],
                "output": "newsvault.day/1",
                "mutates": false
            },
            {"path": ["schema"], "summary": "This document.", "mutates": false}
        ],
        "exit_codes": {"0": "ok", "1": "failed", "2": "usage", "4": "not found"}
    });
    match format {
        Format::Human => println!("{doc:#}"),
        Format::Json | Format::Ndjson => println!("{doc}"),
    }
}
