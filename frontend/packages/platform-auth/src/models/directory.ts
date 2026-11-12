export interface DirectoryOrganization {
    tenant_id: string;
    id: string;
    code: string;
    name: string;
    parent_id: string | null;
    active: boolean;
}

export interface DirectoryOrganizationAccess extends DirectoryOrganization {
    agent_keys: string[];
    include_descendants: boolean;
}

export interface DirectoryAccessSource {
    agent_key: string;
    kind: "organization" | "personal";
    source_id: string;
    source_name: string;
}

export interface DirectoryUserAccess {
    tenant_id: string;
    id: string;
    username: string;
    display_name: string;
    email: string;
    mobile: string;
    active: boolean;
    organizations: DirectoryOrganization[];
    personal_agent_keys: string[];
    organization_agent_keys: string[];
    effective_agent_keys: string[];
    sources: DirectoryAccessSource[];
    linked_subject: string | null;
}

export interface DirectoryStatus {
    organization_count: number;
    user_count: number;
    membership_count: number;
    source_revision: string;
    synced_at: number | null;
}

export interface DirectoryStatusResponse {
    source_available: boolean;
    status: DirectoryStatus;
}

export interface DirectorySyncResult {
    organization_count: number;
    user_count: number;
    membership_count: number;
    source_revision: string;
}
