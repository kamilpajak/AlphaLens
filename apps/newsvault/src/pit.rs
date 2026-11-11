//! The point-in-time contract, as types.
//!
//! Two distinct instants belong to every news item:
//!
//! * **valid time** - when the world published it;
//! * **observation time** - when we could first have known it.
//!
//! They are separate types on purpose. A measured leak is what motivates it: a
//! Perplexity retrieval filtered on publication date alone returned documents we
//! could not have seen at the time in 60.2% of probes, while adding the filter on
//! last-update time closed it on all 69 (2026-11-11). The failure was not a wrong
//! comparison - it was asking only ONE of the two questions. So this module offers
//! no way to ask only one: [`Visibility::visible_at`] is the single query, and the
//! half-predicates it is built from are private.
//!
//! The two instants also cannot be compared with each other, because they are
//! different types:
//!
//! ```compile_fail
//! use newsvault::pit::{ObservationTime, ValidTime};
//! # use chrono::{TimeZone, Utc};
//! let v = ValidTime::new(Utc.timestamp_opt(0, 0).single().unwrap());
//! let o = ObservationTime::new(Utc.timestamp_opt(1, 0).single().unwrap());
//! let _ = v < o; // mismatched types
//! ```

use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};

/// When the world published an item.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct ValidTime(DateTime<Utc>);

/// When we could first have known an item.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct ObservationTime(DateTime<Utc>);

/// The instant an analysis claims to stand at.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Cutoff(DateTime<Utc>);

impl ValidTime {
    #[must_use]
    pub const fn new(at: DateTime<Utc>) -> Self {
        Self(at)
    }
    #[must_use]
    pub const fn inner(self) -> DateTime<Utc> {
        self.0
    }
}

impl ObservationTime {
    #[must_use]
    pub const fn new(at: DateTime<Utc>) -> Self {
        Self(at)
    }
    #[must_use]
    pub const fn inner(self) -> DateTime<Utc> {
        self.0
    }
}

impl Cutoff {
    #[must_use]
    pub const fn new(at: DateTime<Utc>) -> Self {
        Self(at)
    }
    #[must_use]
    pub const fn inner(self) -> DateTime<Utc> {
        self.0
    }

    fn admits_valid(self, t: ValidTime) -> bool {
        t.0 <= self.0
    }

    fn admits_observation(self, t: ObservationTime) -> bool {
        t.0 <= self.0
    }
}

/// How long after publication an item became visible to us, and the probe that
/// established it.
///
/// A backfill cannot invent this number. [`Visibility::from_backfill`] is the only
/// way to date a backfilled item, and it takes a `MeasuredLag`, so "I fetched this
/// a year later and will pretend I saw it on the day" has no spelling here.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct MeasuredLag {
    /// Identifier of the probe run that measured this lag.
    pub probe_id: String,
    /// How many documents the probe checked.
    pub probes: u32,
    /// How many of them turned out not to have been visible at the claimed instant.
    pub leaks: u32,
    /// The lag itself, in whole seconds.
    pub seconds: i64,
}

impl MeasuredLag {
    /// Fraction of probes that leaked. A lag with a non-zero rate is still usable,
    /// but the rate travels with it so a panel can disclose it.
    #[must_use]
    pub fn leak_rate(&self) -> Option<f64> {
        if self.probes == 0 {
            return None;
        }
        Some(f64::from(self.leaks) / f64::from(self.probes))
    }
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum PitError {
    #[error(
        "observation time {observed} precedes valid time {valid}: nothing is seen before it exists"
    )]
    ObservedBeforeValid {
        valid: DateTime<Utc>,
        observed: DateTime<Utc>,
    },
    #[error("measured lag {0} s is negative")]
    NegativeLag(i64),
}

/// The pair of instants that decides whether an item may enter an analysis.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Visibility {
    valid: ValidTime,
    observed: ObservationTime,
}

impl Visibility {
    /// An item recorded by a live ingest run: observation time is the run's instant.
    pub fn from_live(valid: ValidTime, run_at: ObservationTime) -> Result<Self, PitError> {
        if run_at.0 < valid.0 {
            return Err(PitError::ObservedBeforeValid {
                valid: valid.0,
                observed: run_at.0,
            });
        }
        Ok(Self {
            valid,
            observed: run_at,
        })
    }

