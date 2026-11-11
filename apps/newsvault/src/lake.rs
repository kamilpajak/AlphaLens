//! Reader for the append-only news lake.
//!
//! Layout written by the Python ingest:
//! `thematic_news_lake/session_date=<YYYY-MM-DD>/run=<YYYYMMDDTHHMMSSsssZ>.parquet`,
//! one file per ingest run, holding that run's RAW per-source union before dedup
//! and before the 200-item cap.
//!
//! Two differences from the Python current view, both deliberate:
//!
//! * **No cap.** The view keeps 200 items because that is the brief budget. Days
//!   measured 2026-11-11 held 287 / 560 / 678 distinct items in the lake against the
//!   same 200 in the view, so the view truncates by 30-70% at a rate that moves day
//!   to day. A count taken from it is biased by the brief budget.
//! * **First sighting wins.** When several runs carry the same item, the view keeps
//!   the LAST run's `ingested_at`; this reader keeps the FIRST. Observation time is
//!   the earliest instant we could have known the item, and taking the last run
//!   would postdate our own knowledge.

use std::collections::HashMap;
use std::fs::File;
use std::path::{Path, PathBuf};

use arrow::array::{Array, AsArray, RecordBatch};
use arrow::compute::cast;
use arrow::datatypes::{DataType, TimeUnit};
use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

use crate::pit::{ObservationTime, ValidTime, Visibility};

/// One item as the archive holds it.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct NewsItem {
    pub id: String,
    pub source: String,
    pub visibility: Visibility,
    pub tickers: Vec<String>,
    pub title: String,
    pub body: String,
    pub url: String,
}

/// Everything the archive holds for one UTC day, plus what it took to get there.
#[derive(Clone, Debug, serde::Serialize)]
pub struct Day {
    pub date: NaiveDate,
    pub items: Vec<NewsItem>,
    /// Run files read, in filename order.
    pub runs: Vec<String>,
    /// Rows read before collapsing repeats across runs.
    pub raw_rows: usize,
    /// Items a later run re-reported with a different publication time. The first
    /// sighting is kept; this counts how often the sources disagreed.
    pub valid_time_conflicts: usize,
    /// Rows dropped because a required field was null.
    pub unusable_rows: usize,
}

#[derive(Debug, thiserror::Error)]
pub enum LakeError {
    #[error("lake root {0} does not exist")]
    MissingRoot(PathBuf),
    #[error("no partition for {date} under {root}")]
    MissingDay { root: PathBuf, date: NaiveDate },
    #[error("column {0} missing from run file")]
    MissingColumn(&'static str),
    #[error("column {column} has unusable type {actual}")]
    BadColumnType {
        column: &'static str,
        actual: String,
    },
    #[error(transparent)]
    Arrow(#[from] arrow::error::ArrowError),
    #[error(transparent)]
    Parquet(#[from] parquet::errors::ParquetError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Pit(#[from] crate::pit::PitError),
}

/// Reads one day out of the lake.
pub struct LakeReader {
    root: PathBuf,
}

impl LakeReader {
    #[must_use]
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    /// Default location beside the Python current view.
    #[must_use]
    pub fn default_root() -> PathBuf {
        let home = std::env::var_os("HOME").map_or_else(PathBuf::new, PathBuf::from);
        home.join(".alphalens").join("thematic_news_lake")
    }

    pub fn day(&self, date: NaiveDate) -> Result<Day, LakeError> {
        if !self.root.exists() {
            return Err(LakeError::MissingRoot(self.root.clone()));
        }
        let dir = self.root.join(format!("session_date={date}"));
        if !dir.is_dir() {
            return Err(LakeError::MissingDay {
                root: self.root.clone(),
                date,
            });
        }
        let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)?
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "parquet"))
            .collect();
        files.sort();

        let mut first: HashMap<String, (ObservationTime, NewsItem)> = HashMap::new();
        let mut day = Day {
            date,
            items: Vec::new(),
            runs: Vec::new(),
            raw_rows: 0,
            valid_time_conflicts: 0,
            unusable_rows: 0,
        };
        for path in &files {
            day.runs.push(
                path.file_name()
                    .map_or_else(String::new, |n| n.to_string_lossy().into_owned()),
            );
            read_run(path, &mut first, &mut day)?;
        }
        if files.is_empty() {
            return Err(LakeError::MissingDay {
                root: self.root.clone(),
                date,
            });
        }
        let mut items: Vec<NewsItem> = first.into_values().map(|(_, it)| it).collect();
        items.sort_by(|a, b| {
            a.visibility
                .valid()
                .cmp(&b.visibility.valid())
                .then_with(|| a.id.cmp(&b.id))
        });
        day.items = items;
        Ok(day)
    }
}

