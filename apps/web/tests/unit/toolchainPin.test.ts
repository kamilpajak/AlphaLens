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

interface EnginesEntry {
	range: string;
	context: string;
}

/**
 * Every `engines: {node: ...}` range declared in a pnpm-lock.yaml body,
 * excluding entries that cannot install on the build platform (linux/x64 for
 * Pages and CI): an `os:`/`cpu:` list that omits linux/x64 means the package
 * never installs there, so its range must not gate the pin. A platform list
 * that INCLUDES linux/x64 does not exclude the entry.
 *
 * Line-oriented on purpose: the shapes involved (`engines: {...}` inline and
 * flow-style `os: [...]`/`cpu: [...]` inside the same package block) are
 * stable pnpm-9-format output, and a full YAML parse of a multi-MB lockfile
 * per test run buys nothing. The engines regex tolerates extra engine keys
 * (`{node: '>=12', npm: '>=6'}`); os/cpu order within a block is irrelevant
 * because entries are only committed at the next package header.
 */
function lockfileNodeRanges(lockfileText: string): EnginesEntry[] {
	const ranges: EnginesEntry[] = [];
	let currentPackage = '';
	let installsOnLinuxX64 = true;
	let pendingEngines: string | null = null;
	const commit = () => {
		if (pendingEngines !== null && installsOnLinuxX64) {
			ranges.push({ range: pendingEngines, context: currentPackage });
		}
	};
	for (const line of lockfileText.split('\n')) {
		const pkgHeader = line.match(/^ {2}(\S.*):$/);
		if (pkgHeader) {
			commit();
			currentPackage = pkgHeader[1];
			installsOnLinuxX64 = true;
			pendingEngines = null;
			continue;
		}
		const platform = line.match(/^ {4}(os|cpu): \[([^\]]*)\]/);
		if (platform) {
			const wanted = platform[1] === 'os' ? 'linux' : 'x64';
			const listed = platform[2].split(',').map((t) => t.trim().replace(/'/g, ''));
			if (!listed.includes(wanted)) installsOnLinuxX64 = false;
		}
		const engines = line.match(/^ {4}engines: \{node:\s*'?([^,}']+)'?\s*[,}]/);
		if (engines) pendingEngines = engines[1].trim();
	}
	commit();
	return ranges;
}

function rangesRejecting(nodeVersion: string, ranges: EnginesEntry[]) {
	return ranges.filter(({ range }) => !semver.satisfies(nodeVersion, range));
}

function liveLockfileRanges(): EnginesEntry[] {
	return lockfileNodeRanges(readFileSync(join(webRoot, 'pnpm-lock.yaml'), 'utf8'));
}

describe('lockfile engines parser', () => {
	const fixture = [
		'  plain@1.0.0:',
		"    resolution: {integrity: sha512-aaa}",
		'    engines: {node: ^20.19.0 || >=22.12.0}',
		'  quoted@1.0.0:',
		"    engines: {node: '>= 0.4'}",
		'  multikey@1.0.0:',
		"    engines: {node: '>=12', npm: '>=6'}",
		'  win-only@1.0.0:',
		'    engines: {node: ^30.0.0}',
		'    os: [win32]',
		'    cpu: [x64]',
		'  linux-shim@1.0.0:',
		'    os: [linux]',
		'    cpu: [x64]',
		'    engines: {node: ^18.0.0}',
		'  no-engines@1.0.0:',
		'    resolution: {integrity: sha512-bbb}',
		''
	].join('\n');

	it('extracts plain, quoted, and multi-key node ranges', () => {
		const byContext = Object.fromEntries(
			lockfileNodeRanges(fixture).map((e) => [e.context, e.range])
		);
		expect(byContext['plain@1.0.0']).toBe('^20.19.0 || >=22.12.0');
		expect(byContext['quoted@1.0.0']).toBe('>= 0.4');
		expect(byContext['multikey@1.0.0']).toBe('>=12');
	});

	it('excludes entries that cannot install on linux/x64 but keeps linux/x64-scoped ones', () => {
		const contexts = lockfileNodeRanges(fixture).map((e) => e.context);
		expect(contexts).not.toContain('win-only@1.0.0');
		expect(contexts).toContain('linux-shim@1.0.0');
	});
});

describe('build toolchain pins (CI ↔ Cloudflare Pages parity)', () => {
	it('pins the Node version in .node-version', () => {
		const version = pinnedNodeVersion();
		expect(semver.valid(version), `.node-version must hold an exact version, got "${version}"`).not.toBeNull();
	});

	it('pins pnpm via the packageManager field so corepack ignores the registry latest tag', () => {
		// The hash must be the hex sha512 digest of the tarball: corepack parses
		// everything after `pnpm@` as a semver, and base64 hashes (with +/=)
		// fail that parse with "expected a semver version", killing the build.
		expect(packageManagerField()).toMatch(/^pnpm@\d+\.\d+\.\d+(\+sha512\.[0-9a-f]{128})?$/);
	});

	it('finds engines.node ranges in the lockfile (parser vacuity guard)', () => {
		// vite alone declares one; an empty list means the line-oriented parser
		// rotted against a lockfile format change, not that no package cares.
		expect(liveLockfileRanges().length).toBeGreaterThan(0);
	});

	it('every extracted range is valid semver (an unparsable range must fail, not be skipped)', () => {
		const invalid = liveLockfileRanges().filter(({ range }) => semver.validRange(range) === null);
		expect(invalid).toEqual([]);
	});

	it('pinned Node satisfies every engines.node range in the lockfile', () => {
		const rejecting = rangesRejecting(pinnedNodeVersion(), liveLockfileRanges());
		expect(
			rejecting,
			`bump .node-version: ${rejecting.map((r) => `${r.context} needs ${r.range}`).join('; ')}`
		).toEqual([]);
	});

	it('flags a version the ranges reject (positive control)', () => {
		// The checker must be able to fail, or the gate above proves nothing.
		const rejecting = rangesRejecting('0.0.1', liveLockfileRanges());
		expect(rejecting.length).toBeGreaterThan(0);
	});
});