    /// An item recovered from a vendor index after the fact. Its observation time is
    /// its publication time plus a lag somebody measured.
    pub fn from_backfill(valid: ValidTime, lag: &MeasuredLag) -> Result<Self, PitError> {
        if lag.seconds < 0 {
            return Err(PitError::NegativeLag(lag.seconds));
        }
        Ok(Self {
            valid,
            observed: ObservationTime(valid.0 + Duration::seconds(lag.seconds)),
        })
    }

    /// The only visibility query. Both instants must be at or before the cutoff.
    #[must_use]
    pub fn visible_at(&self, cutoff: Cutoff) -> bool {
        cutoff.admits_valid(self.valid) && cutoff.admits_observation(self.observed)
    }

    #[must_use]
    pub const fn valid(&self) -> ValidTime {
        self.valid
    }

    #[must_use]
    pub const fn observed(&self) -> ObservationTime {
        self.observed
    }

    #[must_use]
    pub fn lag(&self) -> Duration {
        self.observed.0 - self.valid.0
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;
    use chrono::TimeZone;

    fn at(secs: i64) -> DateTime<Utc> {
        Utc.timestamp_opt(secs, 0).single().unwrap()
    }

    fn lag(seconds: i64) -> MeasuredLag {
        MeasuredLag {
            probe_id: "pplx-2026-11-11".to_owned(),
            probes: 69,
            leaks: 0,
            seconds,
        }
    }

    #[test]
    fn live_item_is_visible_once_both_instants_have_passed() {
        let v =
            Visibility::from_live(ValidTime::new(at(100)), ObservationTime::new(at(300))).unwrap();
        assert!(!v.visible_at(Cutoff::new(at(99))), "before publication");
        assert!(
            !v.visible_at(Cutoff::new(at(200))),
            "published but not yet observed by us"
        );
        assert!(v.visible_at(Cutoff::new(at(300))), "at the observation");
        assert!(v.visible_at(Cutoff::new(at(301))));
    }

    #[test]
    fn publication_alone_does_not_make_an_item_visible() {
        // This is the 60.2% leak, as a test: filtering on publication date only
        // admits an item we could not have read yet.
        let v =
            Visibility::from_live(ValidTime::new(at(0)), ObservationTime::new(at(86_400))).unwrap();
        let cutoff = Cutoff::new(at(3_600));
        assert!(cutoff.admits_valid(v.valid()), "publication test passes");
        assert!(!v.visible_at(cutoff), "and the item is still not visible");
    }

    #[test]
    fn observation_cannot_precede_publication() {
        let err = Visibility::from_live(ValidTime::new(at(500)), ObservationTime::new(at(400)))
            .unwrap_err();
        assert_eq!(
            err,
            PitError::ObservedBeforeValid {
                valid: at(500),
                observed: at(400)
            }
        );
    }

    #[test]
    fn backfilled_item_is_not_visible_on_its_publication_day() {
        let two_days = 2 * 24 * 3_600;
        let v = Visibility::from_backfill(ValidTime::new(at(0)), &lag(two_days)).unwrap();
        assert!(
            !v.visible_at(Cutoff::new(at(3_600))),
            "a backfilled item must not claim same-day visibility"
        );
        assert!(v.visible_at(Cutoff::new(at(two_days))));
        assert_eq!(v.lag(), Duration::seconds(two_days));
    }

    #[test]
    fn negative_lag_is_rejected() {
        let err = Visibility::from_backfill(ValidTime::new(at(0)), &lag(-1)).unwrap_err();
        assert_eq!(err, PitError::NegativeLag(-1));
    }

    #[test]
    fn leak_rate_is_reported_and_is_none_without_probes() {
        assert_eq!(lag(0).leak_rate(), Some(0.0));
        let mut l = lag(0);
        l.leaks = 42;
        l.probes = 69;
        assert!((l.leak_rate().unwrap() - 42.0 / 69.0).abs() < 1e-12);
        l.probes = 0;
        assert_eq!(l.leak_rate(), None);
    }
}
