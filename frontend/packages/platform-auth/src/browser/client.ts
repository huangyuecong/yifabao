import { AuthClient } from "./auth";
import { EntryAdminClient, EntryClient } from "./entry";
import { BrowserHttpClient } from "./http";

export interface PlatformClientOptions {
    apiBase?: string;
}

/** Public browser client for the central Yifabao platform contract. */
export class PlatformClient {
    readonly http: BrowserHttpClient;
    readonly auth: AuthClient;
    readonly entry: EntryClient;
    readonly admin: EntryAdminClient;

    constructor(options: PlatformClientOptions = {}) {
        this.http = new BrowserHttpClient(options.apiBase ?? "");
        this.auth = new AuthClient(this.http);
        this.entry = new EntryClient(this.http);
        this.admin = new EntryAdminClient(this.http);
    }
}

export function createPlatformClient(options: PlatformClientOptions = {}): PlatformClient {
    return new PlatformClient(options);
}
