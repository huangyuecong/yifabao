import type { AuthState, AuthUser, PasswordPublicKey } from "../models/session";
import { ApiRequestError, BrowserHttpClient, jsonRequest } from "./http";

const ssoQueryKeys = new Set([
    "code",
    "ssotoken",
    "applicationId",
    "agentid",
    "oaApplicationId",
    "system",
    "redirect",
]);
const legacyCredentialKeys = ["yifabao:access-token", "yifabao:tenant-id"];

function clearLegacyBrowserCredentials(): void {
    for (const key of legacyCredentialKeys) localStorage.removeItem(key);
}

function localReturnTarget(url: URL): string {
    const redirect = url.searchParams.get("redirect");
    if (redirect?.startsWith("/") && !redirect.startsWith("//"))
        return redirect;
    const query = new URLSearchParams(url.searchParams);
    for (const key of ssoQueryKeys) query.delete(key);
    const suffix = query.size ? `?${query.toString()}` : "";
    return `${url.pathname}${suffix}`;
}

export class AuthClient {
    constructor(private readonly http: BrowserHttpClient) {
        clearLegacyBrowserCredentials();
    }

    async me(): Promise<AuthState> {
        try {
            return await this.http.requestJson<AuthState>("/api/auth/me");
        } catch (error) {
            if (error instanceof ApiRequestError && error.status > 0)
                return { authenticated: false };
            throw error;
        }
    }

    consumeSsoRedirect(url = new URL(window.location.href)): boolean {
        const code = url.searchParams.get("code");
        const ssoToken = url.searchParams.get("ssotoken");
        const applicationId =
            url.searchParams.get("applicationId") ??
            url.searchParams.get("agentid");
        if (!(ssoToken || (code && applicationId))) return false;
        const callback = new URLSearchParams();
        for (const key of [
            "code",
            "ssotoken",
            "applicationId",
            "agentid",
            "oaApplicationId",
        ]) {
            const value = url.searchParams.get(key);
            if (value) callback.set(key, value);
        }
        callback.set("return_to", localReturnTarget(url));
        window.location.replace(
            this.http.url(`/api/auth/callback?${callback}`),
        );
        return true;
    }

    login(returnTo = window.location.pathname + window.location.search): void {
        const query = new URLSearchParams({ return_to: returnTo });
        window.location.assign(this.http.url(`/api/auth/login?${query}`));
    }

    async developmentLogin(): Promise<AuthState | null> {
        try {
            return await this.http.requestJson<AuthState>(
                "/api/auth/dev-login",
                { method: "POST" },
            );
        } catch (error) {
            if (error instanceof ApiRequestError && error.status === 404) return null;
            throw error;
        }
    }

    async passwordPublicKey(): Promise<PasswordPublicKey> {
        return await this.http.requestJson<PasswordPublicKey>(
            "/api/auth/public-key",
        );
    }

    async passwordLogin(
        username: string,
        passwordCiphertext: string,
        rememberMe = true,
    ): Promise<AuthState> {
        return await this.http.requestJson<AuthState>(
            "/api/auth/password-login",
            jsonRequest({
                username,
                password_ciphertext: passwordCiphertext,
                remember_me: rememberMe,
            }),
        );
    }

    async logout(): Promise<void> {
        const response = await this.http.request("/api/auth/logout", {
            method: "POST",
        });
        if (!response.ok && response.status !== 204) {
            throw new ApiRequestError(
                `auth logout failed: ${response.status}`,
                response.status,
            );
        }
    }

    async require(returnTo?: string): Promise<AuthUser> {
        const state = await this.me();
        if (!state.authenticated || !state.user) {
            this.login(returnTo);
            throw new Error("authentication required");
        }
        return state.user;
    }
}
