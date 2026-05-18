import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { EntryGenerator } from './$types';

export const entries: EntryGenerator = () => {
	const path = resolve('static/data/days.json');
	const raw = readFileSync(path, 'utf-8');
	const days: { date: string }[] = JSON.parse(raw);
	return days.map((d) => ({ date: d.date }));
};
