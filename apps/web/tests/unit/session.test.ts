import { beforeEach, describe, expect, it } from 'vitest';
import {
	clearSessionExpired,
	markSessionExpired,
	sessionExpired
} from '../../src/lib/session.svelte';

// The session store is the single source of truth for "CF Access session
// expired → show the global re-auth overlay". apiFetch flips it true on the
// two synthetic-401 paths; the layout modal renders while it's true.

describe('session expiry store', () => {
	beforeEach(() => {
		clearSessionExpired();
	});

	it('starts cleared', () => {
		expect(sessionExpired()).toBe(false);
	});

	it('markSessionExpired() flips the flag true', () => {
		markSessionExpired();
		expect(sessionExpired()).toBe(true);
	});

	it('clearSessionExpired() resets the flag to false', () => {
		markSessionExpired();
		expect(sessionExpired()).toBe(true);
		clearSessionExpired();
		expect(sessionExpired()).toBe(false);
	});

	it('markSessionExpired() is idempotent', () => {
		markSessionExpired();
		markSessionExpired();
		expect(sessionExpired()).toBe(true);
	});
});