fn utf8_column<'a>(
    batch: &'a RecordBatch,
    name: &'static str,
    held: &'a mut Vec<arrow::array::ArrayRef>,
) -> Result<&'a arrow::array::StringArray, LakeError> {
    let col = batch
        .column_by_name(name)
        .ok_or(LakeError::MissingColumn(name))?;
    // pandas may hand us Utf8, LargeUtf8 or Utf8View depending on its version, so
    // normalise rather than branch on whatever this build happened to write.
    let casted = cast(col, &DataType::Utf8)?;
    held.push(casted);
    held.last()
        .and_then(|a| a.as_any().downcast_ref::<arrow::array::StringArray>())
        .ok_or_else(|| LakeError::BadColumnType {
            column: name,
            actual: col.data_type().to_string(),
        })
}

fn micros_column(
    batch: &RecordBatch,
    name: &'static str,
) -> Result<arrow::array::ArrayRef, LakeError> {
    let col = batch
        .column_by_name(name)
        .ok_or(LakeError::MissingColumn(name))?;
    Ok(cast(
        col,
        &DataType::Timestamp(TimeUnit::Microsecond, Some("UTC".into())),
    )?)
}

fn to_utc(micros: i64) -> Option<DateTime<Utc>> {
    Utc.timestamp_micros(micros).single()
}

fn read_run(
    path: &Path,
    first: &mut HashMap<String, (ObservationTime, NewsItem)>,
    day: &mut Day,
) -> Result<(), LakeError> {
    let reader = ParquetRecordBatchReaderBuilder::try_new(File::open(path)?)?.build()?;
    for batch in reader {
        let batch = batch?;
        day.raw_rows += batch.num_rows();
        let mut held = Vec::new();
        let ids = utf8_column(&batch, "id", &mut held)?.clone();
        let sources = utf8_column(&batch, "source", &mut held)?.clone();
        let titles = utf8_column(&batch, "title", &mut held)?.clone();
        let bodies = utf8_column(&batch, "body", &mut held)?.clone();
        let urls = utf8_column(&batch, "url", &mut held)?.clone();
        let ts = micros_column(&batch, "timestamp")?;
        let ing = micros_column(&batch, "ingested_at")?;
        let ts = ts.as_primitive::<arrow::datatypes::TimestampMicrosecondType>();
        let ing = ing.as_primitive::<arrow::datatypes::TimestampMicrosecondType>();
        let tickers = batch.column_by_name("tickers").cloned();

        for row in 0..batch.num_rows() {
            if ids.is_null(row) || ts.is_null(row) || ing.is_null(row) {
                day.unusable_rows += 1;
                continue;
            }
            let (Some(valid_at), Some(seen_at)) = (to_utc(ts.value(row)), to_utc(ing.value(row)))
            else {
                day.unusable_rows += 1;
                continue;
            };
            let valid = ValidTime::new(valid_at);
            let observed = ObservationTime::new(seen_at);
            let visibility = match Visibility::from_live(valid, observed) {
                Ok(v) => v,
                // A run that recorded an item before its own publication stamp is a
                // source bug, not an archive fact. Count it; never silently date it.
                Err(_) => {
                    day.unusable_rows += 1;
                    continue;
                }
            };
            let id = ids.value(row).to_owned();
            match first.get(&id) {
                Some((seen, kept)) if *seen <= observed => {
                    if kept.visibility.valid() != valid {
                        day.valid_time_conflicts += 1;
                    }
                    continue;
                }
                _ => {}
            }
            let item = NewsItem {
                id: id.clone(),
                source: sources.value(row).to_owned(),
                visibility,
                tickers: string_list_at(tickers.as_ref(), row),
                title: titles.value(row).to_owned(),
                body: bodies.value(row).to_owned(),
                url: urls.value(row).to_owned(),
            };
            first.insert(id, (observed, item));
        }
    }
    Ok(())
}

