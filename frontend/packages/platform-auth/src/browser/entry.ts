import type {
    AgentAdminItem,
    AgentCatalogSettings,
    EntryCatalog,
    EntrySession,
    ManagedUser,
    UserPolicy,
} from "../models/catalog";
import type {
    DirectoryOrganizationAccess,
    DirectoryStatusResponse,
    DirectorySyncResult,
    DirectoryUserAccess,
} from "../models/directory";
import { BrowserHttpClient, jsonRequest } from "./http";

class EntryApiClient {
    constructor(protected readonly http: BrowserHttpClient) {}

    protected async request<T>(path: string, init?: RequestInit): Promise<T> {
        return await this.http.requestJson<T>(`/api/entry${path}`, init);
    }
}

export class EntryClient extends EntryApiClient {
    me(): Promise<EntrySession> {
        return this.request<EntrySession>("/me");
    }
    catalog(): Promise<EntryCatalog> {
        return this.request<EntryCatalog>("/catalog");
    }
    managementCatalog(): Promise<EntryCatalog> {
        return this.request<EntryCatalog>("/management/catalog");
    }
}

export class EntryAdminClient extends EntryApiClient {
    async users(): Promise<ManagedUser[]> {
        return (await this.request<{ users: ManagedUser[] }>("/admin/users"))
            .users;
    }

    async agents(): Promise<AgentAdminItem[]> {
        return (
            await this.request<{ agents: AgentAdminItem[] }>("/admin/agents")
        ).agents;
    }

    async directoryStatus(): Promise<DirectoryStatusResponse> {
        return await this.request<DirectoryStatusResponse>(
            "/admin/directory/status",
        );
    }

    async syncDirectory(): Promise<DirectorySyncResult> {
        return (
            await this.request<{ result: DirectorySyncResult }>(
                "/admin/directory/sync",
                { method: "POST" },
            )
        ).result;
    }

    async directoryOrganizations(
        query = "",
    ): Promise<DirectoryOrganizationAccess[]> {
        const params = new URLSearchParams();
        if (query.trim()) params.set("query", query.trim());
        const suffix = params.size ? `?${params.toString()}` : "";
        return (
            await this.request<{
                organizations: DirectoryOrganizationAccess[];
            }>(`/admin/directory/organizations${suffix}`)
        ).organizations;
    }

    async replaceDirectoryOrganizationAccess(
        tenantId: string,
        organizationId: string,
        policy: { agent_keys: string[]; include_descendants: boolean },
    ): Promise<DirectoryOrganizationAccess> {
        return (
            await this.request<{ organization: DirectoryOrganizationAccess }>(
                `/admin/directory/organizations/${encodeURIComponent(tenantId)}/${encodeURIComponent(organizationId)}/access`,
                jsonRequest(policy, "PUT"),
            )
        ).organization;
    }

    async directoryUsers(
        options: {
            query?: string;
            tenant_id?: string;
            organization_id?: string;
        } = {},
    ): Promise<DirectoryUserAccess[]> {
        const params = new URLSearchParams();
        if (options.query?.trim()) params.set("query", options.query.trim());
        if (options.tenant_id) params.set("tenant_id", options.tenant_id);
        if (options.organization_id)
            params.set("organization_id", options.organization_id);
        const suffix = params.size ? `?${params.toString()}` : "";
        return (
            await this.request<{ users: DirectoryUserAccess[] }>(
                `/admin/directory/users${suffix}`,
            )
        ).users;
    }

    async replaceDirectoryUserAccess(
        tenantId: string,
        userId: string,
        agentKeys: string[],
    ): Promise<DirectoryUserAccess> {
        return (
            await this.request<{ user: DirectoryUserAccess }>(
                `/admin/directory/users/${encodeURIComponent(tenantId)}/${encodeURIComponent(userId)}/access`,
                jsonRequest({ agent_keys: agentKeys }, "PUT"),
            )
        ).user;
    }

    async replaceUserPolicy(
        subject: string,
        policy: UserPolicy,
    ): Promise<ManagedUser> {
        return (
            await this.request<{ user: ManagedUser }>(
                `/admin/users/${encodeURIComponent(subject)}/policy`,
                jsonRequest(policy, "PUT"),
            )
        ).user;
    }

    async updateAgentCatalog(
        key: string,
        catalog: AgentCatalogSettings,
    ): Promise<AgentAdminItem> {
        return (
            await this.request<{ agent: AgentAdminItem }>(
                `/admin/agents/${encodeURIComponent(key)}/catalog`,
                jsonRequest(catalog, "PATCH"),
            )
        ).agent;
    }
}
