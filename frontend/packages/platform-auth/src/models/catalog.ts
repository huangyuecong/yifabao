export type AgentLifecycle = "online" | "maintenance" | "offline";

export interface AgentCatalogItem {
    key: string;
    display_name: string;
    entry_url: string | null;
    icon: string;
    category: string;
    description: string;
    lifecycle: AgentLifecycle;
    management_url?: string | null;
}

export interface AgentCatalogSettings {
    display_name: string;
    icon: string;
    category: string;
    description: string;
    lifecycle: AgentLifecycle;
    sort_order: number;
    management_url: string | null;
}

export interface AgentRegistrationDetails {
    slug: string;
    entry_url: string;
    management_url: string;
    backend_url: string;
}

export interface AgentAdminItem {
    key: string;
    catalog: AgentCatalogSettings;
    registration: AgentRegistrationDetails;
}

export interface ManagedUser {
    subject: string;
    display_name: string;
    department: string | null;
    roles: string[];
    clearance: string;
    enabled: boolean;
    is_admin: boolean;
    agent_keys: string[];
    manageable_agent_keys: string[];
    direct_agent_keys: string[];
    organization_agent_keys: string[];
    organizations: string[];
    directory_linked: boolean;
}

export interface UserPolicy {
    enabled: boolean;
    is_admin: boolean;
    clearance: string;
    agent_keys: string[];
    manageable_agent_keys?: string[] | null;
}

export interface EntrySession {
    user: ManagedUser;
}

export interface EntryCatalog {
    agents: AgentCatalogItem[];
}
