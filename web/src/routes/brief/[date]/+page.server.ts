import days from '../../../../static/data/days.json';
import type { EntryGenerator } from './$types';

export const entries: EntryGenerator = () => {
	return (days as { date: string }[]).map((d) => ({ date: d.date }));
};