fn string_list_at(col: Option<&arrow::array::ArrayRef>, row: usize) -> Vec<String> {
    let Some(col) = col else { return Vec::new() };
    if col.is_null(row) {
        return Vec::new();
    }
    let Ok(listed) = cast(col, &DataType::new_list(DataType::Utf8, true)) else {
        return Vec::new();
    };
    let Some(list) = listed.as_any().downcast_ref::<arrow::array::ListArray>() else {
        return Vec::new();
    };
    let values = list.value(row);
    let Ok(strings) = cast(&values, &DataType::Utf8) else {
        return Vec::new();
    };
    strings
        .as_any()
        .downcast_ref::<arrow::array::StringArray>()
        .map(|a| {
            (0..a.len())
                .filter(|i| !a.is_null(*i))
                .map(|i| a.value(i).to_owned())
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use std::sync::Arc;

    use arrow::array::{ListArray, StringArray, TimestampMicrosecondArray};
    use arrow::datatypes::{Field, Schema};
    use parquet::arrow::ArrowWriter;

    use super::*;
    use crate::pit::Cutoff;

    /// One row of the lake schema, as the Python ingest writes it.
    struct Row {
        id: &'static str,
        source: &'static str,
        published_micros: i64,
        ingested_micros: i64,
    }

    fn schema() -> Arc<Schema> {
        let tz: Arc<str> = Arc::from("UTC");
        Arc::new(Schema::new(vec![
            Field::new("id", DataType::Utf8, true),
            Field::new("source", DataType::Utf8, true),
            Field::new(
                "timestamp",
                DataType::Timestamp(TimeUnit::Microsecond, Some(tz.clone())),
                true,
            ),
            Field::new("tickers", DataType::new_list(DataType::Utf8, true), true),
            Field::new("title", DataType::Utf8, true),
            Field::new("body", DataType::Utf8, true),
            Field::new("url", DataType::Utf8, true),
            Field::new(
                "ingested_at",
                DataType::Timestamp(TimeUnit::Microsecond, Some(tz)),
                true,
            ),
        ]))
    }

    fn write_run(dir: &Path, name: &str, rows: &[Row]) {
        let tz: Arc<str> = Arc::from("UTC");
        let ids: Vec<Option<&str>> = rows
            .iter()
            .map(|r| if r.id.is_empty() { None } else { Some(r.id) })
            .collect();
        let tickers = ListArray::from_iter_primitive::<arrow::datatypes::Int32Type, _, _>(
            rows.iter().map(|_| Some(Vec::<Option<i32>>::new())),
        );
        // Cast the empty int list to a string list so the fixture matches the real
        // schema while staying easy to build.
        let tickers = cast(&tickers, &DataType::new_list(DataType::Utf8, true)).unwrap();
        let batch = RecordBatch::try_new(
            schema(),
            vec![
                Arc::new(StringArray::from(ids)),
                Arc::new(StringArray::from(
                    rows.iter().map(|r| r.source).collect::<Vec<_>>(),
                )),
                Arc::new(
                    TimestampMicrosecondArray::from(
                        rows.iter().map(|r| r.published_micros).collect::<Vec<_>>(),
                    )
                    .with_timezone(tz.clone()),
                ),
                tickers,
                Arc::new(StringArray::from(
                    rows.iter().map(|r| r.id).collect::<Vec<_>>(),
                )),
                Arc::new(StringArray::from(vec!["body"; rows.len()])),
                Arc::new(StringArray::from(vec!["https://example.test"; rows.len()])),
                Arc::new(
                    TimestampMicrosecondArray::from(
                        rows.iter().map(|r| r.ingested_micros).collect::<Vec<_>>(),
                    )
                    .with_timezone(tz),
                ),
            ],
        )
        .unwrap();
        let file = File::create(dir.join(name)).unwrap();
        let mut w = ArrowWriter::try_new(file, batch.schema(), None).unwrap();
        w.write(&batch).unwrap();
        w.close().unwrap();
    }

    const SEC: i64 = 1_000_000;

    fn lake_with_two_runs() -> tempfile::TempDir {
        let root = tempfile::tempdir().unwrap();
        let part = root.path().join("session_date=2026-07-01");
        std::fs::create_dir_all(&part).unwrap();
        write_run(
            &part,
            "run=20260702T003000000Z.parquet",
            &[
                Row {
                    id: "a",
                    source: "gdelt",
                    published_micros: 100 * SEC,
                    ingested_micros: 1_000 * SEC,
                },
                Row {
                    id: "b",
                    source: "rss",
                    published_micros: 200 * SEC,
                    ingested_micros: 1_000 * SEC,
                },
            ],
        );
        write_run(
            &part,
            "run=20260702T043000000Z.parquet",
            &[
                // same item, later run: the first sighting must win
                Row {
                    id: "a",
                    source: "gdelt",
                    published_micros: 100 * SEC,
                    ingested_micros: 9_000 * SEC,
                },
                // only the later run has it
                Row {
                    id: "c",
                    source: "polygon",
                    published_micros: 300 * SEC,
                    ingested_micros: 9_000 * SEC,
                },
            ],
        );
        root
    }

    fn day_of(root: &Path) -> Day {
        LakeReader::new(root)
            .day(NaiveDate::from_ymd_opt(2026, 7, 1).unwrap())
            .unwrap()
    }

    #[test]
    fn collapses_repeats_across_runs_and_counts_the_raw_rows() {
        let root = lake_with_two_runs();
        let day = day_of(root.path());
        assert_eq!(day.raw_rows, 4, "every row of every run is counted");
        assert_eq!(day.items.len(), 3, "three distinct ids");
        assert_eq!(day.runs.len(), 2);
        assert_eq!(day.unusable_rows, 0);
    }

    #[test]
    fn first_sighting_wins_so_our_knowledge_is_not_postdated() {
        let root = lake_with_two_runs();
        let day = day_of(root.path());
        let a = day.items.iter().find(|i| i.id == "a").unwrap();
        assert_eq!(
            a.visibility.observed().inner().timestamp(),
            1_000,
            "the earlier run's instant, not the later one"
        );
    }

    #[test]
    fn an_item_is_invisible_before_the_run_that_fetched_it() {
        let root = lake_with_two_runs();
        let day = day_of(root.path());
        let c = day.items.iter().find(|i| i.id == "c").unwrap();
        let after_publication = Cutoff::new(Utc.timestamp_opt(400, 0).single().unwrap());
        assert!(
            !c.visibility.visible_at(after_publication),
            "published at 300 but not fetched until 9000"
        );
        let after_fetch = Cutoff::new(Utc.timestamp_opt(9_000, 0).single().unwrap());
        assert!(c.visibility.visible_at(after_fetch));
    }

    #[test]
    fn rows_without_an_id_or_a_timestamp_are_counted_not_dropped_silently() {
        let root = tempfile::tempdir().unwrap();
        let part = root.path().join("session_date=2026-07-01");
        std::fs::create_dir_all(&part).unwrap();
        write_run(
            &part,
            "run=20260702T003000000Z.parquet",
            &[
                Row {
                    id: "",
                    source: "rss",
                    published_micros: 100 * SEC,
                    ingested_micros: 1_000 * SEC,
                },
                Row {
                    id: "ok",
                    source: "rss",
                    published_micros: 100 * SEC,
                    ingested_micros: 1_000 * SEC,
                },
            ],
        );
        let day = day_of(root.path());
        assert_eq!(day.raw_rows, 2);
        assert_eq!(day.items.len(), 1);
        assert_eq!(day.unusable_rows, 1, "the null id is reported, not hidden");
    }

    #[test]
    fn a_row_fetched_before_its_own_publication_stamp_is_unusable() {
        let root = tempfile::tempdir().unwrap();
        let part = root.path().join("session_date=2026-07-01");
        std::fs::create_dir_all(&part).unwrap();
        write_run(
            &part,
            "run=20260702T003000000Z.parquet",
            &[Row {
                id: "backwards",
                source: "rss",
                published_micros: 5_000 * SEC,
                ingested_micros: 1_000 * SEC,
            }],
        );
        let day = day_of(root.path());
        assert!(day.items.is_empty());
        assert_eq!(day.unusable_rows, 1);
    }

    #[test]
    fn a_missing_day_is_an_error_not_an_empty_result() {
        let root = lake_with_two_runs();
        let err = LakeReader::new(root.path())
            .day(NaiveDate::from_ymd_opt(2026, 7, 2).unwrap())
            .unwrap_err();
        assert!(matches!(err, LakeError::MissingDay { .. }), "got {err:?}");
    }

    #[test]
    fn a_missing_root_is_an_error() {
        let err = LakeReader::new("/nonexistent/newsvault/lake")
            .day(NaiveDate::from_ymd_opt(2026, 7, 1).unwrap())
            .unwrap_err();
        assert!(matches!(err, LakeError::MissingRoot(_)), "got {err:?}");
    }

    /// Opt-in check against the production lake. Set `NEWSVAULT_TEST_LAKE` to a copy
    /// of `thematic_news_lake`. Skipped when unset so CI needs no data.
    #[test]
    fn matches_the_python_count_on_the_real_lake() {
        let Some(root) = std::env::var_os("NEWSVAULT_TEST_LAKE") else {
            return;
        };
        let day = LakeReader::new(root)
            .day(NaiveDate::from_ymd_opt(2026, 7, 1).unwrap())
            .unwrap();
        // Counted independently in pandas on 2026-11-11.
        assert_eq!(day.raw_rows, 1_649);
        assert_eq!(day.items.len(), 287);
    }
}
