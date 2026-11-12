import { computed, ref } from "vue";
import {
    createPlatformClient,
    type PlatformClient,
} from "../browser/client";
import type { AuthUser } from "../models/session";

export interface PlatformSessionOptions {
    client?: PlatformClient;
    immediate?: boolean;
    logoutRedirect?: string | false;
}

/** Shared Vue session adapter used by Agent workspaces. */
export function useYifabaoSession(options: PlatformSessionOptions = {}) {
    const client = options.client ?? createPlatformClient();
    const user = ref<AuthUser | null>(null);
    const loading = ref(false);
    const error = ref<unknown>(null);
    const authenticated = computed(() => user.value !== null);

    async function initialize(): Promise<AuthUser | null> {
        loading.value = true;
        error.value = null;
        try {
            const state = await client.auth.me();
            user.value = state.authenticated && state.user ? state.user : null;
            return user.value;
        } catch (reason) {
            user.value = null;
            error.value = reason;
            return null;
        } finally {
            loading.value = false;
        }
    }

    async function logout(): Promise<void> {
        await client.auth.logout();
        user.value = null;
        const redirect = options.logoutRedirect ?? "/";
        if (redirect !== false) window.location.assign(redirect);
    }

    if (options.immediate !== false) void initialize();

    return {
        client,
        user,
        authenticated,
        loading,
        error,
        initialize,
        logout,
    };
}
