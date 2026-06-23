/**
 * Footer time formatters — everything renders in the *viewer's* local zone.
 *
 * The footer once mixed two unlabelled zones: the next-open label was the
 * exchange-local time (ET for XNYS) while the ambient clock was UTC, so
 * "opens 09:30 … 11:53 utc" read as a contradiction. Both now resolve to the
 * viewer's own zone via ``Intl``, which is DST-aware: a Warsaw viewer sees
 * "15:30 … 13:53 CEST", a Tokyo viewer "22:30 … 20:53 JST".
 *
 * Both functions accept optional ``timeZone``/``locale`` overrides. Production
 * omits them so the browser supplies the viewer's real zone + locale; the unit
 * suite passes them explicitly so assertions don't depend on the CI runner's
 * system zone.
 */

interface ZoneOpts {
	/** IANA zone (e.g. ``Europe/Warsaw``). Omit in production to inherit the
	 *  runtime's local zone. */
	timeZone?: string;
	/** BCP-47 locale for the zone *abbreviation* only. Omit in production to
	 *  inherit the browser locale so a pl-PL viewer gets "CEST", ja-JP "JST". */
	locale?: string;
}

/**
 * "mon 15:30" — weekday + 24h time in the viewer's zone, lowercase.
 *
 * The weekday name is always English ('en-US') because the footer copy is
 * English; only the clock reading shifts with the zone. No trailing zone token
 * — the ambient clock right beside it carries the label, and both are now the
 * same zone, so repeating it on the chip would be noise.
 */
export function formatLocalWeekdayTime(iso: string, opts: ZoneOpts = {}): string {
	return new Intl.DateTimeFormat('en-US', {
		weekday: 'short',
		hour: '2-digit',
		minute: '2-digit',
		hour12: false,
		...(opts.timeZone ? { timeZone: opts.timeZone } : {})
	})
		.format(new Date(iso))
		.toLowerCase();
}

/**
 * "2026-06-22 13:53 CEST" — ISO date + 24h time + zone label, viewer's zone.
 *
 * The date/time half is formatted with 'en-CA' so it stays ``YYYY-MM-DD`` 24h
 * regardless of the viewer's locale (we don't want "22.06.2026" for a pl-PL
 * viewer). The zone abbreviation, however, is taken in the viewer's *own*
 * locale so it reads as the name they expect (CEST / JST / EDT), falling back
 * to a "GMT+N" offset where the locale has no named abbreviation.
 */
export function formatLocalClock(date: Date, opts: ZoneOpts = {}): string {
	const dateTime = new Intl.DateTimeFormat('en-CA', {
		year: 'numeric',
		month: '2-digit',
		day: '2-digit',
		hour: '2-digit',
		minute: '2-digit',
		hour12: false,
		...(opts.timeZone ? { timeZone: opts.timeZone } : {})
	})
		.format(date)
		.replace(', ', ' ');

	const parts = new Intl.DateTimeFormat(opts.locale, {
		hour: '2-digit',
		minute: '2-digit',
		timeZoneName: 'short',
		...(opts.timeZone ? { timeZone: opts.timeZone } : {})
	}).formatToParts(date);
	const zone = parts.find((p) => p.type === 'timeZoneName')?.value ?? '';

	return zone ? `${dateTime} ${zone}` : dateTime;
}
