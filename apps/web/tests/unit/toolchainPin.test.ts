// Guards the build-toolchain pins that keep CI and Cloudflare Pages on the
// same Node/pnpm versions. Without them, Pages builds with the build image's
// default Node and whatever pnpm the npm `latest` dist-tag serves — both of
// which broke every deploy on 2026-08-24 (#1146) while CI stayed green.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import semver from 'semver';
import { describe, expect, it } from 'vitest';

const webRoot = join(__dirname, '..', '..');

function pinnedNodeVersion(): string {
	return readFileSync(join(webRoot, '.node-version'), 'utf8').trim();
}

function packageManagerField(): string {
	const pkg = JSON.parse(readFileSync(join(webRoot, 'package.json'), 'utf8'));
	return pkg.packageManager ?? '';
}

/**
 * Every `engines: {node: ...}` range declared in pnpm-lock.yaml, excluding
 * entries that are platform-scoped (os/cpu-restricted binary shims may never
 * install on the build platform, so their ranges must not gate the pin).
 *
 * Line-oriented on purpose: the lockfile is YAML, but the two shapes involved
 * (`engines: {node: <range>}` inline, and an os/cpu line inside the same
 * package block) are stable pnpm-9-format output, and a full YAML parse of a
 * multi-MB lockfile per test run buys nothing.
 */
function lockfileNodeRanges(): { range: string; context: string }[] {
	const lines = readFileSync(join(webRoot, 'pnpm-lock.yaml'), 'utf8').split('\n');
	const ranges: { range: string; context: string }[] = [];
	let currentPackage = '';
	let platformScoped = false;
	let pendingEngines: string | null = null;
	for (const line of lines) {
		const pkgHeader = line.match(/^ {2}(\S.*):$/);
		if (pkgHeader) {
			if (pendingEngines !== null && !platformScoped) {
				ranges.push({ range: pendingEngines, context: currentPackage });
			}
			currentPackage = pkgHeader[1];
			platformScoped = false;
			pendingEngines = null;
			continue;
		}
		if (/^ {4}(os|cpu):/.test(line)) platformScoped = true;
		const engines = line.match(/^ {4}engines: \{node: '?([^,}']+)'?\}/);
		if (engines) pendingEngines = engines[1].trim();
	}
	if (pendingEngines !== null && !platformScoped) {
		ranges.push({ range: pendingEngines, context: currentPackage });
	}
	return ranges;
}

function rangesRejecting(nodeVersion: string, ranges: { range: string; context: string }[]) {
	return ranges.filter(
		({ range }) => semver.validRange(range) !== null && !semver.satisfies(nodeVersion, range)
	);
}

describe('build toolchain pins (CI ↔ Cloudflare Pages parity)', () => {
	it('pins the Node version in .node-version', () => {
		const version = pinnedNodeVersion();
		expect(semver.valid(version), `.node-version must hold an exact version, got "${version}"`).not.toBeNull();
	});

	it('pins pnpm via the packageManager field so corepack ignores the registry latest tag', () => {
		expect(packageManagerField()).toMatch(/^pnpm@\d+\.\d+\.\d+/);
	});

	it('finds engines.node ranges in the lockfile (parser vacuity guard)', () => {
		// vite alone declares one; an empty list means the line-oriented parser
		// rotted against a lockfile format change, not that no package cares.
		expect(lockfileNodeRanges().length).toBeGreaterThan(0);
	});

	it('pinned Node satisfies every engines.node range in the lockfile', () => {
		const rejecting = rangesRejecting(pinnedNodeVersion(), lockfileNodeRanges());
		expect(
			rejecting,
			`bump .node-version: ${rejecting.map((r) => `${r.context} needs ${r.range}`).join('; ')}`
		).toEqual([]);
	});

	it('flags a version the ranges reject (positive control)', () => {
		// The checker must be able to fail, or the gate above proves nothing.
		const rejecting = rangesRejecting('0.0.1', lockfileNodeRanges());
		expect(rejecting.length).toBeGreaterThan(0);
	});
});
