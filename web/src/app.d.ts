declare global {
	namespace App {
		interface Error {
			message: string;
			status?: number;
		}
	}
}

export {};
