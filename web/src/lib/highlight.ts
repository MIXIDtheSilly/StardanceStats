const ESCAPES: Record<string, string> = {
	'&': '&amp;',
	'<': '&lt;',
	'>': '&gt;',
	'"': '&quot;',
	"'": '&#39;'
};

function escape(text: string): string {
	return text.replace(/[&<>"']/g, (character) => ESCAPES[character]);
}

/** Regex-quote, so a search for c++ is not read as a repeat. */
function quote(term: string): string {
	return term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function terms(query: string): string[] {
	const phrases = [...query.matchAll(/"([^"]*)"/g)].map((match) => match[1]);
	const loose = query.replace(/"[^"]*"/g, ' ').split(/\s+/);
	return [...phrases, ...loose]
		.map((term) => term.trim())
		.filter((term) => term.length > 1 && !term.startsWith('-'));
}

/** Longest first, so a search for "designs" is not cut short at "design". */
const SUFFIXES = ['ings', 'ing', 'edly', 'ed', 'es', 'ly', 's'];

/** A rough stem, because the API's text index stems: "soldering" has to mark "solder". */
function stem(word: string): string {
	const lower = word.toLowerCase();
	for (const suffix of SUFFIXES) {
		// A root under four letters stems to noise.
		if (lower.endsWith(suffix) && word.length - suffix.length >= 4) {
			return undouble(word.slice(0, -suffix.length));
		}
	}
	return word;
}

/** "stopped" cuts to "stopp", which is not how the word is spelled anywhere else. */
function undouble(word: string): string {
	const last = (word.at(-1) ?? '').toLowerCase();
	const twice = last === (word.at(-2) ?? '').toLowerCase();
	return twice && !'aeioulsfz'.includes(last) ? word.slice(0, -1) : word;
}

/** The trailing \w* is what marks the whole word a stem matched. */
function matcher(query: string, flags: string): RegExp | null {
	const wanted = terms(query);
	if (!wanted.length) return null;
	// Longest first, so "space station" wins the overlap against "space".
	const pattern = [...wanted]
		.sort((a, b) => b.length - a.length)
		.map((term) => quote(term.includes(' ') ? term : stem(term)))
		.join('|');
	return new RegExp(`\\b(?:${pattern})\\w*`, flags);
}

export function firstMatch(text: string, query: string): number {
	const finder = matcher(query, 'i');
	return finder ? text.search(finder) : -1;
}

/** `text` as HTML with matches marked; escaped here, since the template uses @html. */
export function highlight(text: string, query: string): string {
	const finder = matcher(query, 'gi');
	if (!finder) return escape(text);

	let out = '';
	let at = 0;
	for (const match of text.matchAll(finder)) {
		out += escape(text.slice(at, match.index)) + `<mark>${escape(match[0])}</mark>`;
		at = match.index + match[0].length;
	}
	return out + escape(text.slice(at));
}
