import { fail } from '@sveltejs/kit';
import { StatsError, statsPost } from '$lib/server/stats';
import type { AskAnswer } from '$lib/types';
import type { Actions } from './$types';

/** The API refuses anything shorter, and truncates anything longer. */
const MIN_QUESTION = 3;
const MAX_QUESTION = 400;

const FORWARDED = ['cf-connecting-ip', 'x-real-ip', 'x-forwarded-for'];

function caller(request: Request, socket: () => string): Record<string, string> {
	const headers: Record<string, string> = {};
	for (const name of FORWARDED) {
		const value = request.headers.get(name);
		if (value) headers[name] = value;
	}
	if (Object.keys(headers).length) return headers;

	try {
		return { 'x-forwarded-for': socket() };
	} catch {
		return {};
	}
}

export const actions: Actions = {
	default: async ({ request, fetch, getClientAddress }) => {
		const form = await request.formData();
		const question = String(form.get('question') ?? '')
			.trim()
			.slice(0, MAX_QUESTION);

		if (question.length < MIN_QUESTION) {
			return fail(400, { question, error: 'Ask a question first.' });
		}

		try {
			const answer = await statsPost<AskAnswer>(
				fetch,
				'/ask',
				{ question },
				caller(request, getClientAddress)
			);
			return { question, answer };
		} catch (error) {
			const status = error instanceof StatsError ? error.status : 502;
			const message =
				error instanceof StatsError
					? error.message
					: 'The stats API did not answer. Try again in a moment.';
			return fail(status, { question, error: message });
		}
	}
};
